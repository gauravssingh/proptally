"""Lightweight, standalone Python client for Document Intelligence Lab API.

Zero internal dependencies other than httpx.
Copy this file directly into any downstream application (e.g. proptally-mt).

Usage Example:
--------------
from doc_intel_client import DocumentIntelligenceClient

client = DocumentIntelligenceClient(base_url="http://localhost:8765")

# 1. Health & profile check
print("Supported profiles:", client.list_profiles())

# 2. Async extraction (with progress callback)
job = client.extract_file(
    file="path/to/bank_statement.pdf",
    profile_name="bank_statement",
    wait=True,
    on_progress=lambda msg: print(f"Progress: {msg}")
)

if job.is_success:
    result = job.result
    print("Document ID:", result["document_id"])
    print("Cross-field valid:", result["cross_field_valid"])
    
    # PropTally Prisma models
    pt_payload = result["proptally_payload"]
    statement = pt_payload["statement"]
    print("Status:", statement["ingestion_status"])  # 'VALIDATED_READY'
    print("Period:", statement["period_start_date"], "to", statement["period_end_date"])
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

import httpx


@dataclass
class DocIntelJob:
    """Representation of an asynchronous extraction job."""

    job_id: str
    status: str  # queued, processing, completed, failed, cancelled
    profile_name: str
    filename: Optional[str] = None
    document_id: Optional[str] = None
    progress_message: Optional[str] = None
    error_message: Optional[str] = None
    duration_seconds: Optional[float] = None
    result: Optional[dict[str, Any]] = None

    @property
    def is_done(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    @property
    def is_success(self) -> bool:
        return self.status == "completed"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocIntelJob:
        return cls(
            job_id=data.get("job_id", ""),
            status=data.get("status", "unknown"),
            profile_name=data.get("profile_name", ""),
            filename=data.get("filename"),
            document_id=data.get("document_id"),
            progress_message=data.get("progress_message"),
            error_message=data.get("error_message"),
            duration_seconds=data.get("duration_seconds"),
            result=data.get("result"),
        )


class DocumentIntelligenceClient:
    """Reusable client for communicating with Document Intelligence Lab service."""

    def __init__(
        self,
        base_url: str = "http://localhost:8765",
        timeout: float = 180.0,
        http_client: Optional[httpx.Client] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = http_client or httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DocumentIntelligenceClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def health(self, timeout: Optional[float] = None) -> dict[str, Any]:
        """Check if the service is healthy.

        ``timeout`` overrides the client's default for this one call, so a probe
        can fail fast instead of waiting out a 180-second inference budget.
        """
        resp = self._client.get("/health", timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def list_profiles(self) -> list[str]:
        """List all available extraction profiles."""
        resp = self._client.get("/api/v1/profiles")
        resp.raise_for_status()
        return list(resp.json().get("profiles", {}).keys())

    def profile_details(self) -> dict[str, Any]:
        """Every profile with its display name, version and field schema."""
        resp = self._client.get("/api/v1/profiles")
        resp.raise_for_status()
        return dict(resp.json().get("profiles", {}))

    def profile_detail(self, profile_id: str) -> dict[str, Any]:
        """One profile: full field schema, and the YAML it was loaded from."""
        resp = self._client.get(f"/api/v1/profiles/{profile_id}")
        resp.raise_for_status()
        return dict(resp.json())

    def submit_extraction(
        self,
        file: Union[str, Path, bytes],
        filename: Optional[str] = None,
        profile_name: str = "bank_statement",
        provider: str = "local",
        model_id: Optional[str] = None,
        strategy: str = "text_only",
        property_id: str = "",
        map_to_proptally: bool = True,
    ) -> DocIntelJob:
        """Submit a document extraction job asynchronously. Returns immediately with JobInfo."""
        file_bytes: bytes
        orig_filename: str

        if isinstance(file, (str, Path)):
            p = Path(file)
            if not p.exists():
                raise FileNotFoundError(f"File not found: {p}")
            orig_filename = filename or p.name
            file_bytes = p.read_bytes()
        elif isinstance(file, bytes):
            orig_filename = filename or "document.pdf"
            file_bytes = file
        else:
            raise TypeError("file must be a filepath (str/Path) or raw bytes")

        files = {"file": (orig_filename, file_bytes, "application/pdf")}
        data: dict[str, Any] = {
            "profile_name": profile_name,
            "provider": provider,
            "strategy": strategy,
            "property_id": property_id or "",
            "map_to_proptally": "true" if map_to_proptally else "false",
        }
        if model_id:
            data["model_id"] = model_id

        resp = self._client.post("/api/v1/jobs/extract/upload", files=files, data=data)
        resp.raise_for_status()
        return DocIntelJob.from_dict(resp.json())

    def get_job(self, job_id: str) -> DocIntelJob:
        """Fetch the current execution status and result of a job."""
        resp = self._client.get(f"/api/v1/jobs/{job_id}")
        resp.raise_for_status()
        return DocIntelJob.from_dict(resp.json())

    def list_jobs(self, limit: int = 50) -> list[DocIntelJob]:
        """List historical jobs."""
        resp = self._client.get("/api/v1/jobs", params={"limit": limit})
        resp.raise_for_status()
        return [DocIntelJob.from_dict(j) for j in resp.json()]

    def wait_for_job(
        self,
        job_id: str,
        timeout: Optional[float] = None,
        poll_interval: float = 2.0,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> DocIntelJob:
        """Poll job until completion or timeout. Calls on_progress callback if provided."""
        max_time = timeout or self.timeout
        t0 = time.time()
        last_msg = ""

        while True:
            job = self.get_job(job_id)

            if job.progress_message and job.progress_message != last_msg:
                last_msg = job.progress_message
                if on_progress:
                    on_progress(last_msg)

            if job.is_done:
                return job

            if time.time() - t0 > max_time:
                raise TimeoutError(f"Job {job_id} did not complete within {max_time} seconds (status: {job.status}).")

            time.sleep(poll_interval)

    def extract_file(
        self,
        file: Union[str, Path, bytes],
        filename: Optional[str] = None,
        profile_name: str = "bank_statement",
        provider: str = "local",
        model_id: Optional[str] = None,
        strategy: str = "text_only",
        property_id: str = "",
        map_to_proptally: bool = True,
        wait: bool = True,
        timeout: Optional[float] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> DocIntelJob:
        """Convenience method: Submits extraction job and optionally waits for full completion."""
        job = self.submit_extraction(
            file=file,
            filename=filename,
            profile_name=profile_name,
            provider=provider,
            model_id=model_id,
            strategy=strategy,
            property_id=property_id,
            map_to_proptally=map_to_proptally,
        )
        if not wait:
            return job
        return self.wait_for_job(job.job_id, timeout=timeout, on_progress=on_progress)

    def extract_sync(
        self,
        file: Union[str, Path, bytes],
        filename: Optional[str] = None,
        profile_name: str = "bank_statement",
        provider: str = "local",
        model_id: Optional[str] = None,
        strategy: str = "text_only",
        property_id: str = "",
        map_to_proptally: bool = True,
    ) -> dict[str, Any]:
        """Direct synchronous extraction (blocks until inference completes)."""
        file_bytes: bytes
        orig_filename: str

        if isinstance(file, (str, Path)):
            p = Path(file)
            orig_filename = filename or p.name
            file_bytes = p.read_bytes()
        else:
            orig_filename = filename or "document.pdf"
            file_bytes = file

        files = {"file": (orig_filename, file_bytes, "application/pdf")}
        data: dict[str, Any] = {
            "profile_name": profile_name,
            "provider": provider,
            "strategy": strategy,
            "property_id": property_id or "",
            "map_to_proptally": "true" if map_to_proptally else "false",
        }
        if model_id:
            data["model_id"] = model_id

        resp = self._client.post("/api/v1/extract/upload", files=files, data=data)
        resp.raise_for_status()
        return resp.json()

    def export_to_proptally(
        self,
        profile_name: str,
        document_id: str,
        normalized_fields: dict[str, Any],
        property_id: str = "",
        cross_field_valid: bool = True,
        auto_commit: bool = False,
    ) -> dict[str, Any]:
        """Convert normalized extraction dictionary into PropTally Prisma models."""
        payload = {
            "profile_name": profile_name,
            "document_id": document_id,
            "normalized_fields": normalized_fields,
            "property_id": property_id,
            "cross_field_valid": cross_field_valid,
            "auto_commit": auto_commit,
        }
        resp = self._client.post("/api/v1/proptally/export", json=payload)
        resp.raise_for_status()
        return resp.json()
