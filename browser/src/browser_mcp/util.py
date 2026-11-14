"""Shared path helpers for the MCP browser server (workspace-relative defaults)."""
from __future__ import annotations

import os
import time
from pathlib import Path


def _workspace() -> Path:
    return Path(os.environ.get("SAMET_WORKSPACE", ".")).resolve()


def screenshot_path(explicit: str = "") -> Path:
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else _workspace() / p
    shots = _workspace() / ".samet" / "screenshots"
    return shots / f"shot_{int(time.time() * 1000)}.png"


def cookie_path(explicit: str = "") -> Path:
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else _workspace() / p
    return _workspace() / ".samet" / "cookies.json"


def sandbox_network_path(explicit: str = "") -> Path:
    """Durable sandbox network log: <workspace>/network_logs.json.

    When the workspace already IS a per-session sandbox dir
    (``.../sandbox/<sid>``) the file lives directly inside it; otherwise it
    lives in ``<workspace>/sandbox/network_logs.json``. Mirrors the
    screenshot_path/cookie_path workspace-relative pattern.
    """
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else _workspace() / p
    ws = _workspace()
    if ws.parent.name == "sandbox":
        return ws / "network_logs.json"
    return ws / "sandbox" / "network_logs.json"
