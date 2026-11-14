"""CDP WebSocket bridge for Ghostwire on SeleniumBase UC Chrome.

SeleniumBase owns the browser (chromedriver + ``execute_cdp_cmd`` for sends,
``get_log("performance")`` for Network drain).  Ghostwire needs the event
channels SeleniumBase does NOT forward — ``Debugger.paused``,
``HeapProfiler.addHeapSnapshotChunk``, ``Debugger.scriptParsed`` — which only
arrive as CDP events.  This module opens a *parallel* WebSocket to the same
Chrome (via ``debuggerAddress → /json → webSocketDebuggerUrl``) that listens
only for events, sharing nothing with the drain.

If the WS cannot be established (no debuggerAddress, http/json unreachable,
WS lib missing) the bridge stays degraded: ``alive == False`` and callers
fall back to ``execute_cdp_cmd`` polling or return a useful hint.  No new
hard dependency — ``websocket-client`` is already transitive via seleniumbase.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
import urllib.error

log = logging.getLogger(__name__)


class CdpWsBridge:
    """Event-only WS bridge.  Commands still go via ``execute_cdp_cmd``."""

    def __init__(self, driver):
        self.driver = driver
        self._handlers: dict[str, list] = {}
        self._lock = threading.RLock()
        self._ws = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.alive = False
        self._ws_url: str | None = None
        self._connect()

    # -- discovery ----------------------------------------------------------
    def _debugger_address(self) -> str | None:
        try:
            caps = getattr(self.driver, "capabilities", {}) or {}
            opts = caps.get("goog:chromeOptions") or {}
            addr = opts.get("debuggerAddress")
            if addr:
                return str(addr)
        except Exception:
            pass
        # fallback: try service url host (chromedriver listens nearby)
        return None

    def _fetch_ws_url(self, addr: str) -> str | None:
        # addr like "127.0.0.1:9222" or "localhost:58123"
        for host in (addr,):
            for path in ("/json", "/json/list"):
                url = f"http://{host}{path}"
                try:
                    with urllib.request.urlopen(url, timeout=1.5) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                        # /json returns list of targets; pick page/browser with webSocketDebuggerUrl
                        if isinstance(data, list):
                            for item in data:
                                ws = item.get("webSocketDebuggerUrl")
                                if ws:
                                    return ws
                        elif isinstance(data, dict) and data.get("webSocketDebuggerUrl"):
                            return data["webSocketDebuggerUrl"]
                except Exception as e:
                    log.debug("CDP /json fetch failed %s: %s", url, e)
                    continue
        return None

    def _connect(self) -> None:
        addr = self._debugger_address()
        if not addr:
            log.debug("CdpWsBridge: no debuggerAddress in capabilities — degraded")
            return
        ws_url = self._fetch_ws_url(addr)
        if not ws_url:
            log.debug("CdpWsBridge: could not resolve webSocketDebuggerUrl at %s", addr)
            return
        self._ws_url = ws_url
        try:
            import websocket  # websocket-client

            ws = websocket.WebSocketApp(
                ws_url,
                on_message=self._on_message,
                on_error=lambda _ws, err: log.debug("WS error: %s", err),
                on_close=lambda *_: log.debug("WS closed"),
                on_open=lambda _ws: log.debug("WS open %s", ws_url),
            )
            self._ws = ws
            t = threading.Thread(target=ws.run_forever, name="samet-cdp-ws", daemon=True)
            t.start()
            self._thread = t
            # give handshake a moment
            time.sleep(0.3)
            self.alive = True
            log.debug("CdpWsBridge alive at %s", ws_url)
        except Exception as e:
            log.debug("CdpWsBridge connect failed: %s", e)

    # -- event dispatch -----------------------------------------------------
    def on(self, method: str, handler) -> None:
        with self._lock:
            self._handlers.setdefault(method, []).append(handler)

    def off(self, method: str, handler) -> None:
        with self._lock:
            lst = self._handlers.get(method) or []
            try:
                lst.remove(handler)
            except ValueError:
                pass

    def _on_message(self, _ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        # replies (id) ignored — sends go via execute_cdp_cmd
        method = msg.get("method")
        if not method:
            return
        params = msg.get("params") or {}
        session_id = msg.get("sessionId")
        # snapshot handlers to avoid holding lock while calling
        with self._lock:
            handlers = list(self._handlers.get(method, []))
        for h in handlers:
            try:
                # ghostwire handlers signature: (params, session_id)
                try:
                    h(params, session_id)
                except TypeError:
                    h(params)
            except Exception as e:
                log.debug("WS handler %s failed: %s", method, e)

    def close(self) -> None:
        self._stop.set()
        self.alive = False
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
