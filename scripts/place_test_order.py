"""Step-3 first live order: place ONE small market order on Binance TESTNET.

Verifies the full execution path end to end, with NO strategy loop running:

    CcxtBroker (testnet, sandbox-locked) -> OMS -> FillEvent -> Portfolio

and confirms the testnet account balance actually moves.

TESTNET ONLY — the broker forces ccxt sandbox mode on; this can never touch a
real-money account. Requires *spot* testnet keys from
https://testnet.binance.vision (log in with GitHub, create an API key):

    export BINANCE_TESTNET_API_KEY=...
    export BINANCE_TESTNET_SECRET=...

Usage:
    python scripts/place_test_order.py                       # BUY ~15 USDT BTC
    SYMBOL=ETH/USDT SIDE=buy NOTIONAL=20 python scripts/place_test_order.py
    SIDE=sell python scripts/place_test_order.py             # sell some BTC back
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# allow running directly: add repo root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import Credentials, load_credentials, load_local_env
from src.core.events import OrderEvent
from src.core.models import Order, OrderType, Side
from src.part3_execution.broker import CcxtBroker
from src.part3_execution.oms import OMS
from src.portfolio.portfolio import Portfolio


def _load_creds(venue: str) -> Credentials:
    """Pick the right paper-venue keys. Demo and testnet are separate venues
    with separate keys, so they read different env vars (auto-loaded from a
    gitignored .env if present)."""
    load_local_env()
    if venue == "demo":
        return Credentials(
            api_key=os.environ.get("BINANCE_DEMO_API_KEY"),
            secret=os.environ.get("BINANCE_DEMO_SECRET"),
        )
    return load_credentials("spot")  # testnet.binance.vision keys


def main() -> int:
    load_local_env()  # pull .env so PAPER_VENUE / keys are available
    venue = os.environ.get("PAPER_VENUE", "demo").lower()  # "demo" | "testnet"
    creds = _load_creds(venue)
    if not creds.present:
        if venue == "demo":
            print("Missing Binance Demo Trading API keys. Set them, then re-run:")
            print("  export BINANCE_DEMO_API_KEY=...")
            print("  export BINANCE_DEMO_SECRET=...")
            print("Create keys at https://demo.binance.com (API Management).")
        else:
            print("Missing testnet API keys. Set them, then re-run:")
            print("  export BINANCE_TESTNET_API_KEY=...")
            print("  export BINANCE_TESTNET_SECRET=...")
            print("Get keys at https://testnet.binance.vision (GitHub login).")
        return 2

    symbol = os.environ.get("SYMBOL", "BTC/USDT")
    side = Side.BUY if os.environ.get("SIDE", "buy").lower() == "buy" else Side.SELL
    notional = float(os.environ.get("NOTIONAL", "15"))  # target order size in USDT
    base = symbol.split("/")[0]

    venue_label = ("Binance Demo Trading (demo-api.binance.com)" if venue == "demo"
                   else "Binance Testnet (testnet.binance.vision)")
    print("=" * 60)
    print(f"STEP-3 LIVE PAPER ORDER  |  {side.value} ~{notional:g} USDT of {symbol}")
    print(f"  venue: {venue_label} — PAPER ONLY")
    print("=" * 60)

    broker = CcxtBroker(creds, market_type="spot", venue=venue)
    try:
        price = broker.last_price(symbol)
        if not price:
            print(f"FAILED: could not fetch testnet price for {symbol}")
            return 1
        qty = broker.amount_to_precision(symbol, notional / price)
        if qty <= 0:
            print(f"FAILED: computed qty {qty} <= 0 (raise NOTIONAL?)")
            return 1

        usdt_before = broker.get_cash("USDT")
        base_before = broker.get_cash(base)
        print(f"\nTESTNET price  {symbol} = {price:,.2f}")
        print(f"BEFORE  USDT={usdt_before}  {base}={base_before}")
        print(f"\nPlacing  {side.value} MARKET  qty={qty} {symbol}  (~{qty * price:,.2f} USDT)")

        # route through the real OMS so the whole path is exercised
        portfolio = Portfolio(starting_cash=usdt_before or 0.0)
        oms = OMS(broker)
        oms._last_price[symbol] = price  # seed market-order reference price
        order = Order(symbol=symbol, side=side, qty=float(qty), order_type=OrderType.MARKET)
        fills = oms.on_order(OrderEvent(order=order))
        if not fills:
            print("\nORDER REJECTED — see broker error above "
                  "(insufficient balance? min-notional? symbol?).")
            return 1
        fill = fills[0]
        portfolio.on_fill(fill)
        print(f"\nFILL  {fill.side.value} {fill.qty} {symbol} @ {fill.price:,.2f}  "
              f"fee={fill.fee}  exchange_id={order.exchange_id}")
        print(f"      order status = {order.status.value}")

        usdt_after = broker.get_cash("USDT")
        base_after = broker.get_cash(base)
        print(f"\nAFTER   USDT={usdt_after}  {base}={base_after}")
        if usdt_before is not None and usdt_after is not None:
            print(f"  USDT delta = {usdt_after - usdt_before:+.4f}")
        if base_before is not None and base_after is not None:
            print(f"  {base} delta = {base_after - base_before:+.8f}")
        print("\nPortfolio (local accounting from the fill):")
        for k, v in portfolio.snapshot().items():
            print(f"  {k}: {v}")
        print("\nOK - live testnet order placed, filled, and balance moved.")
    finally:
        broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
