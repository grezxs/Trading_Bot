"""Order Management System.

Handles ORDER events: hands the order to the broker, and on a fill emits a
FillEvent. Tracks the last mark per symbol so MARKET orders have a reference
price (the broker reports the real fill).

Implemented (verified in mock): market orders, full fills, fee pass-through,
LIMIT 5s-TTL auto-cancel, and kill orders (cancel one / cancel-all).

TODO (sookoon):
- Partial-fill handling (multiple FillEvents per order).
The hooks below (``_open_orders``, ``on_market`` TTL sweep, ``kill``) are
scaffolding for these.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..core.engine import Engine
from ..core.events import FillEvent, MarketEvent, OrderEvent
from ..core.models import Order, OrderStatus, OrderType
from .broker import BaseBroker

log = logging.getLogger("oms")


class OMS:
    def __init__(self, broker: BaseBroker, default_ttl: float = 5.0,
                 cancel_unfilled: bool = True,
                 engine: Optional[Engine] = None) -> None:
        self.broker = broker
        self.default_ttl = default_ttl
        self.cancel_unfilled = cancel_unfilled
        self.engine = engine  # optional: cancel-all when the engine halts
        self._last_price: dict[str, float] = {}
        self._open_orders: dict[int, Order] = {}  # live LIMIT orders awaiting fill

    def on_market(self, event: MarketEvent) -> None:
        """Track the mark, sweep expired unfilled LIMIT orders, and — if the
        engine has halted — cancel everything still resting.

        Part 4 (rough): any open LIMIT older than its ttl is canceled at the
        broker and marked CANCELED. MARKET orders never rest here, so this only
        affects resting limits (e.g. on the live demo venue). On a hard halt
        (drawdown breach) we also pull all live orders so nothing fills after
        the kill-switch trips.
        """
        self._last_price[event.symbol] = event.price
        # hard halt: pull every resting order, then stop touching the book
        if self.engine is not None and self.engine.halted:
            if self._open_orders:
                n = self.cancel_all()
                log.warning("engine halted — canceled %d resting order(s)", n)
            return None
        if not self.cancel_unfilled or not self._open_orders:
            return None
        now = event.ts
        for order in list(self._open_orders.values()):
            if order.order_type is not OrderType.LIMIT or not order.ttl:
                continue
            if (now - order.created_ts) < order.ttl:
                continue
            ok = self.broker.cancel(order)
            order.status = OrderStatus.CANCELED if ok else order.status
            self._open_orders.pop(order.order_id, None)
            log.info("limit TTL %.0fs expired — %s cancel %s %s %.6f",
                     order.ttl, "canceled" if ok else "FAILED to cancel",
                     order.symbol, order.side.value, order.qty)
        return None

    def on_order(self, event: OrderEvent) -> Optional[list[FillEvent]]:
        order = event.order
        ref = self._last_price.get(order.symbol, order.limit_price or 0.0)

        if order.order_type is OrderType.LIMIT:
            order.ttl = order.ttl or self.default_ttl
            self._open_orders[order.order_id] = order  # may rest; swept in on_market

        fill = self.broker.place(order, ref)
        if fill is None:
            order.status = OrderStatus.REJECTED
            log.warning("order rejected: %s %s %.6f", order.symbol, order.side.value, order.qty)
            self._open_orders.pop(order.order_id, None)
            return None

        filled_qty = float(fill.get("qty") or 0.0)
        order.exchange_id = fill.get("exchange_id")
        order.avg_fill_price = float(fill.get("price") or ref or 0.0)

        # A LIMIT can come back unfilled (resting on the book). Keep it open so
        # the TTL sweep in on_market can cancel it later; emit no fill yet.
        if filled_qty <= 0:
            order.status = OrderStatus.NEW
            log.info("limit resting (no immediate fill): %s %s %.6f @ %.2f",
                     order.symbol, order.side.value, order.qty, order.limit_price or 0.0)
            return None

        order.filled_qty = filled_qty
        order.status = OrderStatus.FILLED if order.remaining <= 1e-12 else OrderStatus.PARTIAL
        if order.status is OrderStatus.FILLED:
            self._open_orders.pop(order.order_id, None)

        return [FillEvent(
            symbol=order.symbol,
            side=order.side,
            qty=filled_qty,
            price=order.avg_fill_price,
            order_id=order.order_id,
            fee=fill.get("fee", 0.0),
        )]

    def kill(self, order_id: int) -> bool:
        """Cancel a single live order by its internal id.

        Cancels at the broker, marks the order CANCELED and drops it from the
        open book. Returns False if the id isn't a live order or the venue
        rejects the cancel (in which case the order is left open to retry).
        """
        order = self._open_orders.get(order_id)
        if order is None:
            return False
        ok = self.broker.cancel(order)
        if ok:
            order.status = OrderStatus.CANCELED
            self._open_orders.pop(order_id, None)
            log.info("killed order %d: %s %s %.6f", order_id,
                     order.symbol, order.side.value, order.qty)
        else:
            log.error("kill FAILED for order %d (%s) — left open", order_id, order.symbol)
        return ok

    def cancel_all(self, symbol: Optional[str] = None) -> int:
        """Cancel all live orders, or just those for ``symbol``. Returns the
        number actually canceled. Used for a panic flatten / engine halt."""
        targets = [o for o in self._open_orders.values()
                   if symbol is None or o.symbol == symbol]
        canceled = 0
        for order in targets:
            if self.kill(order.order_id):
                canceled += 1
        return canceled
