"""A small durable worker for onboarding jobs.

The application is a single self-hosted service, so a thread is sufficient and adds
no infrastructure: the *durability* lives in the ``onboarding_jobs`` table, not in
the thread. If the process stops mid-archive, the pending rows are still there, and
the next process claims them. Each document is its own row, which is what makes a
single failure independent of the rest.

Disable with ``PROPTALLY_ONBOARDING_WORKER=0``; the test harness uses that when it
wants to drive :meth:`OnboardingWorker.drain` explicitly.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime

from sqlmodel import Session, select

from proptally.db.onboarding_models import (
    DOC_EXTRACTING,
    ONBOARDING_CANCELLED,
    ONBOARDING_COMPLETED,
    ONBOARDING_EXPIRED,
    OnboardingDocument,
    OnboardingJob,
    OnboardingSession,
)
from proptally.onboarding.pipeline import process_all
from proptally.onboarding.store import OnboardingStore

logger = logging.getLogger(__name__)


def recover_interrupted_jobs(control_engine) -> int:
    """Return work claimed by a previous process to the queue.

    A row in RUNNING at startup belongs to a worker that is gone. Its document may
    still say EXTRACTING; resetting both makes the retry visible and idempotent.
    Provisioning needs nothing special: its manifest already records what completed.
    """
    recovered = 0
    with Session(control_engine) as control:
        jobs = control.exec(
            select(OnboardingJob).where(OnboardingJob.status == "RUNNING")
        ).all()
        for job in jobs:
            job.status = "PENDING"
            job.started_at = None
            job.error = "Interrupted by a restart; queued again."
            control.add(job)
            recovered += 1
        documents = control.exec(
            select(OnboardingDocument).where(OnboardingDocument.status == DOC_EXTRACTING)
        ).all()
        for document in documents:
            document.status = "DISCOVERED"
            document.error = None
            control.add(document)
        control.commit()
    return recovered


def expire_stale_sessions(control_engine, store: OnboardingStore, now: datetime | None = None) -> int:
    """Apply the retention policy: abandon and delete expired staging sessions.

    A completed session is never expired here: its approved documents were copied
    into the portfolio vault and its audit rows still point at that copy. A session
    that never completed is temporary by definition, so its staged bytes go.
    """
    moment = now or datetime.now(UTC)
    expired = 0
    with Session(control_engine) as control:
        sessions = control.exec(
            select(OnboardingSession).where(
                OnboardingSession.expires_at.is_not(None),
                OnboardingSession.expires_at < moment,
                ~OnboardingSession.status.in_((
                    ONBOARDING_COMPLETED, ONBOARDING_CANCELLED, ONBOARDING_EXPIRED,
                )),
            )
        ).all()
        for onboarding in sessions:
            onboarding.status = ONBOARDING_EXPIRED
            onboarding.last_error = "The staged import expired after 30 days."
            onboarding.updated_at = moment
            control.add(onboarding)
            for job in control.exec(
                select(OnboardingJob).where(
                    OnboardingJob.onboarding_id == onboarding.id,
                    OnboardingJob.status == "PENDING",
                )
            ).all():
                job.status = "FAILED"
                job.error = "The staged import expired."
                control.add(job)
            expired += 1
        control.commit()
    for onboarding in sessions:
        store.remove_session(onboarding.id)
    return expired


def worker_enabled() -> bool:
    return os.environ.get("PROPTALLY_ONBOARDING_WORKER", "1").strip().lower() not in {
        "0", "false", "off", "no"
    }


class OnboardingWorker:
    def __init__(self, control_engine, tenant_manager, store: OnboardingStore) -> None:
        self.control_engine = control_engine
        self.tenant_manager = tenant_manager
        self.store = store
        self.interval = float(os.environ.get("PROPTALLY_ONBOARDING_POLL_SECONDS", "1.0"))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_expiry: datetime | None = None

    def drain(self, limit: int = 200) -> int:
        """Process pending work now. Used by the worker loop and by tests."""
        return process_all(self.control_engine, self.store, self.tenant_manager, limit=limit)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = datetime.now(UTC)
                if self._last_expiry is None or (now - self._last_expiry).total_seconds() > 3600:
                    expire_stale_sessions(self.control_engine, self.store, now)
                    self._last_expiry = now
                processed = self.drain()
            except Exception:
                logger.exception("Onboarding worker iteration failed")
                processed = 0
            if processed == 0:
                self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        recover_interrupted_jobs(self.control_engine)
        self._thread = threading.Thread(
            target=self._loop, name="proptally-onboarding", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5)
