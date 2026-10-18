"""Filesystem layout for staged onboarding data.

Everything lives under ``<storage root>/onboarding/<session id>/``, outside the
portfolio vaults and outside the web root. Database rows keep paths *relative* to
the onboarding root; :meth:`OnboardingStore.resolve` is the only way back to a
filesystem path, and it refuses anything that escapes the root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

#: A session directory holds exactly these subdirectories.
ARCHIVE_DIR = "archive"
DOCUMENT_DIR = "documents"


@dataclass(frozen=True)
class OnboardingPaths:
    root: Path
    archive: Path
    documents: Path


class OnboardingStore:
    """Resolves and creates the staging directories for one installation."""

    def __init__(self, storage_root: Path) -> None:
        self.root = (Path(storage_root) / "onboarding").resolve()

    def paths(self, onboarding_id: str) -> OnboardingPaths:
        base = (self.root / onboarding_id).resolve()
        if self.root not in base.parents:
            raise ValueError("Invalid onboarding session id.")
        return OnboardingPaths(
            root=base,
            archive=base / ARCHIVE_DIR,
            documents=base / DOCUMENT_DIR,
        )

    def prepare(self, onboarding_id: str) -> OnboardingPaths:
        """Create the session's directories, idempotently."""
        paths = self.paths(onboarding_id)
        paths.archive.mkdir(parents=True, exist_ok=True)
        paths.documents.mkdir(parents=True, exist_ok=True)
        return paths

    def archive_relative(self, onboarding_id: str, upload_id: str) -> str:
        return f"{onboarding_id}/{ARCHIVE_DIR}/{upload_id}.zip"

    def document_relative(self, onboarding_id: str, suffix: str) -> tuple[str, str]:
        """A generated on-disk name and the relative path that refers to it.

        The name is generated here, never taken from the archive, so an uploaded
        filename cannot influence where a byte is written.
        """
        safe_suffix = suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
        internal = f"doc_{uuid4().hex}{safe_suffix}"
        return internal, f"{onboarding_id}/{DOCUMENT_DIR}/{internal}"

    def resolve(self, relative: str) -> Path:
        """The path a stored relative name refers to, or ``ValueError``.

        Both the lexical path and its resolved form are checked: a symlink inside
        the store must not become a route out of it.
        """
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Stored path escapes the onboarding store.")
        return candidate

    def remove_session(self, onboarding_id: str) -> None:
        """Delete a cancelled session's staged bytes.

        Database rows are retained for the audit trail; the documents themselves
        are not. Callers must have checked ownership and state first.
        """
        import shutil

        paths = self.paths(onboarding_id)
        if paths.root.exists():
            shutil.rmtree(paths.root)
