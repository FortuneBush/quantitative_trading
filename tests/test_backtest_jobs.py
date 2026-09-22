"""Offline lifecycle tests for asynchronous research backtest jobs."""

from __future__ import annotations

import asyncio

import pytest

from quant_monitor.backtesting.jobs import ResearchBacktestJobManager


@pytest.mark.asyncio
async def test_idempotency_coalesces_and_success_is_observable():
    manager = ResearchBacktestJobManager(max_active=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute():
        started.set()
        await release.wait()
        return {"result_hash": "sha256:fixture"}

    first = await manager.submit("run-a", execute, idempotency_key="retry-1")
    replay = await manager.submit("run-a", execute, idempotency_key="retry-1")
    assert replay["job_id"] == first["job_id"]
    assert replay["idempotency_replayed"] is True
    await asyncio.wait_for(started.wait(), timeout=1)
    assert (await manager.get(first["job_id"]))["status"] == "running"
    release.set()
    for _ in range(20):
        snapshot = await manager.get(first["job_id"])
        if snapshot["terminal"]:
            break
        await asyncio.sleep(0)
    assert snapshot["status"] == "succeeded"
    assert snapshot["result_hash"] == "sha256:fixture"
    assert (await manager.list(run_id="run-a"))["total"] == 1
    await manager.stop()


@pytest.mark.asyncio
async def test_capacity_and_cancellation_are_bounded():
    manager = ResearchBacktestJobManager(max_active=1)
    release = asyncio.Event()

    async def execute():
        await release.wait()
        return {}

    first = await manager.submit("run-a", execute)
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="capacity"):
        await manager.submit("run-b", execute)
    cancelled = await manager.cancel(first["job_id"])
    assert cancelled["status"] == "cancelling"
    assert cancelled["terminal"] is False
    for _ in range(20):
        snapshot = await manager.get(first["job_id"])
        if snapshot["terminal"]:
            break
        await asyncio.sleep(0)
    assert snapshot["status"] == "cancelled"
    await manager.stop()


@pytest.mark.asyncio
async def test_running_cancel_is_nonterminal_and_repeated_cancel_is_idempotent():
    manager = ResearchBacktestJobManager(max_active=1)
    started = asyncio.Event()
    cleanup = asyncio.Event()
    cleanup_started = asyncio.Event()
    cancel_count = 0

    async def execute():
        nonlocal cancel_count
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_count += 1
            cleanup_started.set()
            await cleanup.wait()
            raise

    first = await manager.submit("run-cancel", execute)
    await asyncio.wait_for(started.wait(), timeout=1)
    requested = await manager.cancel(first["job_id"])
    assert requested["status"] == "cancelling"
    assert requested["terminal"] is False
    # Cleanup is still consuming the active slot.
    with pytest.raises(RuntimeError, match="capacity"):
        await manager.submit("run-blocked", execute)
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    repeated = await manager.cancel(first["job_id"])
    assert repeated["status"] == "cancelling"
    assert cancel_count == 1
    cleanup.set()
    for _ in range(50):
        snapshot = await manager.get(first["job_id"])
        if snapshot["terminal"]:
            break
        await asyncio.sleep(0)
    assert snapshot["status"] == "cancelled"
    assert snapshot["error_type"] == "Cancelled"
    await manager.stop()


@pytest.mark.asyncio
async def test_cancelled_execute_may_ignore_cancel_and_commit_success():
    manager = ResearchBacktestJobManager()
    started = asyncio.Event()

    async def execute():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A worker can finish a non-cancellable commit/cleanup section
            # after receiving cancellation.  Its committed result wins.
            return {"result_hash": "sha256:committed-after-cancel"}

    job = await manager.submit("run-commit", execute)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert (await manager.cancel(job["job_id"]))["status"] == "cancelling"
    for _ in range(50):
        snapshot = await manager.get(job["job_id"])
        if snapshot["terminal"]:
            break
        await asyncio.sleep(0)
    assert snapshot["status"] == "succeeded"
    assert snapshot["error_type"] is None
    assert snapshot["result_hash"] == "sha256:committed-after-cancel"
    await manager.stop()


