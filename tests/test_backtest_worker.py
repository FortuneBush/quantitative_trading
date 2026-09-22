"""Real worker-thread races using only bounded offline synchronization."""

import asyncio
import threading

import pytest

from quant_monitor.backtesting.worker import run_cancellable_worker


@pytest.mark.parametrize("cooperative", [False, True])
async def test_cancel_joins_computation_and_never_commits(cooperative):
    started, release, finished = (threading.Event() for _ in range(3))
    commits = []

    def compute(checkpoint):
        started.set()
        try:
            assert release.wait(5), "test did not release computation"
            if cooperative:
                checkpoint()
            return "would-have-been-result"
        finally:
            finished.set()

    task = asyncio.create_task(run_cancellable_worker(compute, commits.append))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        # A repeated cancel may not detach the native worker either.
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert finished.is_set()
    assert commits == []


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_commit_boundary_returns_actual_outcome_despite_cancellation(commit_fails):
    started, release, finished = (threading.Event() for _ in range(3))
    commits = []

    def commit(value):
        started.set()
        try:
            assert release.wait(5), "test did not release commit"
            if commit_fails:
                raise ValueError("persistence failed")
            commits.append(value)
            return {"result_hash": "durable-result"}
        finally:
            finished.set()

    task = asyncio.create_task(run_cancellable_worker(lambda check: "input", commit))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        if commit_fails:
            with pytest.raises(ValueError, match="persistence failed"):
                await asyncio.wait_for(task, 2)
        else:
            assert await asyncio.wait_for(task, 2) == {"result_hash": "durable-result"}
    assert finished.is_set()
    assert commits == ([] if commit_fails else ["input"])


async def test_compute_error_is_observed_without_commit():
    commits = []

    def compute(checkpoint):
        raise ValueError("invalid input")

    with pytest.raises(ValueError, match="invalid input"):
        await run_cancellable_worker(compute, commits.append)
    assert commits == []
