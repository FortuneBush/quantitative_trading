"""In-process asynchronous publish/subscribe event bus."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Union

from quant_monitor.domain import EventType, MarketEvent


EventHandler = Callable[[MarketEvent], Union[Awaitable[None], None]]
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Subscription:
    """Opaque subscription token returned by :meth:`EventBus.subscribe`."""

    event_type: Optional[EventType]
    handler: EventHandler


class EventBus:
    """A lightweight async event bus for one monitoring process.

    Subscribers for a publication are run concurrently.  One failing subscriber
    is logged and does not prevent other subscribers from receiving the event;
    set ``raise_handler_errors=True`` when constructing the bus in tests if strict
    propagation is preferable.  Subscribe to ``None`` to observe every event.
    """

    def __init__(self, raise_handler_errors: bool = False) -> None:
        self._handlers: Dict[Optional[EventType], Set[EventHandler]] = {}
        # Create the primitive inside the first async operation.  This keeps
        # the runtime constructible from synchronous code on Python 3.9,
        # where asyncio locks may bind to the current event loop at creation.
        self._lock: Optional[asyncio.Lock] = None
        self._raise_handler_errors = bool(raise_handler_errors)

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def subscribe(
        self,
        event_type: Optional[EventType],
        handler: EventHandler,
    ) -> Subscription:
        if not callable(handler):
            raise TypeError("event handler must be callable")
        normalized = None if event_type is None else (
            event_type if isinstance(event_type, EventType) else EventType(event_type)
        )
        async with self._get_lock():
            self._handlers.setdefault(normalized, set()).add(handler)
        return Subscription(normalized, handler)

    async def unsubscribe(self, subscription: Subscription) -> bool:
        async with self._get_lock():
            handlers = self._handlers.get(subscription.event_type)
            if not handlers or subscription.handler not in handlers:
                return False
            handlers.remove(subscription.handler)
            if not handlers:
                self._handlers.pop(subscription.event_type, None)
            return True

    async def publish(
        self,
        event: Union[MarketEvent, EventType, str],
        payload: Any = None,
        **metadata: Any
    ) -> MarketEvent:
        """Publish a pre-built event, or build one from ``type`` and ``payload``."""

        if isinstance(event, MarketEvent):
            if payload is not None or metadata:
                raise ValueError("payload/metadata cannot accompany a MarketEvent")
            market_event = event
        else:
            event_type = event if isinstance(event, EventType) else EventType(event)
            market_event = MarketEvent(event_type, payload, metadata=metadata)

        async with self._get_lock():
            handlers: List[EventHandler] = list(self._handlers.get(market_event.type, set()))
            handlers.extend(self._handlers.get(None, set()))
        if not handlers:
            return market_event

        results = await asyncio.gather(
            *(self._invoke(handler, market_event) for handler in handlers),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        for failure in failures:
            # Handler exceptions can contain provider URLs, credentials or
            # local paths.  Keep logs useful without serializing the failure
            # text or traceback; callers still receive the original exception
            # when ``raise_handler_errors`` is enabled.
            _LOGGER.error(
                "event handler failed for %s error_type=%s",
                market_event.type.value,
                type(failure).__name__,
            )
        if failures and self._raise_handler_errors:
            raise failures[0]
        return market_event

    @staticmethod
    async def _invoke(handler: EventHandler, event: MarketEvent) -> None:
        result = handler(event)
        if inspect.isawaitable(result):
            await result

    async def clear(self) -> None:
        async with self._get_lock():
            self._handlers.clear()

    async def subscriber_count(self, event_type: Optional[EventType] = None) -> int:
        normalized = None if event_type is None else (
            event_type if isinstance(event_type, EventType) else EventType(event_type)
        )
        async with self._get_lock():
            return len(self._handlers.get(normalized, set()))