@pytest.mark.asyncio
async def test_queued_cancel_cleans_task_bookkeeping_before_worker_starts():
    manager = ResearchBacktestJobManager()
    execute_called = False

    async def execute():
        nonlocal execute_called
        execute_called = True
        return {}

    job = await manager.submit("run-queued-cancel", execute)
    cancelled = await manager.cancel(job["job_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["terminal"] is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert execute_called is False
    assert job["job_id"] not in manager._tasks
    await manager.stop()


@pytest.mark.asyncio
async def test_stop_marks_cancelling_and_waits_for_real_cleanup():
    manager = ResearchBacktestJobManager()
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def execute():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await cleanup_release.wait()
            raise

    job = await manager.submit("run-stop-cleanup", execute)
    await asyncio.wait_for(started.wait(), timeout=1)
    stopping = asyncio.create_task(manager.stop())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    during = await manager.get(job["job_id"])
    assert during["status"] == "cancelling"
    assert during["terminal"] is False
    assert during["error_type"] == "ProcessShutdown"
    assert stopping.done() is False
    cleanup_release.set()
    await asyncio.wait_for(stopping, timeout=1)
    final = await manager.get(job["job_id"])
    assert final["status"] == "cancelled"
    assert final["error_type"] == "ProcessShutdown"


@pytest.mark.asyncio
async def test_idempotency_key_is_scoped_to_research_run():
    manager = ResearchBacktestJobManager()
    gate = asyncio.Event()

    async def execute():
        await gate.wait()
        return {}

    first = await manager.submit("run-a", execute, idempotency_key="same-key")
    second = await manager.submit("run-b", execute, idempotency_key="same-key")
    assert second["job_id"] != first["job_id"]
    gate.set()
    await manager.stop()


@pytest.mark.asyncio
async def test_idempotency_key_rejects_changed_request_fingerprint():
    manager = ResearchBacktestJobManager()
    gate = asyncio.Event()

    async def execute():
        await gate.wait()
        return {}

    first = await manager.submit(
        "run-a", execute, idempotency_key="same-key", request_fingerprint="body-v1"
    )
    with pytest.raises(ValueError, match="different request parameters"):
        await manager.submit(
            "run-a", execute, idempotency_key="same-key", request_fingerprint="body-v2"
        )
    gate.set()
    await manager.stop()


@pytest.mark.asyncio
async def test_failures_are_typed_and_message_is_bounded():
    manager = ResearchBacktestJobManager()

    async def execute():
        raise ValueError("bad cached bars")

    job = await manager.submit("run-fail", execute)
    for _ in range(20):
        snapshot = await manager.get(job["job_id"])
        if snapshot["terminal"]:
            break
        await asyncio.sleep(0)
    assert snapshot["status"] == "failed"
    assert snapshot["error_type"] == "ValueError"
    assert snapshot["error_message"] == "bad cached bars"
    await manager.stop()


@pytest.mark.asyncio
async def test_stop_closes_admission_and_cancels_queued_work():
    manager = ResearchBacktestJobManager()
    gate = asyncio.Event()

    async def execute():
        await gate.wait()
        return {"result_hash": "never"}

    first = await manager.submit("run-stop", execute)
    await manager.stop()
    snapshot = await manager.get(first["job_id"])
    assert snapshot["status"] == "cancelled"
    assert snapshot["error_type"] == "ProcessShutdown"
    with pytest.raises(RuntimeError, match="stopped"):
        await manager.submit("run-after-stop", execute)
    # stop() is intentionally idempotent for lifespan error paths.
    await manager.stop()


@pytest.mark.asyncio
async def test_untrusted_exception_details_are_redacted_or_omitted():
    manager = ResearchBacktestJobManager()

    async def execute():
        raise RuntimeError(
            "GET https://quote.example.test?token=secret failed at C:\\private\\cache\\bars.json"
        )

    job = await manager.submit("run-secret", execute)
    for _ in range(20):
        snapshot = await manager.get(job["job_id"])
        if snapshot["terminal"]:
            break
        await asyncio.sleep(0)
    assert snapshot["status"] == "failed"
    assert snapshot["error_type"] == "RuntimeError"
    assert snapshot["error_message"] is None
    await manager.stop()


@pytest.mark.parametrize("message", [
    "upstream echoed " + "ash_" + "x" * 48,
    "access\u200bToken: private-value",
    '{"debug":{"client_secret":"private-value"}}',
    "file%3A%2F%2F%2FC%3A%2FUsers%2Fprivate-user%2Fcache.json",
])
def test_validation_exception_text_cannot_bypass_public_secret_policy(message):
    assert ResearchBacktestJobManager._safe_error_message(ValueError(message)) is None


def test_job_validation_message_preserves_safe_diagnostic():
    message = "cached 1d bars are required for research"
    assert ResearchBacktestJobManager._safe_error_message(ValueError(message)) == message


def test_job_error_does_not_publish_arbitrary_url_query_fields():
    message = "request https://vendor.example/data?account=private-value failed"
    assert ResearchBacktestJobManager._safe_error_message(ValueError(message)) == (
        "request [url redacted] failed"
    )
