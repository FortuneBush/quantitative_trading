"""Small, bounded in-process research backtest job manager.

The HTTP backtest endpoint is intentionally cache-only, but a large universe
can still take longer than a reverse proxy's request timeout.  This manager
provides an asynchronous facade without adding a database table: the result
artifact remains the durable source of truth and job state is explicitly
ephemeral to the serving process.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from quant_monitor.domain import now_market_time


class IdempotencyConflictError(ValueError):
    """An idempotency key was reused for a different request body."""


@dataclass(frozen=True)
class ResearchBacktestJob:
    job_id: str
    run_id: str
    status: str
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_hash: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    idempotency_digest: Optional[str] = None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "cancelled"}


class ResearchBacktestJobManager:
    """Bounded lifecycle manager for cache-only backtests.

    State is process-local by design.  A restart never replays a backtest;
    clients can inspect the persisted result endpoint once a job succeeded.
    """

    def __init__(self, *, max_active: int = 2, retention: int = 100) -> None:
        if int(max_active) < 1:
            raise ValueError("max_active must be positive")
        if int(retention) < 1:
            raise ValueError("retention must be positive")
        self.max_active = int(max_active)
        self.retention = int(retention)
        self._jobs: Dict[str, ResearchBacktestJob] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._idempotency: Dict[str, str] = {}
        # Fingerprint the request represented by an idempotency key.  A key
        # replay with changed parameters is a client error, not a replay of
        # the first job (otherwise a transient retry could silently run with
        # stale symbols/configuration).
        self._idempotency_requests: Dict[str, str] = {}
        # A cancellation is injected at most once per worker.  Keeping this
        # separate from the public job snapshot is important: a running job
        # remains ``cancelling`` until its coroutine has really unwound, so
        # capacity is not released while cleanup is still in progress.
        self._cancel_requested: set[str] = set()
        self._lock: Optional[asyncio.Lock] = None
        # A manager belongs to one application lifespan.  Once shutdown has
        # started, admission must be closed before cancelling worker tasks;
        # otherwise a concurrent request can enqueue work after SQLite is
        # already being torn down.
        self._stopped = False

    def _state_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def _digest(key: Optional[str], run_id: str) -> Optional[str]:
        if key is None:
            return None
        value = str(key).strip()
        if not value:
            raise ValueError("Idempotency-Key must not be empty")
        if len(value) > 255:
            raise ValueError("Idempotency-Key must be 255 characters or fewer")
        # Idempotency keys are scoped to one research run.  Reusing a client
        # key for a different run must never replay the first run's job.
        scoped = "{}\0{}".format(str(run_id), value)
        return hashlib.sha256(scoped.encode("utf-8")).hexdigest()

    @staticmethod
    def _payload(job: ResearchBacktestJob, *, replayed: bool = False) -> Dict[str, Any]:
        # Keep error details typed and concise; endpoint-level exception text
        # may contain local paths or provider implementation details.
        return {
            "job_id": job.job_id,
            "run_id": job.run_id,
            "status": job.status,
            "terminal": job.terminal,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "result_hash": job.result_hash,
            "result_available": job.status == "succeeded",
            "error_type": job.error_type,
            "error_message": job.error_message,
            "idempotency_replayed": bool(replayed),
        }

    @staticmethod
    def _safe_error_message(exc: BaseException) -> Optional[str]:
        """Return a bounded diagnostic that passes the public artifact policy.

        Keep useful validation messages, but omit arbitrary exception types
        and any text containing recognizable credentials or machine paths.
        Scanning the complete message before truncation also catches an
        embedded JSON credential or token beyond the public length bound.
        """

        detail = getattr(exc, "detail", None)
        if detail is None and isinstance(exc, (ValueError, TypeError, LookupError)):
            detail = str(exc)
        if not isinstance(detail, str):
            return None
        message = " ".join(detail.split())
        if not message:
            return None
        from quant_monitor.research.manifest import (
            CanonicalizationError, ManifestValidationError, canonical_json,
        )

        try:
            canonical_json(message)
        except (CanonicalizationError, ManifestValidationError):
            return None
        # Even a URL without a recognizable credential can carry arbitrary
        # account identifiers in its query; do not publish those diagnostics.
        message = re.sub(r"(?i)(?:https?|wss?)://[^\s]+", "[url redacted]", message)
        return message[:240] or None

    def _trim_locked(self) -> None:
        if len(self._jobs) <= self.retention:
            return
        terminal = [job for job in self._jobs.values() if job.terminal]
        terminal.sort(key=lambda job: job.created_at)
        for job in terminal[: max(0, len(self._jobs) - self.retention)]:
            self._jobs.pop(job.job_id, None)
            if job.idempotency_digest:
                self._idempotency.pop(job.idempotency_digest, None)
                self._idempotency_requests.pop(job.idempotency_digest, None)

    def _task_done(self, job_id: str, task: asyncio.Task) -> None:
        """Drop task bookkeeping even when cancellation prevents startup.

        ``Task.cancel()`` called before the coroutine gets its first turn
        means the coroutine body (and therefore its ``finally`` block) never
        runs.  A done callback is the only reliable cleanup hook for that
        case.  The identity check prevents a late callback from deleting a
        replacement task for the same id (which should not normally exist,
        but is cheap to make safe).
        """
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id, None)
        self._cancel_requested.discard(job_id)

    async def submit(
        self,
        run_id: str,
        execute: Callable[[], Awaitable[Dict[str, Any]]],
        *,
        idempotency_key: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_run_id = str(run_id).strip()
        if not normalized_run_id:
            raise ValueError("run_id must not be empty")
        digest = self._digest(idempotency_key, normalized_run_id)
        async with self._state_lock():
            if self._stopped:
                raise RuntimeError("research backtest job manager is stopped")
            if digest is not None:
                existing_id = self._idempotency.get(digest)
                if existing_id and existing_id in self._jobs:
                    previous = self._idempotency_requests.get(digest)
                    if (
                        request_fingerprint is not None
                        and previous is not None
                        and str(request_fingerprint) != previous
                    ):
                        raise IdempotencyConflictError(
                            "Idempotency-Key was already used with different request parameters"
                        )
                    return self._payload(self._jobs[existing_id], replayed=True)
            active = sum(not job.terminal for job in self._jobs.values())
            if active >= self.max_active:
                raise RuntimeError("research backtest job capacity is full")
            job = ResearchBacktestJob(
                job_id=uuid.uuid4().hex,
                run_id=normalized_run_id,
                status="queued",
                created_at=now_market_time(),
                idempotency_digest=digest,
            )
            self._jobs[job.job_id] = job
            if digest is not None:
                self._idempotency[digest] = job.job_id
                if request_fingerprint is not None:
                    self._idempotency_requests[digest] = str(request_fingerprint)
            self._trim_locked()
            task = asyncio.create_task(self._execute(job.job_id, execute), name="research-backtest-{}".format(job.job_id[:8]))
            self._tasks[job.job_id] = task
            task.add_done_callback(
                lambda completed, job_id=job.job_id: self._task_done(job_id, completed)
            )
            return self._payload(job)

    async def _execute(self, job_id: str, execute: Callable[[], Awaitable[Dict[str, Any]]]) -> None:
        async with self._state_lock():
            job = self._jobs.get(job_id)
            if job is None or job.terminal:
                return
            self._jobs[job_id] = replace(job, status="running", started_at=now_market_time())
        try:
            result = await execute()
            result_hash = None
            if isinstance(result, dict):
                result_hash = result.get("result_hash")
            async with self._state_lock():
                job = self._jobs.get(job_id)
                if job is not None and not job.terminal:
                    self._jobs[job_id] = replace(
                        job,
                        status="succeeded",
                        completed_at=now_market_time(),
                        result_hash=str(result_hash) if result_hash else None,
                        error_type=None,
                        error_message=None,
                    )
        except asyncio.CancelledError:
            async with self._state_lock():
                job = self._jobs.get(job_id)
                if job is not None and not job.terminal:
                    self._jobs[job_id] = replace(
                        job,
                        status="cancelled",
                        completed_at=now_market_time(),
                        # Preserve ProcessShutdown when stop() initiated the
                        # cancellation; ordinary user cancellation is typed
                        # as Cancelled.  A later successful commit is allowed
                        # to clear this provisional error on success.
                        error_type=job.error_type or "Cancelled",
                    )
            raise
        except Exception as exc:  # noqa: BLE001 - lifecycle records typed failures
            async with self._state_lock():
                job = self._jobs.get(job_id)
                if job is not None and not job.terminal:
                    self._jobs[job_id] = replace(
                        job,
                        status="failed",
                        completed_at=now_market_time(),
                        error_type=type(exc).__name__,
                        error_message=self._safe_error_message(exc),
                    )
        finally:
            # Terminal snapshots remain queryable, but completed asyncio.Task
            # objects must not accumulate beyond the bounded job ledger.
            self._tasks.pop(job_id, None)

    async def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        async with self._state_lock():
            job = self._jobs.get(str(job_id).strip())
            return self._payload(job) if job is not None else None

    async def list(self, limit: int = 20, *, run_id: Optional[str] = None) -> Dict[str, Any]:
        bounded = max(1, min(int(limit), self.retention))
        async with self._state_lock():
            selected = [
                job for job in self._jobs.values()
                if run_id is None or job.run_id == str(run_id)
            ]
            jobs = sorted(selected, key=lambda item: item.created_at, reverse=True)[:bounded]
            statuses = {status: 0 for status in ("queued", "running", "cancelling", "succeeded", "failed", "cancelled")}
            for job in selected:
                statuses[job.status] = statuses.get(job.status, 0) + 1
            return {
                "items": [self._payload(job) for job in jobs],
                "count": len(jobs),
                "total": len(selected),
                "active": sum(not job.terminal for job in selected),
                "by_status": statuses,
                "limit": bounded,
            }

    async def cancel(self, job_id: str) -> Optional[Dict[str, Any]]:
        async with self._state_lock():
            job = self._jobs.get(str(job_id).strip())
            task = self._tasks.get(str(job_id).strip())
            if job is None:
                return None
            if job.terminal:
                return self._payload(job)
            if job.status == "queued":
                # The worker has not entered _execute yet.  Finalize the
                # snapshot immediately, then cancel the not-yet-started task;
                # its done callback removes _tasks because _execute's finally
                # block may never run.
                job = replace(
                    job,
                    status="cancelled",
                    completed_at=now_market_time(),
                    error_type="Cancelled",
                )
                self._jobs[job.job_id] = job
                if task is not None and not task.done():
                    self._cancel_requested.add(job.job_id)
                    task.cancel()
                return self._payload(job)

            # Running work stays non-terminal until its coroutine has really
            # returned or raised.  This keeps capacity reserved during async
            # cleanup and makes repeated cancellation idempotent.
            if job.status == "running":
                job = replace(job, status="cancelling", error_type="Cancelled")
                self._jobs[job.job_id] = job
            if (
                task is not None
                and not task.done()
                and job.job_id not in self._cancel_requested
            ):
                self._cancel_requested.add(job.job_id)
                task.cancel()
            return self._payload(job)

    async def stop(self) -> None:
        async with self._state_lock():
            self._stopped = True
            tasks = list(self._tasks.values())
            now = now_market_time()
            for job_id, job in tuple(self._jobs.items()):
                if not job.terminal:
                    task = self._tasks.get(job_id)
                    if job.status == "queued":
                        # A queued task cannot have started user work, so it
                        # can be finalized immediately.
                        self._jobs[job_id] = replace(
                            job,
                            status="cancelled",
                            completed_at=now,
                            error_type="ProcessShutdown",
                        )
                    elif job.status == "running":
                        # Keep this non-terminal until the actual coroutine
                        # exits; stop() below waits for that exit.
                        self._jobs[job_id] = replace(
                            job, status="cancelling", error_type="ProcessShutdown"
                        )
                    # A prior user cancellation may already be in flight.
                    # Do not inject a second CancelledError; only update the
                    # reason to ProcessShutdown above.
                    if task is not None and not task.done():
                        if job_id not in self._cancel_requested:
                            self._cancel_requested.add(job_id)
                            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Normal completion/done callbacks remove entries.  Keep this final
        # defensive cleanup for callbacks that are scheduled after gather;
        # never clear a task that is still genuinely running.
        for task in tasks:
            if task.done():
                for job_id, known in tuple(self._tasks.items()):
                    if known is task:
                        self._tasks.pop(job_id, None)
