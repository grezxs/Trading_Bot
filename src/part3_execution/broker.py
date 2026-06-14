"""Brokers: the thing that actually places orders and reports cash.

- ``MockBroker``  — fills market orders instantly at the requested price
  (plus a configurable taker fee). Used in mock mode and unit tests.
- ``CcxtBroker``  — Binance *testnet* via ccxt (sandbox forced on). Wraps
  ``create_order`` and ``fetch_balance``. Credentials come from env only.

A broker returns a fill dict: {price, qty, fee, exchange_id} or None on
failure. The OMS turns that into a FillEvent.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..core.config import Credentials
from ..core.models import Order, OrderType, Side

log = logging.getLogger("broker")


class BaseBroker:
    def place(self, order: Order, ref_price: float) -> Optional[dict]:
        raise NotImplementedError

    def get_cash(self, quote: str = "USDT") -> Optional[float]:
        raise NotImplementedError

    def cancel(self, order: Order) -> bool:  # pragma: no cover - mock no-op
        return True

    def close(self) -> None:  # pragma: no cover
        pass


class MockBroker(BaseBroker):
    def __init__(self, starting_cash: float = 10_000.0, taker_fee: float = 0.001) -> None:
        self._cash = starting_cash
        self.taker_fee = taker_fee

    def place(self, order: Order, ref_price: float) -> Optional[dict]:
        price = order.limit_price if order.order_type is OrderType.LIMIT else ref_price
        if price is None or price <= 0:
            return None
        fee = price * order.qty * self.taker_fee
        self._cash -= order.side.sign * price * order.qty + fee
        return {"price": float(price), "qty": float(order.qty), "fee": float(fee),
                "exchange_id": f"mock-{order.order_id}"}

    def get_cash(self, quote: str = "USDT") -> Optional[float]:
        return self._cash


class CcxtBroker(BaseBroker):
    """Live PAPER broker. Two interchangeable paper venues, never production:

    - ``venue="testnet"`` (default): ccxt sandbox -> testnet.binance.vision.
    - ``venue="demo"``: Binance main-site Demo Trading -> demo-api.binance.com.
      ccxt has no sandbox preset for this, so we repoint the spot REST endpoints
      by hand. Demo keys are made at https://demo.binance.com API management.

    A hard guard (``_assert_paper``) refuses to run if the active spot endpoint
    is production ``api.binance.com`` — so this can never place a real order.
    """

    PROD_HOST = "https://api.binance.com"
    DEMO_HOST = "https://demo-api.binance.com"

    def __init__(self, creds: Credentials, market_type: str = "spot",
                 venue: str = "testnet") -> None:
        import ccxt

        if not creds.present:
            raise ValueError("CcxtBroker requires API credentials (env vars)")
        self.venue = venue
        self._ex = ccxt.binance({
            "apiKey": creds.api_key,
            "secret": creds.secret,
            "enableRateLimit": True,
            "options": {"defaultType": market_type},  # "future" => shorting
        })
        if venue == "demo":
            self._use_demo_endpoints()  # main-site Demo Trading (paper)
            # Demo host serves ONLY the spot trading /api/v3 surface — no /sapi,
            # no futures (fapi/dapi). By default ccxt's load_markets also fetches
            # linear/inverse exchange info and (with keys) margin pairs +
            # currencies, which here either 404 on the demo host or leak to the
            # production fapi/dapi domains. Restrict market loading to spot and
            # disable the margin/currency calls — markets still load via
            # /api/v3/exchangeInfo, which is all we need to size + place orders.
            self._ex.options["fetchMarkets"] = ["spot"]
            self._ex.options["fetchMargins"] = False
            self._ex.has["fetchCurrencies"] = False
        else:
            self._ex.set_sandbox_mode(True)  # testnet.binance.vision (paper)
        self._assert_paper()

    def _use_demo_endpoints(self) -> None:
        """Repoint every spot ``api.binance.com`` REST URL at the demo host.

        Leaves futures/options (dapi/fapi/eapi) untouched — we trade spot. We
        do NOT call set_sandbox_mode here; demo is a separate paper venue.
        """
        api = self._ex.urls["api"]
        for key, url in list(api.items()):
            if isinstance(url, str) and url.startswith(self.PROD_HOST):
                api[key] = url.replace(self.PROD_HOST, self.DEMO_HOST, 1)

    def _assert_paper(self) -> None:
        """Fail closed: refuse to operate against the production spot host."""
        host = str(self._ex.urls["api"].get("private", ""))
        if host.startswith(self.PROD_HOST):
            raise RuntimeError(
                f"REFUSING to trade: spot endpoint is PRODUCTION ({host}). "
                "Paper venue (testnet/demo) was not applied.")
        log.info("CcxtBroker paper venue=%s | spot endpoint=%s", self.venue, host)

    def place(self, order: Order, ref_price: float) -> Optional[dict]:
        side = "buy" if order.side is Side.BUY else "sell"
        otype = "market" if order.order_type is OrderType.MARKET else "limit"
        params: dict = {}
        try:
            resp = self._ex.create_order(
                order.symbol, otype, side, order.qty,
                order.limit_price if otype == "limit" else None, params,
            )
        except Exception as exc:
            log.error("create_order failed (%s %s %s): %s",
                      order.symbol, side, order.qty, exc)
            return None
        filled = float(resp.get("filled") or 0.0)
        avg = resp.get("average") or resp.get("price") or ref_price
        fee = 0.0
        if resp.get("fee") and resp["fee"].get("cost") is not None:
            fee = float(resp["fee"]["cost"])
        # Report the ACTUAL filled qty. A resting LIMIT comes back filled=0; the
        # OMS keeps it open for the TTL sweep instead of booking a phantom fill.
        # A MARKET order fills immediately so filled > 0.
        return {"price": float(avg), "qty": filled, "fee": fee,
                "exchange_id": str(resp.get("id")), "status": resp.get("status")}

    def get_cash(self, quote: str = "USDT") -> Optional[float]:
        try:
            bal = self._ex.fetch_balance()
        except Exception as exc:
            log.error("fetch_balance failed: %s", exc)
            return None
        return float(bal.get("free", {}).get(quote, 0.0))

    def last_price(self, symbol: str) -> Optional[float]:
        """Latest traded price on the (testnet) venue — used to size orders."""
        try:
            t = self._ex.fetch_ticker(symbol)
        except Exception as exc:
            log.error("fetch_ticker(%s) failed: %s", symbol, exc)
            return None
        px = t.get("last") or t.get("close")
        return float(px) if px else None

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Round qty to the market's allowed precision (else Binance rejects)."""
        try:
            self._ex.load_markets()
            return float(self._ex.amount_to_precision(symbol, amount))
        except Exception as exc:
            log.warning("amount_to_precision(%s) failed: %s", symbol, exc)
            return float(amount)

    def cancel(self, order: Order) -> bool:
        if not order.exchange_id:
            return False
        try:
            self._ex.cancel_order(order.exchange_id, order.symbol)
            return True
        except Exception as exc:
            log.error("cancel_order failed: %s", exc)
            return False

    def close(self) -> None:
        try:
            self._ex.close()
        except Exception:
            pass
