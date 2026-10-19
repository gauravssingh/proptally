"""Provider boundaries for text recognition and structured extraction.

The onboarding workflow never names a vendor. Two protocols are enough to swap a
local OCR engine or a different extraction model without touching the pipeline:

* :class:`OcrProvider` turns an image into text;
* :class:`StructuredExtractor` turns a document's text into typed fields.

The defaults are intentionally local and offline. Nothing is sent anywhere unless
the operator configures a provider explicitly; the instance's own promise is that
financial documents do not leave the host.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ProviderError(RuntimeError):
    """A provider was configured but could not complete the request."""


class OcrProvider(Protocol):
    name: str

    def extract(self, image_path: Path) -> str:
        """Return the text visible in an image."""


@dataclass
class TesseractOcr:
    """Local OCR through the ``tesseract`` binary.

    No Python package is required, and no bytes leave the machine. It is used only
    when the binary exists (or ``PROPTALLY_TESSERACT`` points at one).
    """

    name: str = "tesseract"

    def _binary(self) -> str | None:
        configured = os.environ.get("PROPTALLY_TESSERACT", "").strip()
        if configured:
            return configured if Path(configured).exists() else None
        return shutil.which("tesseract")

    def available(self) -> bool:
        return self._binary() is not None

    def extract(self, image_path: Path) -> str:
        binary = self._binary()
        if binary is None:
            raise ProviderError("Tesseract is not installed on this host.")
        try:
            completed = subprocess.run(
                [binary, str(image_path), "stdout"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f"OCR failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
            raise ProviderError(f"OCR failed: {detail[0]}")
        return completed.stdout or ""


def ocr_provider() -> OcrProvider | None:
    """The configured OCR provider, or None when OCR is not available.

    ``PROPTALLY_OCR_PROVIDER=none`` disables OCR entirely; ``auto`` (the default)
    uses Tesseract when it is installed. A configured provider that is unavailable
    is reported as None so callers can explain the limitation rather than silently
    returning an empty document.
    """
    choice = os.environ.get("PROPTALLY_OCR_PROVIDER", "auto").strip().lower()
    if choice in {"", "none", "off", "disabled"}:
        return None
    provider = TesseractOcr()
    if provider.available():
        return provider
    return None


class StructuredExtractor(Protocol):
    """Turns text into typed fields. Implementations must not invent values."""

    name: str

    def extract(self, *, category: str, filename: str, pages: list[str]) -> object:
        """Return an object with ``fields`` mapping field names to extracted values."""


@dataclass
class HttpJsonExtractor:
    """Optional OpenAI-compatible JSON endpoint, for a local inference server.

    This exists so an operator who runs a local model can use it without changing
    the pipeline. It is disabled unless ``PROPTALLY_EXTRACTION_URL`` is set, and the
    URL is required to be loopback or a private address so a misconfiguration cannot
    quietly ship a customer's deeds to a public API.
    """

    name: str = "http-json"
    url: str = ""
    model: str = ""

    def extract(self, *, category: str, filename: str, pages: list[str]) -> object:
        import httpx

        from proptally.onboarding.extraction import (
            ExtractionResult,
            RuleBasedExtractor,
            _fields_from_json,
        )

        if not self.url:
            raise ProviderError("No extraction endpoint is configured.")
        prompt = (
            "Extract only facts explicitly present in the document. Return JSON with a "
            "single object 'fields'. Omit fields that are not present; never guess. "
            f"Document category: {category}. Filename: {filename}.\n\n"
            + "\n".join(pages)
        )
        try:
            response = httpx.post(
                self.url.rstrip("/") + "/chat/completions",
                json={
                    "model": self.model or "local",
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "You extract structured facts from documents."},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=120,
                # A local endpoint must not be able to bounce the document
                # payload to an external host with a 3xx response.
                follow_redirects=False,
            )
            if response.is_redirect:
                raise ProviderError("Extraction provider attempted a redirect; refusing it.")
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            payload = json.loads(content)
        except Exception as exc:
            raise ProviderError(f"Extraction provider failed: {exc}") from exc

        # A provider result has no page provenance unless it supplies one. The rule
        # extractor is used to locate the supporting line for each returned value;
        # values it cannot locate are kept with page 0 and marked as provider-only.
        fields = _fields_from_json(payload.get("fields", payload))
        fallback = RuleBasedExtractor()
        rule_result = fallback.extract(category=category, filename=filename, pages=pages)
        merged = {}
        for name, value in fields.items():
            reference = rule_result.fields.get(name)
            if reference is not None and reference.value == value:
                merged[name] = reference
            else:
                from proptally.onboarding.extraction import ExtractedField

                merged[name] = ExtractedField(
                    name=name, value=value, page=0, evidence="", method="PROVIDER", confidence=0.6
                )
        return ExtractionResult(
            category=category, fields=merged, pages=pages, method="provider:http-json"
        )


def structured_extractor() -> StructuredExtractor:
    """The extractor the pipeline should use.

    Default is deterministic rules on this host. ``PROPTALLY_EXTRACTION_URL`` opts
    into a local model. The URL is validated before use so a typo cannot send a
    customer's financial records to an arbitrary public host.
    """
    from urllib.parse import urlparse

    from proptally.onboarding.extraction import RuleBasedExtractor

    url = os.environ.get("PROPTALLY_EXTRACTION_URL", "").strip()
    if not url:
        return RuleBasedExtractor()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return RuleBasedExtractor()
    host = parsed.hostname or ""

    def _local_endpoint(value: str) -> bool:
        """Every resolved address must be loopback or RFC 1918/4193 private.

        Resolving the hostname closes the old string-prefix hole (``172.2``
        admitted all of 172.200.0.0/15) and checks every A/AAAA answer, not just
        the first.
        """
        if not value:
            return False
        try:
            infos = socket.getaddrinfo(value, None)
        except OSError:
            return False
        if not infos:
            return False
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if address.is_loopback:
                continue
            if not (
                address.is_private
                and not address.is_unspecified
                and not address.is_multicast
            ):
                return False
        return True

    if not _local_endpoint(host):
        # A public endpoint needs an explicit, separate opt-in. Refusing here keeps
        # the default data flow local even if the variable is set by mistake.
        if os.environ.get("PROPTALLY_ALLOW_REMOTE_EXTRACTION", "").lower() not in {"1", "true", "yes"}:
            return RuleBasedExtractor()
    return HttpJsonExtractor(
        url=url, model=os.environ.get("PROPTALLY_EXTRACTION_MODEL", "")
    )


def image_to_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")
