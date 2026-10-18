"""Validate and inventory an uploaded ZIP without trusting a byte of it.

The archive is hostile input. Every rule here exists because the alternative is a
path traversal, a decompression bomb, or a file that claims to be a PDF and is
executed by something downstream:

* caps on archive size, extracted size, file count, individual file size and depth;
* names are rejected for absolute paths, ``..``, backslashes and NUL bytes;
* symlinks are rejected rather than followed;
* encrypted and unusual compression methods are rejected;
* the claimed uncompressed size is checked *before* reading, and the stream is
  capped again while writing;
* file type comes from magic bytes, not from the extension.

A failure rejects the archive as a whole, because a partially trusted archive is
worse than a clear retry. A failure to *process* one document later is separate and
never affects the other documents.
"""

from __future__ import annotations

import hashlib
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from proptally.onboarding.store import OnboardingStore

#: The upload itself. Existing statement uploads use 50 MB; keeping the same
#: ceiling means one bad archive cannot exhaust the disk before validation runs.
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_BYTES = 250 * 1024 * 1024
MAX_FILES = 200
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_DIRECTORY_DEPTH = 5
#: A healthy document archive is nowhere near this ratio; a bomb is orders of
#: magnitude above it.
MAX_COMPRESSION_RATIO = 200

SUPPORTED_SUFFIXES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".txt": "text/plain",
}

#: Files that should never be treated as documents, even though they are retained
#: in the inventory so the customer can see what was in their archive.
BLOCKED_SUFFIXES = {
    ".exe", ".dll", ".so", ".dylib", ".sh", ".bash", ".zsh", ".bat", ".cmd",
    ".ps1", ".py", ".js", ".mjs", ".cjs", ".jar", ".app", ".dmg", ".pkg",
    ".html", ".htm", ".svg", ".php", ".rb", ".pl",
}

JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


class ArchiveRejected(ValueError):
    """The archive cannot be accepted, with a message safe to show a customer."""


@dataclass(frozen=True)
class InventoryEntry:
    """One accepted file, after validation and extraction to the staging store."""

    original_filename: str
    internal_filename: str
    storage_path: str
    sha256: str
    size_bytes: int
    mime_type: str
    format: str
    unsupported: bool


