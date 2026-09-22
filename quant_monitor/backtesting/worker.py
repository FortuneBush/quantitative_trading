"""Cancellation-safe bridge between async research jobs and local workers.

Cancelling ``asyncio.to_thread`` does not stop its operating-system thread.
Keep ownership of that thread until it returns, and keep computation separate
from the irreversible result commit. No provider or database is selected here.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Callable, TypeVar


T = TypeVar("T")
R = TypeVar("R")


class ResearchWorkCancelled(Exception):
    """Cooperative checkpoint stopped a worker before result persistence."""


async def _drain(task: "asyncio.Task[T]") -> T:
    """Join an owned worker even if its async caller is cancelled again."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated cancel requests must not detach the underlying thread.
            # If the worker itself was cancelled, task.result() below raises.
            continue
    return task.result()


async def run_cancellable_worker(
    compute: Callable[[Callable[[], None]], T],
    commit: Callable[[T], R],
) -> R:
    """Compute off-loop, then commit once with an explicit cancellation boundary.

    ``compute`` must not write a result. It receives a cheap checkpoint which
    may raise ``ResearchWorkCancelled``. Cancellation before commit requests
    cooperative termination and joins computation before propagating
    ``CancelledError``; even an uncooperative compute cannot outlive its owner.

    Once commit is scheduled it wins the race: join it and return its actual
    outcome, even if cancellation arrives. A durable result must never be
    labelled cancelled. Neither phase blocks the ASGI event loop.
    """

    cancelled = threading.Event()

    def checkpoint() -> None:
        if cancelled.is_set():
            raise ResearchWorkCancelled("research computation cancelled")

    computation = asyncio.create_task(
        asyncio.to_thread(compute, checkpoint), name="research-computation"
    )
    try:
        value = await asyncio.shield(computation)
    except asyncio.CancelledError:
        cancelled.set()
        try:
            await _drain(computation)
        except (Exception, asyncio.CancelledError):
            # Cancellation was accepted before commit. Consume the worker's
            # terminal exception so no orphaned failure is logged later.
            pass
        raise

    # No await between observing completed computation and scheduling commit:
    # the event loop orders this boundary against a cancellation request.
    persistence = asyncio.create_task(
        asyncio.to_thread(commit, value), name="research-result-commit"
    )
    return await _drain(persistence)
