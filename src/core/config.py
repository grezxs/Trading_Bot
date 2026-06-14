"""Config + credential loading.

Config values come from ``config/config.yaml`` (overridable path). API
credentials come ONLY from environment variables — never from the config
file, never hard-coded, never committed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG = _REPO_ROOT / "config" / "config.yaml"
_ENV_FILE = _REPO_ROOT / ".env"


def load_local_env(path: Optional[str | Path] = None) -> None:
    """Load ``KEY=VALUE`` lines from a gitignored ``.env`` into ``os.environ``.

    Convenience so credentials can be configured once in ``.env`` instead of
    re-exporting every shell. Existing env vars WIN (never overridden), so the
    env-only contract holds: ``.env`` is just a local, untracked env source and
    is gitignored. Silently does nothing if the file is absent.
    """
    env_path = Path(path) if path else _ENV_FILE
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


@dataclass
class Credentials:
    api_key: Optional[str] = None
    secret: Optional[str] = None

    @property
    def present(self) -> bool:
        return bool(self.api_key and self.secret)


def load_credentials(market: str = "spot") -> Credentials:
    """Load testnet credentials from env (auto-loading ``.env`` first).

    ``spot``   -> testnet.binance.vision keys.
    ``future`` -> testnet.binancefuture.com keys (shorting; see brief).
    """
    load_local_env()
    if market == "future":
        return Credentials(
            api_key=os.environ.get("BINANCE_FUTURES_TESTNET_API_KEY"),
            secret=os.environ.get("BINANCE_FUTURES_TESTNET_SECRET"),
        )
    return Credentials(
        api_key=os.environ.get("BINANCE_TESTNET_API_KEY"),
        secret=os.environ.get("BINANCE_TESTNET_SECRET"),
    )


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    # --- convenience accessors with sane defaults ---
    @property
    def symbols(self) -> list[str]:
        return list(self.raw.get("symbols", ["BTC/USDT", "ETH/USDT"]))

    @property
    def market_interval(self) -> float:
        return float(self.raw.get("intervals", {}).get("market_sec", 10))

    @property
    def signal_interval(self) -> float:
        return float(self.raw.get("intervals", {}).get("signal_sec", 60))

    @property
    def regime_window(self) -> int:
        return int(self.raw.get("intervals", {}).get("regime_window", 30))

    @property
    def timeframe(self) -> str:
        return str(self.raw.get("data", {}).get("timeframe", "1m"))

    @property
    def kline_limit(self) -> int:
        return int(self.raw.get("data", {}).get("kline_limit", 100))

    @property
    def book_depth(self) -> int:
        return int(self.raw.get("data", {}).get("book_depth", 10))

    @property
    def data_source(self) -> str:
        """'mainnet' (real market data) or 'testnet' (paper data). Default
        conservative ('testnet') if unset; the shipped config uses mainnet."""
        return str(self.raw.get("data", {}).get("source", "testnet")).lower()

    @property
    def starting_cash(self) -> float:
        return float(self.raw.get("portfolio", {}).get("starting_cash", 10_000))

    @property
    def risk(self) -> dict[str, Any]:
        return dict(self.raw.get("risk", {}))

    @property
    def order(self) -> dict[str, Any]:
        """Order/trade policy knobs (``order:`` block in config.yaml).

        Returns the raw dict merged over sane defaults, so callers can read any
        key without a KeyError even if the user trims the YAML. See config.yaml
        for what each knob does. Wired into the OMS/risk path in Part 4-5.
        """
        defaults: dict[str, Any] = {
            "type": "market",
            "limit_offset_bps": 5.0,
            "time_in_force": "GTC",
            "sizing": "notional",
            "notional_usdt": 100.0,
            "base_qty": 0.0001,
            "pct_equity": 0.10,
            "allin_buffer_pct": 0.01,
            "take_profit_pct": 0.03,
            "stop_loss_pct": 0.03,
            "trailing_stop_pct": 0.0,
            "inventory_balance": False,
            "inventory_target_usdt": 100.0,
            "inventory_tolerance_pct": 0.25,
            "cancel_unfilled": True,
            "ttl_sec": 5.0,
            "trading_enabled": True,
            "max_position_value_per_symbol": 200.0,
            "max_open_positions": 2,
            "cooldown_sec": 30.0,
            "min_notional_usdt": 10.0,
            "max_slippage_bps": 50.0,
            "daily_max_loss_pct": 0.10,
        }
        defaults.update(self.raw.get("order", {}) or {})
        return defaults

    @property
    def mock(self) -> dict[str, Any]:
        return dict(self.raw.get("mock", {}))

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def load_config(path: Optional[str | Path] = None) -> Config:
    cfg_path = Path(path) if path else _DEFAULT_CONFIG
    if not cfg_path.exists():
        return Config(raw={})
    with cfg_path.open("r") as f:
        data = yaml.safe_load(f) or {}
    return Config(raw=data)