def _suffix(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def _safe_name(name: str) -> str:
    """Reject a member name that could escape the extraction directory."""
    if not name or "\x00" in name:
        raise ArchiveRejected("The archive contains an empty or invalid filename.")
    if "\\" in name:
        raise ArchiveRejected(
            "The archive contains a Windows-style path; re-create it as a standard ZIP."
        )
    path = PurePosixPath(name)
    if path.is_absolute() or name.startswith("/"):
        raise ArchiveRejected("The archive contains an absolute path, which is not allowed.")
    parts = [part for part in path.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ArchiveRejected("The archive contains a path traversal ('..'), which is not allowed.")
    if len(parts) - 1 > MAX_DIRECTORY_DEPTH:
        raise ArchiveRejected("The archive nests files too deeply to be a document archive.")
    return name


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def _skip(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if any(part == "__MACOSX" for part in parts):
        return True
    if not parts:
        return True
    return parts[-1] in JUNK_NAMES or parts[-1].startswith("._")


def _format_from_magic(head: bytes, suffix: str) -> str:
    """The real format, established from the bytes rather than the filename."""
    if head.startswith(b"%PDF-"):
        return "PDF"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "IMAGE"
    if head.startswith(b"\xff\xd8\xff"):
        return "IMAGE"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "IMAGE"
    if head.startswith(b"PK\x03\x04"):
        if suffix == ".docx":
            return "DOCX"
        if suffix == ".xlsx":
            return "XLSX"
        return "ZIP"
    if head and b"\x00" not in head[:4096]:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            return "UNKNOWN"
        return "CSV" if suffix == ".csv" else "TEXT"
    return "UNKNOWN"


def _sniff_zip_format(path: Path, suffix: str) -> str:
    """Confirm an OOXML file is the kind its extension claims."""
    try:
        with zipfile.ZipFile(path) as inner:
            names = set(inner.namelist())
    except (zipfile.BadZipFile, OSError):
        return "UNKNOWN"
    if suffix == ".docx" and "word/document.xml" in names:
        return "DOCX"
    if suffix == ".xlsx" and "xl/workbook.xml" in names:
        return "XLSX"
    return "ZIP"


def inventory_archive(
    archive_path: Path,
    store: OnboardingStore,
    onboarding_id: str,
) -> list[InventoryEntry]:
    """Validate the archive, extract each file and return the inventory.

    The archive is read once. Each member is streamed to a generated filename while
    its SHA-256 is computed, so a file is never held in memory in full.
    """
    try:
        archive_size = archive_path.stat().st_size
    except OSError as exc:
        raise ArchiveRejected("The uploaded file could not be read.") from exc
    if archive_size == 0:
        raise ArchiveRejected("The uploaded file is empty.")
    if archive_size > MAX_ARCHIVE_BYTES:
        raise ArchiveRejected(
            f"The archive is larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB."
        )

    try:
        archive = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise ArchiveRejected("The uploaded file is not a readable ZIP archive.") from exc

    entries: list[InventoryEntry] = []
    extracted_bytes = 0
    try:
        members = [info for info in archive.infolist() if not info.is_dir() and not _skip(info.filename)]
        if not members:
            raise ArchiveRejected("The archive contains no documents.")
        if len(members) > MAX_FILES:
            raise ArchiveRejected(f"The archive contains more than {MAX_FILES} files.")

        # Check the whole archive's declared size before extracting anything.
        declared_total = sum(info.file_size for info in members)
        if declared_total > MAX_EXTRACTED_BYTES:
            raise ArchiveRejected("The archive expands to more data than this importer accepts.")
        if archive_size and declared_total / archive_size > MAX_COMPRESSION_RATIO:
            raise ArchiveRejected("The archive's compression ratio looks like a decompression bomb.")

        store.prepare(onboarding_id)

        for info in members:
            name = _safe_name(info.filename)
            if info.flag_bits & 0x1:
                raise ArchiveRejected(f"{name} is encrypted; remove the password and re-upload.")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ArchiveRejected(f"{name} uses an unsupported compression method.")
            if _is_symlink(info):
                raise ArchiveRejected(f"{name} is a symbolic link, which is not allowed.")
            if info.file_size > MAX_FILE_BYTES:
                raise ArchiveRejected(
                    f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB."
                )

            suffix = _suffix(name)
            unsupported = suffix not in SUPPORTED_SUFFIXES or suffix in BLOCKED_SUFFIXES
            internal, relative = store.document_relative(onboarding_id, suffix or ".bin")
            destination = store.resolve(relative)
            digest = hashlib.sha256()
            written = 0

            with archive.open(info) as source, destination.open("wb") as target:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    written += len(block)
                    extracted_bytes += len(block)
                    if written > MAX_FILE_BYTES:
                        raise ArchiveRejected(f"{name} expands past the per-file limit.")
                    if extracted_bytes > MAX_EXTRACTED_BYTES:
                        raise ArchiveRejected("The archive expands past the extracted-size limit.")
                    digest.update(block)
                    target.write(block)

            if written != info.file_size:
                raise ArchiveRejected(f"{name} was truncated or its size is misreported.")

            with destination.open("rb") as handle:
                head = handle.read(4096)
            file_format = "UNSUPPORTED" if unsupported else _format_from_magic(head, suffix)
            if file_format in {"DOCX", "XLSX"}:
                file_format = _sniff_zip_format(destination, suffix)
            if file_format == "ZIP":
                file_format = "UNSUPPORTED"

            mime = (
                "application/octet-stream"
                if unsupported
                else SUPPORTED_SUFFIXES.get(suffix, "application/octet-stream")
            )
            entries.append(
                InventoryEntry(
                    original_filename=name,
                    internal_filename=internal,
                    storage_path=relative,
                    sha256=digest.hexdigest(),
                    size_bytes=written,
                    mime_type=mime,
                    format=file_format,
                    unsupported=unsupported,
                )
            )
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as exc:
        raise ArchiveRejected(f"The archive could not be read: {exc}") from exc
    finally:
        archive.close()

    return entries
