"""Async event engine: one queue, handlers registered per event type.

Handlers run in *registration order* within an event type. Each handler is
``handler(event) -> Optional[Iterable[Event]]`` and may be sync or async;
returned events are fed back into the queue.

Producers (data connectors) push MARKET events. In mock mode the producers
are finite and the engine drains the queue then stops; in testnet mode the
producers run until cancelled (Ctrl-C).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import Awaitable, Callable, Iterable, Optional, Union

from .events import Event, EventType

log = logging.getLogger("engine")

Handler = Callable[[Event], Union[Optional[Iterable[Event]], Awaitable[Optional[Iterable[Event]]]]]


class Engine:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self._handlers: dict[EventType, list[Handler]] = defaultdict(list)
        self._halted = False

    def register(self, event_type: EventType, handler: Handler) -> None:
        """Register a handler. Order matters — see module docstring."""
        self._handlers[event_type].append(handler)

    def halt(self) -> None:
        """Stop dispatching ORDER events (hard risk halt)."""
        self._halted = True
        log.critical("ENGINE HALTED — human intervention required")

    @property
    def halted(self) -> bool:
        return self._halted

    async def put(self, event: Event) -> None:
        await self.queue.put(event)

    async def _dispatch(self, event: Event) -> None:
        if self._halted and event.type == EventType.ORDER:
            log.warning("dropping ORDER while halted: %s", event)
            return
        for handler in self._handlers.get(event.type, []):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:  # one bad handler must not kill the loop
                log.exception("handler %r failed on %s", handler, event.type)
                continue
            if result:
                for new_event in result:
                    await self.queue.put(new_event)

    async def run(self, producers: Iterable[Awaitable[None]]) -> None:
        """Run until all producers finish and the queue drains.

        Pass long-running producers (testnet) and the loop runs until they
        are cancelled. Pass finite producers (mock) and it exits cleanly.
        """
        producer_tasks = [asyncio.create_task(p) for p in producers]
        try:
            while True:
                try:
                    event = await asyncio.wait_for(self.queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if all(t.done() for t in producer_tasks) and self.queue.empty():
                        break
                    continue
                await self._dispatch(event)
                self.queue.task_done()
        finally:
            for t in producer_tasks:
                t.cancel()
            await asyncio.gather(*producer_tasks, return_exceptions=True)
            # surface producer crashes rather than swallowing them
            for t in producer_tasks:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is not None:
                    log.error("producer crashed: %r", exc)
