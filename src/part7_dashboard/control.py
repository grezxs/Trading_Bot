"""Dashboard -> bot control channel (the reverse of state.py).

The Streamlit page and the bot are separate processes, so the dashboard's
buttons can't call the bot directly. Instead they write a tiny JSON command
file (``runtime/control.json``) that the bot polls every MARKET tick:

    {"trading_enabled": true, "kill_switch": false}

Three controls, matching the three dashboard buttons:
- Enable Trading  -> trading_enabled = true   (entries allowed)
- Stop Trading    -> trading_enabled = false  (entries off; protective TP/SL
                     exits still fire, positions kept — same soft semantics as
                     the config kill-switch)
- Kill Switch     -> kill_switch = true        (HARD stop: halt the engine and
                     cancel every resting order; latched until the bot restarts)

``BotControl`` is the shared in-process flag the RiskManager reads. ``ControlBridge``
is the MARKET handler that syncs it from the file and trips the hard halt.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTROL_PATH = _REPO_ROOT / "runtime" / "control.json"

log = logging.getLogger("control")


@dataclass
class BotControl:
    """Shared, in-process control flags the RiskManager consults each signal."""
    trading_enabled: bool = True
    killed: bool = False


def read_control(path: Optional[Path] = None) -> Optional[dict]:
    p = Path(path) if path else DEFAULT_CONTROL_PATH
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def write_control(trading_enabled: bool, kill_switch: bool,
                  path: Optional[Path] = None) -> None:
    """Atomically write the command file (used by the dashboard buttons)."""
    p = Path(path) if path else DEFAULT_CONTROL_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "trading_enabled": bool(trading_enabled),
        "kill_switch": bool(kill_switch),
        "updated": time.time(),
    }))
    os.replace(tmp, p)


class ControlBridge:
    """MARKET handler: pull dashboard commands into the live bot.

    Registered FIRST in the MARKET chain so a toggle takes effect on the same
    tick. Observer (returns None) — emits no events.
    """

    def __init__(self, control: BotControl, engine, oms,
                 path: Optional[Path] = None) -> None:
        self.control = control
        self.engine = engine
        self.oms = oms
        self.path = Path(path) if path else DEFAULT_CONTROL_PATH

    def on_market(self, event) -> None:
        cmd = read_control(self.path)
        if cmd is None:
            return None
        self.control.trading_enabled = bool(cmd.get("trading_enabled", True))
        if cmd.get("kill_switch") and not self.control.killed:
            self.control.killed = True
            log.critical("KILL SWITCH engaged from dashboard — halting + canceling orders")
            try:
                n = self.oms.cancel_all()
                log.warning("kill switch: canceled %d resting order(s)", n)
            except Exception:
                log.exception("kill switch cancel_all failed")
            self.engine.halt()  # drops all future ORDER events; latched
        return None

    def seed_file(self) -> None:
        """Write the initial command file from the in-process defaults so the
        dashboard shows the right state before any button is pressed."""
        write_control(self.control.trading_enabled, False, self.path)
