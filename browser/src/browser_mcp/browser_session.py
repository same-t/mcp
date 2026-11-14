"""SeleniumBase UC-mode (undetected Chrome) browser session — full API.

Every method is synchronous; MCP tools call them via asyncio.to_thread / executor.

Categories:
  Navigation · Element Finding · Element Info · Mouse · Keyboard · Form ·
  Screenshot · Viewport · JS · Wait · Tabs · Frames · Dialog · Cookies ·
  Storage · Network · Console/Debug · Browser Management · Download ·
  Accessibility · Agent Helpers
"""
import datetime
import fnmatch
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# JS helpers
# ---------------------------------------------------------------------------

# Full interactive-element inventory injected into the page.
# KEY DESIGN DECISIONS:
#   - No O(n²) dedup: querySelectorAll on a flat selector list never returns duplicates.
#   - Stable IDs: window.__mcpElCounter + data-mcp-el-id attribute; already-tagged elements
#     keep their original ID across multiple calls, so agent indices survive re-renders.
#   - Visibility: checks own rect + computed style + viewport bounds + occlusion test.
#   - Accessible name: WAI-ARIA order (aria-labelledby → aria-label → <label> → innerText → title/alt).
#   - Shadow DOM: recursive traversal via shadowRoot.
from ._browser_impl.js import _ELEMENTS_JS, _TRACE_JS

GW = "/*gw*/"

# Ghostwire (lazy import to avoid circular / missing ws)
try:
    from ._browser_impl.cdp_ws import CdpWsBridge  # type: ignore
except Exception:  # pragma: no cover
    CdpWsBridge = None  # type: ignore

try:
    from ._browser_impl.ghostwire import (  # type: ignore
        CryptoLogger,
        DataflowTracer,
        GhostwireEngine,
        LiveObjects,
        Oracle,
        OriginTracer,
        ScriptWatcher,
        StringDumper,
        Tracer,
        VMTracer,
        diff_snapshots,
        take_snapshot,
    )
except Exception:  # pragma: no cover
    GhostwireEngine = None  # type: ignore
    Tracer = None  # type: ignore
    Oracle = None  # type: ignore
    LiveObjects = None  # type: ignore
    CryptoLogger = None  # type: ignore
    StringDumper = None  # type: ignore
    VMTracer = None  # type: ignore
    OriginTracer = None  # type: ignore
    DataflowTracer = None  # type: ignore
    ScriptWatcher = None  # type: ignore
    take_snapshot = None  # type: ignore
    diff_snapshots = None  # type: ignore


def _default_headless() -> bool:
    """Should Chrome start headless when the caller expressed no preference?

    `SAMET_BROWSER_HEADLESS` stays authoritative when it is set to anything, so
    an operator can still force a headed browser on a box that happens to have
    a display.

    The default used to be a hard `"0"` — always headed.  On a headless Linux
    server (systemd unit, no X/Wayland) that asks Chrome to open a window on a
    display that does not exist, and the launch dies; because `start()` runs
    lazily inside an MCP tool, the MCP server itself came up fine and the
    failure showed up as a per-tool error instead.  So on Linux the absence of
    a display is now itself the signal.

    The `linux` guard is load-bearing: this Windows dev box also has no
    `DISPLAY`, and headed mode is exactly what you want there.
    """
    raw = os.environ.get("SAMET_BROWSER_HEADLESS")
    if raw is not None and raw.strip():
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if sys.platform.startswith("linux") and not (
        os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")
    ):
        return True
    return False


# Accessibility tree builder
_A11Y_JS = r"""
(function(){
  function walk(el, depth){
    if (depth > 8) return null;
    var role = el.getAttribute('role') || el.tagName.toLowerCase();
    var name = el.getAttribute('aria-label')
             || (el.innerText || '').trim().slice(0, 80)
             || el.getAttribute('placeholder')
             || el.getAttribute('title')
             || '';
    var r = el.getBoundingClientRect();
    var vis = r.width > 0 && r.height > 0;
    var node = {
      role: role,
      name: name,
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
      expanded: el.getAttribute('aria-expanded'),
      checked: el.getAttribute('aria-checked'),
      selected: el.getAttribute('aria-selected'),
      visible: vis
    };
    var childNodes = [];
    var children = el.children;
    for (var i = 0; i < children.length && childNodes.length < 30; i++){
      var child = walk(children[i], depth + 1);
      if (child) childNodes.push(child);
    }
    if (childNodes.length) node.children = childNodes;
    return node;
  }
  return walk(document.body, 0);
})()
"""

def _xpath_escape(text: str) -> str:
    """Escapes strings for use in XPath (concat trick)."""
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat('" + "', \"'\" , '".join(parts) + "')"


# ---------------------------------------------------------------------------
# JS execution trace — injected once per page, idempotent.
# Captures which JS functions/files run when an action (e.g. a like click)
# fires, so the agent can answer "hangi dosyada hangi fonksiyon sırayla
# çalıştı?" without guessing.  Implementation:
#   * Wraps EventTarget.addEventListener to log every listener invocation
#     with Error().stack (file:line file:line is what the user asked for).
#   * Wraps fetch / XHR / setTimeout / requestAnimationFrame to keep the
#     async continuation visible as well.
#   * All records go to window.__sametTrace.calls; start/stop just toggles
#     `enabled` and optionally clears.  No Debugger domain required.
# _TRACE_JS moved to browser/js.py


# ---------------------------------------------------------------------------
# Network capture filter
# Four categories are recorded (everything else is dropped at capture):
#   type="request" → HTTP calls: XHR/fetch and anything API-like (GET/POST/
#                    PUT/DELETE/PATCH...), including unknown-typed entries
#   type="js"      → JavaScript resources (.js)
#   type="wss"     → WebSocket connections/frames
#   type="doc"     → top-level navigations and iframe documents.  These were
#                    dropped historically, which silently threw away every
#                    form-submit POST body and every redirect chain — the
#                    single most valuable piece of evidence when reverse
#                    engineering a login/signup flow.  A page load produces a
#                    handful of documents, so keeping them costs nothing.
# Dropped: image, media, font, stylesheet/css, manifest, ping, preflight etc.
# They are noise for API work and would flood the FIFO buffer.
_CAPTURE_SKIP_TYPES = {
    "image", "img", "media", "font", "stylesheet", "css",
    "manifest", "ping", "preflight", "cspviolationreport", "texttrack",
    "signedexchange",
}


def _capture_record_type(cdp_type) -> str | None:
    """Map a CDP resource type to our capture category, or None to drop it."""
    t = str(cdp_type or "").strip().lower()
    if t in _CAPTURE_SKIP_TYPES:
        return None
    if t == "script":
        return "js"
    if t == "document":
        return "doc"
    if t == "eventsource":
        return "sse"
    # xhr, fetch, websocket(legacy), other and unknown types all count as
    # request traffic — dropping them could lose API evidence.
    return "request"

# ---------------------------------------------------------------------------
class BrowserSession:
    # Configuration constants
    MAX_NETWORK_REQUESTS = 20000  # FIFO limit for captured network requests
    MAX_CONSOLE_LOGS = 5000      # FIFO limit for console logs
    MAX_PAGE_ERRORS = 2000       # FIFO limit for JS errors
    MAX_BODY_CHARS = 400_000     # per-request response body cap (chars)
    BODIES_PER_DRAIN = 400       # max getResponseBody calls per drain cycle
    MAX_WS_MESSAGES = 20000      # FIFO limit for websocket frames
    WS_PAYLOAD_CHARS = 20_000    # per-frame websocket payload cap (chars)

    # --- background drain -----------------------------------------------------
    # `driver.get_log("performance")` is a DESTRUCTIVE read of a BOUNDED buffer:
    # whatever is not read in time is dropped by Chrome, forever.  Draining only
    # from the getters (the old behaviour) meant "listening" was a lie — browse
    # for a minute, then call browser_network, and the earliest requests were
    # already gone.  A daemon thread now drains continuously so capture no
    # longer depends on the agent remembering to ask.
    DRAIN_INTERVAL_SEC = 0.4
    DRAIN_IDLE_INTERVAL_SEC = 1.5   # back off when the log comes back empty

    # --- durable capture ------------------------------------------------------
    # In-memory capture used to be the only copy, so a driver self-heal
    # (reset()) or an MCP restart destroyed the evidence.  Every record is now
    # mirrored to a JSONL file as it is captured/updated.
    PERSIST_ENABLED = True
    PERSIST_DIRNAME = ".samet/network"
    PERSIST_FLUSH_EVERY = 25        # records buffered before an fsync-less flush
    # Sandbox mirror: every captured request is also durably mirrored to
    # <workspace>/network_logs.json (JSON array, append-merge, dedup by id) so
    # CDP traffic stays visible inside the session sandbox.
    SANDBOX_LOGNAME = "network_logs.json"

    def __init__(self):
        self._driver = None
        # Reentrant lock guarding driver lifecycle and shared capture state.
        # FastMCP may dispatch concurrent tool coroutines onto worker threads.
        self.state_lock = threading.RLock()
        self.requests: dict[str, dict] = {}   # requestId -> record
        self._request_order: list[str] = []   # Track insertion order for FIFO cleanup
        self.last_attrs: dict[str, str] = {}  # stable mcp-id -> data-mcp-el-id value (same thing)
        self.console_logs: list[dict] = []
        self.page_errors: list[dict] = []
        # Capture is ON by default: the agent should never have to "arm" the
        # recorder before a flow it did not know it would need.  Explicitly
        # calling stop_network_recording() is the only way to pause it.
        self._network_recording = True
        self._downloads: list[dict] = []
        self._blocked_urls: list[str] = []
        self._current_frame = None  # None = main frame
        self._action_history: list[dict] = []
        self._ws_messages: list[dict] = []
        self._ws_connections: dict[str, dict] = {}
        self._ws_recording = False
        # Background drain machinery
        self._drain_lock = threading.RLock()
        self._drain_thread: threading.Thread | None = None
        self._drain_stop = threading.Event()
        self._drain_stats = {
            "cycles": 0, "events": 0, "dropped_events": 0, "errors": 0,
            "bodies_ok": 0, "bodies_failed": 0, "last_error": "",
            "last_drain_ts": 0.0, "started_at": 0.0,
        }
        # Durable capture
        self._persist_path: Path | None = None
        self._persist_fh = None
        self._persist_pending = 0
        self._persist_workspace: Path | None = None
        # Last live-browser interaction (for close guard)
        self._last_action_ts: float = 0.0
        # Sandbox mirror of the same traffic: <workspace>/network_logs.json
        # (or sandbox/network_logs.json when the workspace is the repo root).
        self._sandbox_records: dict[str, dict] = {}
        self._sandbox_pending = 0
        self._sandbox_loaded = False
        # Ghostwire (lazy — single Chrome adapter)
        self._gw_ws: Any | None = None
        self._gw_engine: Any | None = None
        self._gw_tracer: Any | None = None
        self._gw_oracle: Any | None = None
        self._gw_live: Any | None = None
        self._gw_crypto: Any | None = None
        self._gw_strings: Any | None = None
        self._gw_vm: Any | None = None
        self._gw_origin: Any | None = None
        self._gw_dataflow: Any | None = None
        self._gw_watcher: Any | None = None

    # ------------------------------------------------------------------ start
    @property
    def available(self) -> bool:
        try:
            import seleniumbase  # noqa: F401
            return True
        except ImportError:
            return False

    def start(self, headless: bool | None = None):
        with self.state_lock:
            # Self-healing: if the user (or a crash) closed the browser window,
            # drop the dead driver so the next call relaunches a fresh session
            # instead of failing forever with "no such window" errors.
            if self._driver is not None and not self._is_session_alive():
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None
            if self._driver is not None:
                return self._driver
            from seleniumbase import Driver
            if headless is None:
                headless = _default_headless()
            driver = Driver(uc=True, headless=headless, log_cdp_events=True)
            self._driver = driver

            try:
                driver.set_page_load_timeout(30)
            except Exception:
                pass
            self._enable_cdp_domains(driver)
            if self._blocked_urls:
                try:
                    driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": self._blocked_urls})
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).debug("setBlockedURLs failed: %s", e)
            self._ensure_drain_thread()
            return self._driver

    # --- CDP domain enabling -------------------------------------------------
    # NOTE: `Network.enable` is idempotent BUT its arguments are not sticky —
    # re-issuing it with `{}` silently resets maxTotalBufferSize /
    # maxResourceBufferSize back to Chrome's small defaults, which is why
    # response bodies used to vanish before they could be read.  Every
    # re-enable in this file must go through here so the generous buffer sizes
    # are always re-applied.
    NETWORK_ENABLE_ARGS = {
        "maxTotalBufferSize": 1_000_000_000,
        "maxResourceBufferSize": 200_000_000,
        "maxPostDataSize": 10_000_000,
    }

    def _enable_cdp_domains(self, driver=None) -> None:
        d = driver if driver is not None else self._driver
        if d is None:
            return
        import logging
        for cmd, args in (
            ("Network.enable", dict(self.NETWORK_ENABLE_ARGS)),
            ("Runtime.enable", {}),
            ("Log.enable", {}),
            ("Performance.enable", {}),
            ("DOM.enable", {}),
            ("Debugger.enable", {}),
            ("HeapProfiler.enable", {}),
        ):
            try:
                d.execute_cdp_cmd(cmd, args)
            except Exception as e:
                logging.getLogger(__name__).debug("CDP enable %s failed: %s", cmd, e)

    # --- background drain thread --------------------------------------------
    def _ensure_drain_thread(self) -> None:
        """Start the daemon that continuously drains Chrome's CDP log.

        Idempotent and safe to call from any tool entry point.
        """
        with self._drain_lock:
            if self._drain_thread is not None and self._drain_thread.is_alive():
                return
            self._drain_stop.clear()
            self._drain_stats["started_at"] = time.time()
            t = threading.Thread(
                target=self._drain_loop, name="samet-cdp-drain", daemon=True
            )
            self._drain_thread = t
            t.start()

    def _drain_loop(self) -> None:
        idle = 0
        while not self._drain_stop.is_set():
            if self._driver is None:
                # No browser yet (or it was reset) — wait for one instead of
                # burning the CPU or killing the thread permanently.
                if self._drain_stop.wait(self.DRAIN_IDLE_INTERVAL_SEC):
                    break
                continue
            try:
                got = self._drain_perf_log()
            except Exception as e:  # never let the daemon die
                self._drain_stats["errors"] += 1
                self._drain_stats["last_error"] = f"{type(e).__name__}: {e}"[:300]
                got = 0
            idle = idle + 1 if not got else 0
            delay = (
                self.DRAIN_IDLE_INTERVAL_SEC if idle >= 3 else self.DRAIN_INTERVAL_SEC
            )
            if self._drain_stop.wait(delay):
                break

    def stop_drain_thread(self) -> None:
        self._drain_stop.set()
        with self._drain_lock:
            t = self._drain_thread
            self._drain_thread = None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)

    def capture_status(self) -> dict:
        """Diagnostics for the capture pipeline — is it really listening?"""
        # Flush any staged sandbox records so the file the status reports
        # is not stale when the agent checks immediately after a browse.
        try:
            self._sandbox_flush()
        except Exception:
            pass
        with self._drain_lock:
            alive = bool(self._drain_thread is not None and self._drain_thread.is_alive())
        st = dict(self._drain_stats)
        last = st.get("last_drain_ts") or 0.0
        try:
            sandbox_log = str(self._sandbox_path())
        except Exception:
            sandbox_log = None
        return {
            "recording": bool(self._network_recording),
            "drain_thread_alive": alive,
            "drain_interval_sec": self.DRAIN_INTERVAL_SEC,
            "driver_started": self._driver is not None,
            "captured_requests": len(self.requests),
            "captured_ws_connections": len(self._ws_connections),
            "captured_ws_messages": len(self._ws_messages),
            "console_logs": len(self.console_logs),
            "page_errors": len(self.page_errors),
            "seconds_since_last_drain": round(time.time() - last, 2) if last else None,
            "persist_file": str(self._persist_path) if self._persist_path else None,
            "persist_enabled": bool(self.PERSIST_ENABLED),
            "sandbox_log": sandbox_log,
            "sandbox_pending": int(self._sandbox_pending),
            "sandbox_records": len(self._sandbox_records),
            "limits": {
                "max_requests": self.MAX_NETWORK_REQUESTS,
                "max_body_chars": self.MAX_BODY_CHARS,
                "bodies_per_drain": self.BODIES_PER_DRAIN,
            },
            "stats": {
                k: st[k] for k in
                ("cycles", "events", "dropped_events", "errors",
                 "bodies_ok", "bodies_failed", "last_error")
            },
        }

    def _is_session_alive(self) -> bool:
        """Return True when the driver has a live Chrome session."""
        if self._driver is None:
            return False
        try:
            _ = self._driver.current_url
            return True
        except Exception:
            return False

    def reset(self):
        """Drop a broken driver so the next call starts fresh.

        Captured network traffic is deliberately KEPT: reset() runs as a
        self-heal when the window dies mid-task, and wiping the evidence at
        exactly that moment is how "it was listening but recorded nothing"
        happened.  Use clear_capture() to drop traffic on purpose.
        """
        # eb5c03bb hardening — preserve authenticated session before discarding
        # the driver so a reset/open fallback never silently loses cookies.
        try:
            self.auto_save_session()
        except Exception:
            pass
        with self.state_lock:
            # Last chance to pull whatever Chrome still has buffered.
            try:
                self._drain_perf_log()
            except Exception:
                pass
            self._gw_cleanup_locked()
            if self._driver is not None:
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None
            self.last_attrs.clear()
            self._current_frame = None
            self._persist_flush()

    _CLOSE_GUARD_SEC = 30.0

    def close(self, force: bool = False) -> str:
        """Close the browser session.

        Guard: if the last browser interaction was < _CLOSE_GUARD_SEC ago, a
        plain close() is refused — the caller must pass force=True.  This
        prevents the coding fallback path from silently killing a live browser
        (the eb5c03bb regression: session lost to chrome://new-tab-page).
        """
        last = float(getattr(self, "_last_action_ts", 0.0) or 0.0)
        if not force and last and (time.time() - last) < self._CLOSE_GUARD_SEC:
            return (
                "REFUSED: browser was used "
                f"{time.time() - last:.1f}s ago — pass force=true to close anyway. "
                "Refusing to kill a live authenticated session on the coding fallback path."
            )
        self.stop_drain_thread()
        self.reset()
        self._persist_close()
        try:
            self._gw_close_ws()
        except Exception:
            pass
        return "OK: browser session closed"

    def driver_or_none(self):
        return self._driver

    # ================================================================ NAVIGATION
    def open(self, url: str):
        """Open URL — returns driver (backward compat for browser_server).
        Only resets the session on hard driver failures; transient page errors
        raise RuntimeError without destroying cookies/localStorage.
        """
        d = self.start()
        # Re-assert the domains WITH their buffer sizes.  The old code called
        # `Network.enable` with `{}` here, which reset the buffers set in
        # start() and made response bodies evaporate.
        self._enable_cdp_domains(d)
        self._ensure_drain_thread()
        try:
            d.get(url)
        except Exception as e:
            msg = str(e).split("\n")[0][:200]
            # Only hard-reset if the driver itself is dead (invalid session)
            if not self._is_session_alive():
                self.reset()
                raise RuntimeError(f"Browser session died navigating to {url}: {msg}")
            raise RuntimeError(f"Sayfa yuklenemedi ({url}): {msg}")
        cur = (d.current_url or "").lower()
        # Hard error pages — Chrome's internal error pages, not SPA "loading" states
        if "chrome-error://" in cur or "chromewebdata" in cur:
            raise RuntimeError(f"Sayfa acilamadi (baglanti/DNS hatasi): {url}")
        return d

    def goto(self, url: str) -> str:
        d = self.open(url)
        self._record_action("goto", url=url)
        return json.dumps({"url": d.current_url, "title": d.title})

    def back(self) -> str:
        d = self.start()
        d.back()
        return json.dumps({"url": d.current_url, "title": d.title})

    def forward(self) -> str:
        d = self.start()
        d.forward()
        return json.dumps({"url": d.current_url, "title": d.title})

    def reload(self) -> str:
        d = self.start()
        d.refresh()
        return json.dumps({"url": d.current_url, "title": d.title})

    def get_url(self) -> str:
        return self.start().current_url or ""

    def get_title(self) -> str:
        return self.start().title or ""

    def wait_for_url(self, pattern: str, timeout: float = 10.0) -> str:
        d = self.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = d.current_url or ""
            if pattern in cur or fnmatch.fnmatch(cur, pattern):
                return cur
            time.sleep(0.3)
        raise TimeoutError(f"URL '{pattern}' beklendi ama gelmedi. Mevcut: {d.current_url}")

    def wait_for_load_state(self, state: str = "load", timeout: float = 15.0) -> str:
        d = self.start()
        states = {
            "load": "return document.readyState === 'complete'",
            "domcontentloaded": "return document.readyState !== 'loading'",
            "networkidle": "return document.readyState === 'complete'",
        }
        script = states.get(state, states["load"])
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if d.execute_script(script):
                    return f"OK: load state '{state}' reached"
            except Exception:
                pass
            time.sleep(0.3)
        raise TimeoutError(f"Load state '{state}' timeout")

    def navigation_history(self) -> dict:
        d = self.start()
        res = d.execute_cdp_cmd("Page.getNavigationHistory", {})
        return {
            "index": res.get("currentIndex"),
            "entries": [
                {"id": e.get("id"), "url": e.get("url"), "title": e.get("title")}
                for e in (res.get("entries") or [])[-10:]
            ],
        }

    # ============================================================= ELEMENT FINDING
    def _current_ctx(self):
        return self._current_frame if self._current_frame is not None else self.start()

    def _resolve_selector(self, index: int | str | None, selector: str) -> str:
        """Resolve (index, selector) to a CSS selector.

        Handles integer indices (e.g. 14), string indices ('14'),
        mcp-prefixed indices ('mcp14', '[mcp14]'), and raw CSS selectors.
        """
        if index is not None:
            sid = str(index).strip()
            if sid.startswith("[") and sid.endswith("]"):
                sid = sid[1:-1].strip()
            if sid.isdigit():
                return f'[data-mcp-el-id="{sid}"], [data-mcp-el-id="mcp{sid}"]'
            elif sid.startswith("mcp"):
                raw_num = sid[3:]
                return f'[data-mcp-el-id="{sid}"], [data-mcp-el-id="{raw_num}"]'
            return f'[data-mcp-el-id="{sid}"]'
        if not selector:
            raise RuntimeError("selector veya index gerekli.")
        return selector

    def find(self, selector: str) -> dict | None:
        try:
            el = self._current_ctx().find_element("css selector", selector)
            return self._el_info(el, selector)
        except Exception:
            return None

    def find_all(self, selector: str) -> list[dict]:
        els = self._current_ctx().find_elements("css selector", selector)
        return [self._el_info(el, selector) for el in els[:50]]

    def find_by_text(self, text: str) -> dict | None:
        # XPath-escape: replace " with concat trick to avoid injection
        safe = _xpath_escape(text)
        xp = f'//*[normalize-space(text())={safe} or contains(.,{safe})]'
        try:
            el = self._current_ctx().find_element("xpath", xp)
            return self._el_info(el, f'text={text!r}')
        except Exception:
            return None

    def find_by_role(self, role: str, name: str | None = None) -> dict | None:
        sel = f'[role="{role}"]'
        els = self._current_ctx().find_elements("css selector", sel)
        for el in els:
            if name is None:
                return self._el_info(el, sel)
            lbl = (el.get_attribute("aria-label") or el.text or "").strip()
            if name.lower() in lbl.lower():
                return self._el_info(el, sel)
        return None

    def find_by_label(self, label: str) -> dict | None:
        ctx = self._current_ctx()
        safe = _xpath_escape(label)
        try:
            lbl_el = ctx.find_element(
                "xpath",
                f'//label[normalize-space(.)={safe} or contains(.,{safe})]'
            )
            for_id = lbl_el.get_attribute("for")
            if for_id:
                el = ctx.find_element("css selector", f"#{for_id}")
                return self._el_info(el, f"#{for_id}")
            el = lbl_el.find_element("css selector", "input,select,textarea")
            return self._el_info(el, f'label:{label!r}')
        except Exception:
            return None

    def find_by_placeholder(self, text: str) -> dict | None:
        ctx = self._current_ctx()
        # CSS-safe attribute selector: use contains via xpath for safety
        try:
            safe = _xpath_escape(text)
            el = ctx.find_element("xpath", f'//*[@placeholder={safe}]')
            return self._el_info(el, f'[placeholder={text!r}]')
        except Exception:
            for el in ctx.find_elements("css selector", "[placeholder]"):
                ph = el.get_attribute("placeholder") or ""
                if text.lower() in ph.lower():
                    return self._el_info(el, f'[placeholder~={text!r}]')
            return None

    def _el_info(self, el, selector: str) -> dict:
        try:
            r = el.rect
        except Exception:
            r = {}
        return {
            "selector": selector,
            "tag": el.tag_name,
            "id": el.get_attribute("id") or "",
            "name": el.get_attribute("name") or "",
            "type": el.get_attribute("type") or "",
            "placeholder": el.get_attribute("placeholder") or "",
            "aria_label": el.get_attribute("aria-label") or "",
            "role": el.get_attribute("role") or el.tag_name,
            "text": (el.text or "")[:80],
            "value": el.get_attribute("value") or "",
            "disabled": el.get_attribute("disabled") is not None,
            "visible": el.is_displayed(),
            "enabled": el.is_enabled(),
            "rect": r,
        }

    # ============================================================= ELEMENT INFO
    def get_element_detail(self, selector: str = "", index: int | None = None) -> dict:
        sel = self._resolve_selector(index, selector)
        el = self._current_ctx().find_element("css selector", sel)
        return self._el_info(el, sel)

    def elements(self) -> list[dict]:
        """Numbered inventory of interactive elements with full metadata."""
        d = self.start()
        rows = d.execute_script(f"return ({_ELEMENTS_JS.strip()});") or []
        # Store stable-id → stable-id mapping (the index is now the string sid)
        self.last_attrs = {r["i"]: r["i"] for r in rows}
        return rows

    def body_text(self, d, limit: int = 4000) -> str:
        el = d.find_element("css selector", "body")
        return (el.text or "")[:limit]

    def get_dom(self, selector: str | None = None) -> str:
        d = self.start()
        if selector:
            el = d.find_element("css selector", selector)
            return el.get_attribute("outerHTML") or ""
        return d.execute_script("return document.documentElement.outerHTML;") or ""

    def get_text(self, selector: str | None = None) -> str:
        d = self.start()
        if selector:
            el = d.find_element("css selector", selector)
            return el.text or ""
        body = d.find_element("css selector", "body")
        return (body.text or "")[:8000]

    def get_attribute(self, selector: str, attribute: str) -> str | None:
        el = self._current_ctx().find_element("css selector", selector)
        return el.get_attribute(attribute)

    def is_visible(self, selector: str) -> bool:
        try:
            return self.start().find_element("css selector", selector).is_displayed()
        except Exception:
            return False

    def is_enabled(self, selector: str) -> bool:
        try:
            return self.start().find_element("css selector", selector).is_enabled()
        except Exception:
            return False

    def is_checked(self, selector: str) -> bool:
        try:
            return self.start().find_element("css selector", selector).is_selected()
        except Exception:
            return False

    def get_input_value(self, selector: str = "", index: int | None = None) -> str:
        sel = self._resolve_selector(index, selector)
        el = self.start().find_element("css selector", sel)
        return el.get_attribute("value") or ""

    def get_outer_html(self, selector: str) -> str:
        el = self.start().find_element("css selector", selector)
        return el.get_attribute("outerHTML") or ""

    def get_inner_html(self, selector: str) -> str:
        el = self.start().find_element("css selector", selector)
        return el.get_attribute("innerHTML") or ""

    # ================================================================== MOUSE
    def click(self, selector: str = "", index: int | None = None):
        sel = self._resolve_selector(index, selector)
        d = self.start()
        el = d.find_element("css selector", sel)
        try:
            # Scroll into center before clicking to avoid fixed header/footer occlusion
            d.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center',inline:'center'});", el)
        except Exception:
            pass
        try:
            el.click()
        except Exception:
            # Fallback: synthetic pointer/mouse event dispatch. NOTE: we dispatch
            # exactly one 'click' event — adding another el.click() here caused
            # double-fire (double submits / double navigations).
            d.execute_script(
                """
                var el = arguments[0];
                try { el.focus(); } catch(e){}
                var opts = { bubbles: true, cancelable: true, composed: true, view: window };
                el.dispatchEvent(new PointerEvent('pointerdown', opts));
                el.dispatchEvent(new MouseEvent('mousedown', opts));
                el.dispatchEvent(new PointerEvent('pointerup', opts));
                el.dispatchEvent(new MouseEvent('mouseup', opts));
                el.dispatchEvent(new MouseEvent('click', opts));
                """,
                el,
            )
        self._record_action("click", selector=sel)

    def double_click(self, selector: str = "", index: int | None = None):
        from selenium.webdriver.common.action_chains import ActionChains
        sel = self._resolve_selector(index, selector)
        d = self.start()
        ActionChains(d).double_click(d.find_element("css selector", sel)).perform()

    def right_click(self, selector: str = "", index: int | None = None):
        from selenium.webdriver.common.action_chains import ActionChains
        sel = self._resolve_selector(index, selector)
        d = self.start()
        ActionChains(d).context_click(d.find_element("css selector", sel)).perform()

    def hover(self, selector: str = "", index: int | None = None):
        from selenium.webdriver.common.action_chains import ActionChains
        sel = self._resolve_selector(index, selector)
        d = self.start()
        ActionChains(d).move_to_element(d.find_element("css selector", sel)).perform()

    def move_mouse(self, x: int, y: int):
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(self.start()).move_by_offset(x, y).perform()

    def mouse_down(self):
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(self.start()).click_and_hold().perform()

    def mouse_up(self):
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(self.start()).release().perform()

    def drag(self, source_selector: str, target_selector: str):
        from selenium.webdriver.common.action_chains import ActionChains
        d = self.start()
        src = d.find_element("css selector", source_selector)
        tgt = d.find_element("css selector", target_selector)
        ActionChains(d).drag_and_drop(src, tgt).perform()

    def scroll(self, x: int = 0, y: int = 500):
        self.start().execute_script(f"window.scrollBy({x}, {y});")

    def scroll_to(self, selector: str = "", index: int | None = None):
        sel = self._resolve_selector(index, selector)
        d = self.start()
        el = d.find_element("css selector", sel)
        d.execute_script("arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", el)

    # ================================================================ KEYBOARD

    def _organic_type(self, el, text: str, min_delay: float = 0.04, max_delay: float = 0.18) -> None:
        """Type text character-by-character with human-like random intervals.

        - No JS value injection, no clipboard paste, no dispatchEvent.
        - Each character is sent via WebDriver send_keys exactly as a real keyboard event.
        - Occasionally adds micro-pauses to simulate thinking, muscle-memory bursts, and corrections.
        - Works with Selenium UC mode — every keystroke arrives as a genuine OS input event.
        """
        from selenium.webdriver.common.action_chains import ActionChains

        el.click()
        time.sleep(random.uniform(0.1, 0.3))

        i = 0
        burst_size = random.randint(2, 5)
        burst_count = 0

        while i < len(text):
            char = text[i]
            el.send_keys(char)
            burst_count += 1

            # Base per-character delay
            delay = random.uniform(min_delay, max_delay)

            # Burst mode: sometimes type a few chars faster (muscle memory)
            if burst_count >= burst_size:
                burst_count = 0
                burst_size = random.randint(2, 6)
                # Occasional micro-pause between bursts (0.1–0.5s)
                delay += random.uniform(0.08, 0.45)

            # After space or punctuation: slightly longer pause (natural rhythm)
            if char in (' ', '.', ',', '!', '@', '-', '_'):
                delay += random.uniform(0.05, 0.25)

            # Very rare: longer pause (simulates distraction or thinking)
            if random.random() < 0.04:
                delay += random.uniform(0.4, 1.2)

            time.sleep(delay)
            i += 1

    def _chunked_type(self, el, text: str, min_chunk: int = 3, max_chunk: int = 10) -> None:
        """Type in small chunks with brief pauses — fast enough for long texts
        while still sending genuine keyboard events (no JS injection)."""
        i = 0
        while i < len(text):
            n = random.randint(min_chunk, max_chunk)
            el.send_keys(text[i:i + n])
            time.sleep(random.uniform(0.01, 0.06))
            i += n

    def _clear_field(self, el) -> None:
        """Select-all + delete existing content via real keyboard events."""
        from selenium.webdriver.common.keys import Keys

        select_all = Keys.CONTROL  # Windows/Linux; macOS handled below
        try:
            import platform
            if platform.system() == "Darwin":
                select_all = Keys.COMMAND
        except Exception:
            pass
        el.send_keys(select_all, "a")
        time.sleep(random.uniform(0.05, 0.12))
        el.send_keys(Keys.DELETE)
        time.sleep(random.uniform(0.05, 0.15))

    def type_text(self, selector: str = "", text: str = "", index: int | None = None):
        """Human-like typing: sends each character individually via send_keys
        with randomized delays. No JS injection, no clipboard paste.
        For long texts (>120 chars) automatically switches to fast chunked
        typing so tool calls do not time out."""
        sel = self._resolve_selector(index, selector)
        d = self.start()
        el = d.find_element("css selector", sel)

        # Scroll element into view first (native scroll, not JS)
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(d).move_to_element(el).perform()
        time.sleep(random.uniform(0.1, 0.25))

        self._clear_field(el)

        if len(text) > 120:
            self._chunked_type(el, text)
        else:
            self._organic_type(el, text)

    def fill(self, selector: str = "", text: str = "", index: int | None = None):
        """Fast fill using genuine WebDriver keyboard events (no JS value
        injection): clears the field, then sends the whole string in one
        send_keys call — every keystroke still arrives as a real input event,
        but without per-character sleeps. Use this for normal form inputs;
        use `type` when maximum human-likeness matters (short fields)."""
        sel = self._resolve_selector(index, selector)
        d = self.start()
        el = d.find_element("css selector", sel)

        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(d).move_to_element(el).perform()
        time.sleep(random.uniform(0.05, 0.15))

        self._clear_field(el)
        el.send_keys(text)
        time.sleep(random.uniform(0.1, 0.25))
        self._record_action("fill", selector=sel, length=len(text))



    def clear_input(self, selector: str = "", index: int | None = None):
        sel = self._resolve_selector(index, selector)
        self.start().find_element("css selector", sel).clear()

    def press_key(self, key: str, selector: str = "", index: int | None = None):
        from selenium.webdriver.common.keys import Keys
        key_map = {k.lower(): v for k, v in vars(Keys).items() if not k.startswith("_")}
        k = key_map.get(key.lower(), key)
        d = self.start()
        if selector or index is not None:
            sel = self._resolve_selector(index, selector)
            d.find_element("css selector", sel).send_keys(k)
        else:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(d).send_keys(k).perform()

    def key_down(self, key: str):
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(self.start()).key_down(key).perform()

    def key_up(self, key: str):
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(self.start()).key_up(key).perform()

    def hotkey(self, *keys: str):
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys
        key_map = {k.lower(): v for k, v in vars(Keys).items() if not k.startswith("_")}
        resolved = [key_map.get(k.lower(), k) for k in keys]
        ac = ActionChains(self.start())
        for k in resolved[:-1]:
            ac.key_down(k)
        ac.send_keys(resolved[-1])
        for k in reversed(resolved[:-1]):
            ac.key_up(k)
        ac.perform()

    def focus(self, selector: str = "", index: int | None = None):
        sel = self._resolve_selector(index, selector)
        d = self.start()
        el = d.find_element("css selector", sel)
        d.execute_script("arguments[0].focus();", el)

    # ================================================================== FORM
    def select_option(self, selector: str = "", value: str = "", by: str = "auto", index: int | None = None) -> str:
        """Select an option from a <select> element.
        by: 'auto' (value then visible_text), 'value', 'text', or 'index'.
        """
        from selenium.webdriver.support.ui import Select
        sel = self._resolve_selector(index, selector)
        el = self.start().find_element("css selector", sel)
        s = Select(el)
        if by == "value":
            s.select_by_value(value)
            return f"Selected by value='{value}'"
        elif by == "text":
            s.select_by_visible_text(value)
            return f"Selected by text='{value}'"
        elif by == "index":
            s.select_by_index(int(value))
            return f"Selected by index={value}"
        else:  # auto
            try:
                s.select_by_value(value)
                return f"Selected by value='{value}'"
            except Exception:
                try:
                    s.select_by_visible_text(value)
                    return f"Selected by text='{value}'"
                except Exception as e:
                    raise RuntimeError(f"Could not select option '{value}' in '{sel}' (tried value and text): {e}")

    def check_box(self, selector: str = "", index: int | None = None, checked: bool = True):
        """Check or uncheck a checkbox. Handles visually-hidden custom inputs by targeting associated label."""
        sel = self._resolve_selector(index, selector)
        d = self.start()
        el = d.find_element("css selector", sel)
        if el.is_selected() != checked:
            try:
                if el.is_displayed():
                    el.click()
                else:
                    raise Exception("Element not directly visible")
            except Exception:
                d.execute_script(
                    """
                    var el = arguments[0];
                    var want = arguments[1];
                    var target = el;
                    if (el.id) {
                        var lbl = document.querySelector('label[for="' + el.id + '"]');
                        if (lbl) target = lbl;
                    }
                    if (target === el) {
                        var parentLbl = el.closest('label');
                        if (parentLbl) target = parentLbl;
                    }
                    if (target !== el) {
                        target.click();
                    } else {
                        el.checked = want;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    """,
                    el, checked,
                )
        self._record_action("check_box", selector=sel, checked=checked)

    def upload_file(self, selector: str, path: str, index: int | None = None):
        sel = self._resolve_selector(index, selector)
        self.start().find_element("css selector", sel).send_keys(str(Path(path).resolve()))

    def submit_form(self, selector: str = "", index: int | None = None):
        """Submit form using requestSubmit() / bubbling submit event so SPA handlers trigger."""
        sel = self._resolve_selector(index, selector)
        d = self.start()
        el = d.find_element("css selector", sel)
        d.execute_script(
            """
            var el = arguments[0];
            var form = el.form || (el.tagName === 'FORM' ? el : el.closest('form'));
            if (form) {
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                    return;
                }
                var btn = form.querySelector('button[type=submit], input[type=submit]');
                if (btn) {
                    btn.click();
                    return;
                }
                var event = new Event('submit', { bubbles: true, cancelable: true });
                var notCancelled = form.dispatchEvent(event);
                if (notCancelled) form.submit();
            } else {
                el.click();
            }
            """,
            el,
        )


    # ============================================================== SCREENSHOT
    def screenshot(self, path: str):
        if not self.start().get_screenshot_as_file(path):
            raise RuntimeError("screenshot failed")

    def screenshot_element(self, selector: str, path: str, index: int | None = None):
        sel = self._resolve_selector(index, selector)
        self.start().find_element("css selector", sel).screenshot(path)

    def screenshot_full_page(self, path: str):
        d = self.start()
        orig_size = d.get_window_size()
        orig_h = d.execute_script("return document.body.scrollHeight;")
        d.set_window_size(orig_size["width"], orig_h)
        if not d.get_screenshot_as_file(path):
            raise RuntimeError("full-page screenshot failed")
        d.set_window_size(orig_size["width"], orig_size["height"])

    # ================================================================ VIEWPORT
    def get_viewport(self) -> dict:
        sz = self.start().get_window_size()
        return {"width": sz["width"], "height": sz["height"]}

    def set_viewport(self, width: int, height: int):
        self.start().set_window_size(width, height)

    # ===================================================================== JS
    def evaluate(self, script: str) -> Any:
        d = self.start()
        if not script.strip().startswith("return"):
            script = "return " + script
        return d.execute_script(script)

    def evaluate_on_element(self, selector: str, script: str, index: int | None = None) -> Any:
        sel = self._resolve_selector(index, selector)
        d = self.start()
        el = d.find_element("css selector", sel)
        if not script.strip().startswith("return"):
            script = "return " + script
        return d.execute_script(script, el)

    # ==================================================================== WAIT
    def wait_sec(self, seconds: float):
        time.sleep(seconds)

    def wait_for_selector(self, selector: str, timeout: float = 10.0) -> str:
        d = self.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if d.find_element("css selector", selector):
                    return f"OK: {selector} found"
            except Exception:
                pass
            time.sleep(0.3)
        raise TimeoutError(f"Selector '{selector}' timeout ({timeout}s)")

    def wait_for_visible(self, selector: str, timeout: float = 10.0) -> str:
        d = self.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if d.find_element("css selector", selector).is_displayed():
                    return f"OK: {selector} visible"
            except Exception:
                pass
            time.sleep(0.3)
        raise TimeoutError(f"'{selector}' visible timeout ({timeout}s)")

    def wait_for_hidden(self, selector: str, timeout: float = 10.0) -> str:
        d = self.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                el = d.find_element("css selector", selector)
                if not el.is_displayed():
                    return f"OK: {selector} hidden"
            except Exception:
                return f"OK: {selector} not in DOM"
            time.sleep(0.3)
        raise TimeoutError(f"'{selector}' hidden timeout ({timeout}s)")

    def wait_for_text_on_page(self, text: str, timeout: float = 10.0) -> str:
        d = self.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            body = d.find_element("css selector", "body").text or ""
            if text in body:
                return f"OK: text '{text}' found on page"
            time.sleep(0.4)
        raise TimeoutError(f"Text '{text}' not found after {timeout}s")

    def wait_for_network_idle(self, timeout: float = 10.0, idle_ms: int = 500) -> str:
        self.wait_for_load_state("load", timeout)
        time.sleep(idle_ms / 1000)
        return "OK: network idle"

    def wait_for_function(self, script: str, timeout: float = 10.0) -> str:
        d = self.start()
        if not script.strip().startswith("return"):
            script = "return " + script
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if d.execute_script(script):
                    return "OK: condition true"
            except Exception:
                pass
            time.sleep(0.3)
        raise TimeoutError(f"JS condition timeout ({timeout}s): {script[:80]}")

    def wait_request(self, pattern: str, timeout_s: float = 10.0) -> dict | None:
        low = pattern.lower()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._drain_perf_log()
            for r in reversed(list(self.requests.values())):
                if fnmatch.fnmatch(r.get("url", "").lower(), low) or low in r.get("url", "").lower():
                    return r
            time.sleep(0.3)
        return None

    # ==================================================================== TABS
    def new_tab(self, url: str | None = None) -> str:
        d = self.start()
        d.execute_script("window.open(arguments[0]);", url or "about:blank")
        d.switch_to.window(d.window_handles[-1])
        if url and url != "about:blank":
            d.get(url)
        return json.dumps({"handle": d.current_window_handle, "url": d.current_url})

    def close_tab(self):
        d = self.start()
        d.close()
        if d.window_handles:
            d.switch_to.window(d.window_handles[-1])

    def list_tabs(self) -> list[dict]:
        d = self.start()
        current = d.current_window_handle
        out = []
        for i, h in enumerate(d.window_handles):
            d.switch_to.window(h)
            out.append({
                "index": i, "handle": h,
                "url": d.current_url, "title": d.title,
                "active": h == current,
            })
        d.switch_to.window(current)
        return out

    def switch_tab(self, index: int | None = None, handle: str | None = None) -> str:
        d = self.start()
        if handle:
            d.switch_to.window(handle)
        elif index is not None:
            d.switch_to.window(d.window_handles[index])
        else:
            raise RuntimeError("switch_tab: index veya handle gerekli")
        return json.dumps({"url": d.current_url, "title": d.title})

    def get_active_tab(self) -> dict:
        d = self.start()
        return {"handle": d.current_window_handle, "url": d.current_url, "title": d.title}

    # ================================================================== FRAMES
    def list_frames(self) -> list[dict]:
        d = self.start()
        return [
            {
                "index": i,
                "id": el.get_attribute("id") or "",
                "name": el.get_attribute("name") or "",
                "src": el.get_attribute("src") or "",
            }
            for i, el in enumerate(d.find_elements("css selector", "iframe, frame"))
        ]

    def switch_frame(self, selector: str):
        d = self.start()
        el = d.find_element("css selector", selector)
        d.switch_to.frame(el)
        self._current_frame = d

    def switch_main_frame(self):
        self.start().switch_to.default_content()
        self._current_frame = None

    def frame_find(self, frame_selector: str, element_selector: str) -> dict | None:
        self.switch_frame(frame_selector)
        try:
            return self.find(element_selector)
        finally:
            self.switch_main_frame()

    def frame_click(self, frame_selector: str, element_selector: str):
        self.switch_frame(frame_selector)
        try:
            self.click(element_selector)
        finally:
            self.switch_main_frame()

    def frame_type(self, frame_selector: str, element_selector: str, text: str):
        self.switch_frame(frame_selector)
        try:
            self.type_text(element_selector, text)
        finally:
            self.switch_main_frame()

    # ================================================================= DIALOG
    def accept_dialog(self) -> str:
        d = self.start()
        alert = d.switch_to.alert
        txt = getattr(alert, "text", "")
        alert.accept()
        return f"accepted: {txt[:200]}"

    def dismiss_dialog(self) -> str:
        d = self.start()
        alert = d.switch_to.alert
        txt = getattr(alert, "text", "")
        alert.dismiss()
        return f"dismissed: {txt[:200]}"

    def get_dialog(self) -> dict | None:
        try:
            alert = self.start().switch_to.alert
            return {"text": getattr(alert, "text", "")}
        except Exception:
            return None

    def dialog(self, action: str) -> str:
        """Backward compat."""
        return self.accept_dialog() if action == "accept" else self.dismiss_dialog()

    # ================================================================ COOKIES
    def get_cookies(self) -> list[dict]:
        return self.start().get_cookies()

    def set_cookie(self, name: str, value: str, domain: str | None = None, path: str = "/"):
        cookie: dict = {"name": name, "value": value, "path": path}
        if domain:
            cookie["domain"] = domain
        self.start().add_cookie(cookie)

    def delete_cookie(self, name: str):
        self.start().delete_cookie(name)

    def clear_cookies(self):
        self.start().delete_all_cookies()

    def export_cookies(self, path: str) -> int:
        d = self.start()
        cookies = d.get_cookies()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=1)
        return len(cookies)

    # ================================================================ STORAGE
    def storage_op(self, kind: str, op: str, key: str = "", value: str = "") -> str:
        d = self.start()
        store = "localStorage" if kind == "local" else "sessionStorage"
        if op == "get":
            return d.execute_script(f"return JSON.stringify({store});")
        if op == "get_item":
            return d.execute_script(f"return {store}.getItem(arguments[0]);", key)
        if op == "set_item":
            d.execute_script(f"{store}.setItem(arguments[0], arguments[1]);", key, value)
            return f"OK: {kind}Storage['{key}'] set"
        if op == "remove_item":
            d.execute_script(f"{store}.removeItem(arguments[0]);", key)
            return f"OK: removed '{key}'"
        return f"ERROR: unknown op '{op}'"

    def get_local_storage(self) -> dict:
        try:
            return json.loads(self.storage_op("local", "get") or "{}")
        except Exception:
            return {}

    def get_local_storage_item(self, key: str) -> str | None:
        return self.storage_op("local", "get_item", key=key)

    def set_local_storage_item(self, key: str, value: str) -> str:
        return self.storage_op("local", "set_item", key=key, value=value)

    def remove_local_storage_item(self, key: str) -> str:
        return self.storage_op("local", "remove_item", key=key)

    def get_session_storage(self) -> dict:
        try:
            return json.loads(self.storage_op("session", "get") or "{}")
        except Exception:
            return {}

    def set_session_storage_item(self, key: str, value: str) -> str:
        return self.storage_op("session", "set_item", key=key, value=value)

    # ================================================================ NETWORK
    def _drain_perf_log(self) -> int:  # noqa: C901
        """Drain Chrome's CDP performance log into our capture buffers.

        Returns the number of raw CDP events consumed.  This is a DESTRUCTIVE
        read of a bounded buffer, so it runs on a background daemon (see
        _drain_loop) as well as from every getter — whatever is not read in
        time is discarded by Chrome and cannot be recovered.
        """
        d = self.driver_or_none()
        if d is None:
            return 0
        # Serialise drains: two threads reading the same destructive buffer
        # would interleave and shred the event stream.
        if not self._drain_lock.acquire(blocking=False):
            return 0
        try:
            try:
                entries = d.get_log("performance")
            except Exception as e:
                self._drain_stats["errors"] += 1
                self._drain_stats["last_error"] = f"{type(e).__name__}: {e}"[:300]
                return 0
            self._drain_stats["cycles"] += 1
            self._drain_stats["last_drain_ts"] = time.time()
            self._drain_stats["events"] += len(entries)
            if not self._network_recording:
                # Explicitly paused by stop_network_recording(): we still have
                # to drain (the buffer is destructive and shared with console/
                # error capture) but network records are not stored.
                self._drain_stats["dropped_events"] += len(entries)
                self._consume_entries(d, entries, record_network=False)
                return len(entries)
            self._consume_entries(d, entries, record_network=True)
            return len(entries)
        finally:
            self._drain_lock.release()

    def _consume_entries(self, d, entries, record_network: bool = True) -> None:  # noqa: C901
        bodies_fetched = 0  # cap CDP getResponseBody calls per drain cycle
        for e in entries:
            try:
                msg = json.loads(e["message"])["message"]
            except Exception:
                continue
            method, params = msg.get("method"), msg.get("params", {})

            # Console logs with FIFO limit
            if method == "Runtime.consoleAPICalled":
                args = " ".join(
                    str(a.get("value", a.get("description", "")))[:200]
                    for a in params.get("args", [])
                )
                self.console_logs.append({"type": params.get("type", "log"), "text": args[:500]})
                # FIFO cleanup: keep only most recent MAX_CONSOLE_LOGS entries
                if len(self.console_logs) > self.MAX_CONSOLE_LOGS:
                    self.console_logs = self.console_logs[-self.MAX_CONSOLE_LOGS:]
                continue

            # JS exceptions with FIFO limit
            if method == "Runtime.exceptionThrown":
                det = params.get("exceptionDetails", {})
                ex = det.get("exception", {})
                self.page_errors.append({
                    "text": str(
                        ex.get("description") or ex.get("value") or det.get("text", "")
                    )[:400],
                    "line": det.get("lineNumber"),
                })
                # FIFO cleanup: keep only most recent MAX_PAGE_ERRORS entries
                if len(self.page_errors) > self.MAX_PAGE_ERRORS:
                    self.page_errors = self.page_errors[-self.MAX_PAGE_ERRORS:]
                continue

            rid = params.get("requestId", "")
            if not rid:
                continue
            if not record_network:
                continue

            if method == "Network.requestWillBeSent":
                req = params.get("request", {})
                # ----------------------------------------------------------------
                # Capture filter: HTTP requests (XHR/fetch), JavaScript,
                # WebSockets, documents and SSE streams are recorded.  Images,
                # media segments, fonts and CSS are dropped at capture time —
                # they bloat the buffer, evict valuable POST evidence via FIFO
                # and are noise for the agent when writing API clients.
                # ----------------------------------------------------------------
                rec_type = _capture_record_type(params.get("type", ""))
                if rec_type is None:
                    continue
                # Track new request in order list
                if rid not in self.requests:
                    self._request_order.append(rid)

                rec = self.requests.setdefault(rid, {"id": rid})
                url = req.get("url", "")
                now_ts = time.time()
                time_str = datetime.datetime.now().strftime("%H:%M:%S")
                rec.update(
                    id=rid,
                    url=url,
                    method=req.get("method", "GET"),
                    headers=req.get("headers", {}),
                    query=url.split("?", 1)[-1] if "?" in url else "",
                    post_data=req.get("postData") or rec.get("post_data") or "",
                    post_data_truncated=bool(req.get("hasPostData")
                                             and not req.get("postData")),
                    resource_type=params.get("type", ""),
                    type=rec_type,
                    timestamp=params.get("timestamp", 0),
                    wall_time=now_ts,
                    time_str=time_str,
                    frame_id=params.get("frameId", ""),
                    document_url=params.get("documentURL", ""),
                    initiator=(params.get("initiator") or {}).get("type", ""),
                    response_status=rec.get("response_status"),
                    response_headers=rec.get("response_headers") or {},
                    error=rec.get("error"),
                )
                # A redirect reuses the same requestId; keep the whole chain
                # instead of overwriting the original request.
                redirect = params.get("redirectResponse")
                if redirect:
                    chain = rec.setdefault("redirect_chain", [])
                    chain.append({
                        "url": redirect.get("url", ""),
                        "status": redirect.get("status"),
                        "location": (redirect.get("headers") or {}).get("location", ""),
                    })
                self._persist_record(rec)

                # FIFO cleanup: if we exceed MAX_NETWORK_REQUESTS, remove oldest.
                # Records are persisted to disk before eviction, so nothing is
                # actually lost — only the in-memory view is bounded.
                if len(self.requests) > self.MAX_NETWORK_REQUESTS:
                    cleanup_count = max(1, self.MAX_NETWORK_REQUESTS // 5)
                    victims = self._request_order[:cleanup_count]
                    self._request_order = self._request_order[cleanup_count:]
                    for victim_rid in victims:
                        self.requests.pop(victim_rid, None)

            elif method == "Network.requestWillBeSentExtraInfo":
                # Carries the REAL request headers (incl. Cookie) that the
                # renderer-side event above does not have.
                rec = self.requests.get(rid)
                if rec is None:
                    continue
                extra = params.get("headers") or {}
                if extra:
                    merged = dict(rec.get("headers") or {})
                    merged.update(extra)
                    rec["headers"] = merged
                    self._persist_record(rec)
            elif method == "Network.responseReceived":
                rec = self.requests.get(rid)
                if rec is None:
                    continue  # filtered out at capture time — keep it out
                resp = params.get("response", {})
                rec.setdefault("url", resp.get("url", ""))
                rec["response_status"] = resp.get("status")
                rec["response_headers"] = resp.get("headers", {})
                rec["mime"] = resp.get("mimeType", "")
                rec["remote_address"] = (
                    f"{resp.get('remoteIPAddress', '')}:{resp.get('remotePort', '')}"
                    if resp.get("remoteIPAddress") else ""
                )
                rec["from_cache"] = bool(resp.get("fromDiskCache")
                                         or resp.get("fromPrefetchCache"))
                self._persist_record(rec)
            elif method == "Network.loadingFailed":
                rec = self.requests.get(rid)
                if rec is None:
                    continue  # filtered out at capture time
                rec["error"] = params.get("errorText") or params.get("blockedReason") or "failed"
                self._persist_record(rec)
            elif method == "Network.loadingFinished":
                # Auto-capture the response BODY while Chrome still has it
                # buffered. Without this, bodies are unrecoverable once the
                # page navigates away or the window closes.
                rec = self.requests.get(rid)
                if rec is None or rec.get("response_body") is not None:
                    continue
                rec["encoded_data_length"] = params.get("encodedDataLength")
                if bodies_fetched < self.BODIES_PER_DRAIN:
                    bodies_fetched += 1
                    try:
                        body_resp = d.execute_cdp_cmd(
                            "Network.getResponseBody", {"requestId": rid}
                        )
                        raw = str(body_resp.get("body", "") or "")
                        b64 = bool(body_resp.get("base64Encoded"))
                        if len(raw) > self.MAX_BODY_CHARS:
                            raw = raw[: self.MAX_BODY_CHARS] + (
                                f"\n…[truncated at {self.MAX_BODY_CHARS} chars]"
                            )
                        rec["response_body"] = ("(base64) " if b64 else "") + raw
                        if b64:
                            rec["response_body_base64"] = True
                        self._drain_stats["bodies_ok"] += 1
                    except Exception as be:
                        # Body evicted from Chrome's buffer, opaque resource, or
                        # a redirect with no body.  Record WHY instead of
                        # silently pretending nothing happened — a missing body
                        # with no explanation is what made this look broken.
                        self._drain_stats["bodies_failed"] += 1
                        rec["response_body_error"] = f"{type(be).__name__}: {be}".split("\n")[0][:200]
                else:
                    rec["response_body_error"] = (
                        f"deferred: >{self.BODIES_PER_DRAIN} bodies in one drain cycle"
                    )
                self._persist_record(rec)
            elif method == "Network.webSocketCreated":
                self._ws_connections[rid] = {
                    "id": rid,
                    "url": params.get("url", ""),
                    "timestamp": params.get("timestamp", 0),
                    "closed": False,
                }
                # WebSocket connections also appear in the network list/dump
                # (type="wss") so the agent can discover them alongside HTTP.
                if rid not in self.requests:
                    self._request_order.append(rid)
                wrec = self.requests.setdefault(rid, {"id": rid})
                wrec.update(
                    id=rid,
                    url=params.get("url", ""),
                    method="WSS",
                    headers={},
                    post_data="",
                    resource_type="WebSocket",
                    type="wss",
                    timestamp=params.get("timestamp", 0),
                    wall_time=time.time(),
                    time_str=datetime.datetime.now().strftime("%H:%M:%S"),
                    response_status=None,
                    response_headers={},
                    error=None,
                    closed=False,
                )
                self._persist_record(wrec)
            elif method == "Network.webSocketHandshakeResponseReceived":
                resp = params.get("response", {}) or {}
                if rid in self._ws_connections:
                    self._ws_connections[rid]["status"] = resp.get("status")
                rec = self.requests.get(rid)
                if rec is not None:
                    rec["response_status"] = resp.get("status")
                    rec["response_headers"] = resp.get("headers", {}) or {}
                    self._persist_record(rec)
            elif method == "Network.webSocketClosed":
                if rid in self._ws_connections:
                    self._ws_connections[rid]["closed"] = True
                rec = self.requests.get(rid)
                if rec is not None:
                    rec["closed"] = True
                    self._persist_record(rec)
            elif method in ("Network.webSocketFrameReceived", "Network.webSocketFrameSent"):
                frame = params.get("response", {}) or params.get("request", {})
                direction = "recv" if "Received" in method else "sent"
                payload = str(frame.get("payloadData", "") or "")
                if len(payload) > self.WS_PAYLOAD_CHARS:
                    payload = payload[: self.WS_PAYLOAD_CHARS] + "…[truncated]"
                msg_rec = {
                    "type": "wss",
                    "connection_id": rid,
                    "direction": direction,
                    "payload": payload,
                    "timestamp": params.get("timestamp", 0),
                    "time_str": datetime.datetime.now().strftime("%H:%M:%S"),
                    "opcode": frame.get("opcode"),
                }
                self._ws_messages.append(msg_rec)
                if len(self._ws_messages) > self.MAX_WS_MESSAGES:
                    del self._ws_messages[: -self.MAX_WS_MESSAGES]
                self._persist_ws(msg_rec)

    # --- durable capture (JSONL mirror) -------------------------------------
    def set_persist_workspace(self, workspace: Path | str | None) -> str:
        """Point durable capture at <workspace>/.samet/network/."""
        new = Path(workspace).resolve() if workspace else None
        with self.state_lock:
            cur = self._persist_workspace.resolve() if self._persist_workspace else None
            if cur == new or (new is None and cur is None):
                # Same workspace: switching would only rotate the JSONL
                # capture file and reset the sandbox stage for nothing.
                return str(self._persist_dir())
            try:
                self._sandbox_flush_locked()
            except Exception:
                pass
            self._persist_close()
            self._persist_workspace = Path(workspace) if workspace else None
            self._persist_path = None
            self._sandbox_records.clear()
            self._sandbox_pending = 0
            self._sandbox_loaded = False
        return str(self._persist_dir())

    def _persist_dir(self) -> Path:
        base = self._persist_workspace or Path.cwd()
        return Path(base) / self.PERSIST_DIRNAME

    def _persist_fh_get(self):
        if not self.PERSIST_ENABLED:
            return None
        if self._persist_fh is not None:
            return self._persist_fh
        try:
            d = self._persist_dir()
            d.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            self._persist_path = d / f"capture-{stamp}.jsonl"
            self._persist_fh = open(self._persist_path, "a", encoding="utf-8")
            # A stable "latest" pointer so tooling never has to guess the name.
            try:
                (d / "latest.txt").write_text(str(self._persist_path), encoding="utf-8")
            except Exception:
                pass
        except Exception as e:
            self._drain_stats["last_error"] = f"persist: {type(e).__name__}: {e}"[:300]
            self.PERSIST_ENABLED = False
            self._persist_fh = None
        return self._persist_fh

    def _persist_write(self, obj: dict) -> None:
        fh = self._persist_fh_get()
        if fh is None:
            return
        try:
            fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
            self._persist_pending += 1
            if self._persist_pending >= self.PERSIST_FLUSH_EVERY:
                fh.flush()
                self._persist_pending = 0
        except Exception as e:
            self._drain_stats["last_error"] = f"persist: {type(e).__name__}: {e}"[:300]

    def _persist_record(self, rec: dict) -> None:
        """Mirror one network record to disk (last write for an id wins)."""
        self._persist_write({"kind": "request", "rec": rec})
        self._sandbox_mirror(rec)

    def _sandbox_path(self) -> Path:
        """Sandbox mirror file: <workspace>/network_logs.json.

        When the workspace already IS a per-session sandbox dir
        (``.../sandbox/<sid>``) the file lives directly inside it; otherwise
        it lives in ``<workspace>/sandbox/network_logs.json``.  Mirrors
        ``mcp/util.sandbox_network_path`` without importing it (keeps this
        module dependency-free).
        """
        base = self._persist_workspace or Path.cwd()
        base = Path(base)
        if base.parent.name == "sandbox":
            return base / self.SANDBOX_LOGNAME
        return base / "sandbox" / self.SANDBOX_LOGNAME

    def _sandbox_load(self) -> None:
        """Load existing sandbox records once (append-merge base, dedup by id)."""
        if self._sandbox_loaded:
            return
        self._sandbox_loaded = True
        p = self._sandbox_path()
        if not p.exists():
            return
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(old, list):
                raise ValueError("not a JSON array")
            for item in old:
                if isinstance(item, dict) and item.get("id"):
                    self._sandbox_records[str(item["id"])] = item
        except Exception:
            try:
                ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                bak = p.with_name(f"{p.stem}.corrupt-{ts}{p.suffix}")
                bak.write_bytes(p.read_bytes())
            except Exception:
                pass
            self._sandbox_records.clear()

    def _sandbox_mirror(self, rec: dict) -> None:
        """Stage one record for the sandbox mirror; flush every N records."""
        try:
            rid = str((rec or {}).get("id") or "")
            if not rid:
                return
            with self.state_lock:
                self._sandbox_load()
                self._sandbox_records[rid] = dict(rec)
                self._sandbox_pending += 1
                if self._sandbox_pending >= self.PERSIST_FLUSH_EVERY:
                    self._sandbox_flush_locked()
        except Exception as e:
            self._drain_stats["last_error"] = f"sandbox: {type(e).__name__}: {e}"[:300]

    def _sandbox_flush_locked(self) -> None:
        """Write staged records to sandbox/network_logs.json (caller holds state_lock).

        Append-merge: existing file records stay on top, new ones are
        appended below, deduped by id.  The file is never overwritten
        wholesale — only merged.  Corrupt files are backed up, not lost.
        """
        if self._sandbox_pending <= 0:
            return
        try:
            p = self._sandbox_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            existing: list = []
            if p.exists():
                try:
                    old = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(old, list):
                        existing = old
                    else:
                        raise ValueError("not a JSON array")
                except Exception:
                    try:
                        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                        bak = p.with_name(f"{p.stem}.corrupt-{ts}{p.suffix}")
                        bak.write_bytes(p.read_bytes())
                    except Exception:
                        pass
                    existing = []
            seen = {str(x.get("id")) for x in existing
                    if isinstance(x, dict) and x.get("id")}
            merged = list(existing)
            for rid, rec in self._sandbox_records.items():
                if rid in seen:
                    for i, item in enumerate(merged):
                        if isinstance(item, dict) and str(item.get("id")) == rid:
                            merged[i] = dict(rec)
                            break
                    continue
                merged.append(dict(rec))
                seen.add(rid)
            tmp = p.with_name(f"{p.stem}.tmp{p.suffix}")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2, default=str),
                           encoding="utf-8")
            tmp.replace(p)
            self._sandbox_pending = 0
        except Exception as e:
            self._drain_stats["last_error"] = f"sandbox: {type(e).__name__}: {e}"[:300]

    def _sandbox_flush(self) -> None:
        try:
            with self.state_lock:
                self._sandbox_flush_locked()
        except Exception:
            pass

    def _persist_ws(self, msg: dict) -> None:
        self._persist_write({"kind": "ws", "rec": msg})

    def _persist_flush(self) -> None:
        fh = self._persist_fh
        if fh is None:
            self._sandbox_flush()
            return
        try:
            fh.flush()
            self._persist_pending = 0
        except Exception:
            pass
        self._sandbox_flush()

    def _persist_close(self) -> None:
        self._sandbox_flush()
        fh = self._persist_fh
        self._persist_fh = None
        self._persist_pending = 0
        if fh is not None:
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass

    def load_persisted_capture(self, path: str | Path | None = None) -> dict:
        """Rebuild in-memory capture from a JSONL mirror.

        Lets a fresh MCP process (or a post-crash session) see the traffic that
        was recorded before it started.
        """
        p = Path(path) if path else None
        if p is None:
            d = self._persist_dir()
            latest = d / "latest.txt"
            if latest.exists():
                try:
                    p = Path(latest.read_text(encoding="utf-8").strip())
                except Exception:
                    p = None
            if p is None or not p.exists():
                files = sorted(d.glob("capture-*.jsonl"))
                p = files[-1] if files else None
        if p is None or not Path(p).exists():
            return {"ok": False, "error": "no persisted capture found",
                    "dir": str(self._persist_dir())}
        loaded_req = 0
        loaded_ws = 0
        with self.state_lock:
            for line in Path(p).read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                rec = obj.get("rec") or {}
                if obj.get("kind") == "ws":
                    self._ws_messages.append(rec)
                    loaded_ws += 1
                    continue
                rid = str(rec.get("id") or "")
                if not rid:
                    continue
                if rid not in self.requests:
                    self._request_order.append(rid)
                self.requests.setdefault(rid, {}).update(rec)
                loaded_req += 1
            if len(self._ws_messages) > self.MAX_WS_MESSAGES:
                del self._ws_messages[: -self.MAX_WS_MESSAGES]
        return {"ok": True, "file": str(p), "records": loaded_req,
                "ws_messages": loaded_ws, "requests_in_memory": len(self.requests)}

    def start_network_recording(self, clear: bool = False):
        """(Re)arm capture.  Capture is ON by default, so this is normally only
        needed after an explicit stop_network_recording().

        It no longer wipes previously captured traffic unless clear=True —
        silently discarding evidence on "start recording" was one of the ways
        traffic went missing.
        """
        self._network_recording = True
        if clear:
            self.clear_capture()
        try:
            self._enable_cdp_domains(self.start())
        except Exception:
            pass
        self._ensure_drain_thread()
        return self.capture_status()

    def stop_network_recording(self):
        """Pause storing network records.

        The background drain keeps running (Chrome's log is destructive and
        shared with console/error capture) but network events are discarded
        while paused.  Previously this flag was never read at all, so "stop"
        did nothing and "start" was decorative.
        """
        self._network_recording = False
        self._persist_flush()
        return self.capture_status()

    def get_network_requests(self, pattern: str = "", limit: int = 40) -> list[dict]:
        self._drain_perf_log()

        # Use _request_order for proper chronological ordering (FIFO-aware)
        rows = [self.requests[rid] for rid in self._request_order if rid in self.requests]

        if pattern:
            low = pattern.lower()
            rows = [r for r in rows if low in r.get("url", "").lower()]

        return rows if limit <= 0 else rows[-limit:]

    def get_network_request(self, request_id: str | int) -> dict | None:
        self._drain_perf_log()
        rid = str(request_id).strip()
        if rid.startswith("[") and rid.endswith("]"):
            rid = rid[1:-1].strip()
        # 1. Direct ID match
        if rid in self.requests:
            return self.requests[rid]
        # 2. Sequential index.  Indices handed out by network_summary /
        #    browser_network are 1-based, so 1-based is the ONLY interpretation
        #    here — the old 0-based fallback made "give me #12" ambiguous and
        #    could silently return the wrong request.
        if rid.isdigit():
            idx = int(rid)
            if 1 <= idx <= len(self._request_order):
                return self.requests.get(self._request_order[idx - 1])
            return None
        # 3. Match by ID or URL substring (most recent match wins)
        low = rid.lower()
        for cand in reversed(self._request_order):
            v = self.requests.get(cand)
            if not v:
                continue
            if low in cand.lower() or low in str(v.get("url", "")).lower():
                return v
        return None

    def get_network_responses(self, limit: int = 40) -> list[dict]:
        self._drain_perf_log()
        # Use _request_order for proper chronological ordering
        rows = [self.requests[rid] for rid in self._request_order
                if rid in self.requests and self.requests[rid].get("response_status") is not None]
        return rows if limit <= 0 else rows[-limit:]

    def wait_for_response(self, pattern: str, timeout_s: float = 10.0) -> dict | None:
        low = pattern.lower()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._drain_perf_log()
            # Iterate in reverse chronological order (most recent first)
            for rid in reversed(self._request_order):
                r = self.requests.get(rid)
                if r and r.get("response_status") is not None:
                    url = r.get("url", "").lower()
                    if fnmatch.fnmatch(url, low) or low in url:
                        return r
            time.sleep(0.3)
        return None

    def block_url(self, pattern: str):
        if pattern not in self._blocked_urls:
            self._blocked_urls.append(pattern)
        try:
            self.start().execute_cdp_cmd("Network.setBlockedURLs", {"urls": self._blocked_urls})
        except Exception:
            pass

    def unblock_url(self, pattern: str):
        if pattern in self._blocked_urls:
            self._blocked_urls.remove(pattern)
        try:
            self.start().execute_cdp_cmd("Network.setBlockedURLs", {"urls": self._blocked_urls})
        except Exception:
            pass

    def set_blocked(self, patterns: list[str]) -> None:
        self._blocked_urls = patterns
        try:
            self.start().execute_cdp_cmd("Network.setBlockedURLs", {"urls": patterns})
        except Exception:
            pass

    def network_summary(self, pattern: str = "", limit: int = 40) -> list[dict]:
        self._drain_perf_log()

        # Use _request_order for proper chronological ordering (FIFO-aware)
        order = [rid for rid in self._request_order if rid in self.requests]
        # Stamp a STABLE 1-based index over the whole capture first, so a
        # filtered or limited view still reports indices that resolve back to
        # the same request via get_network_request().
        for i, rid in enumerate(order, 1):
            self.requests[rid]["seq_index"] = i
        rows = [self.requests[rid] for rid in order]

        if pattern:
            low = pattern.lower()
            rows = [r for r in rows if low in str(r.get("url", "")).lower()]

        # limit=0 (or negative) means no cap → return all
        return rows if limit <= 0 else rows[-limit:]

    def response_body(self, request_id: str) -> dict:
        """Return a response body: the auto-captured copy first, live CDP after.

        Chrome drops bodies from its buffer aggressively, so the live call is a
        fallback — not the primary source — and its failure is reported instead
        of raised as an opaque CDP error.
        """
        req = self.get_network_request(request_id)
        if req and req.get("response_body") is not None:
            body = str(req["response_body"])
            b64 = bool(req.get("response_body_base64"))
            if b64 and body.startswith("(base64) "):
                body = body[len("(base64) "):]
            return {"body": body, "base64Encoded": b64, "source": "auto-captured",
                    "id": req.get("id"), "url": req.get("url", "")}
        actual_id = req.get("id") if req else str(request_id).strip()
        try:
            out = self.start().execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": actual_id}
            )
            out["source"] = "live-cdp"
            return out
        except Exception as e:
            return {
                "body": "", "base64Encoded": False, "source": "unavailable",
                "id": actual_id,
                "url": (req or {}).get("url", ""),
                "error": f"{type(e).__name__}: {e}".split("\n")[0][:300],
                "hint": (req or {}).get("response_body_error")
                        or "Body is no longer in Chrome's buffer. Bodies are "
                           "auto-captured during loading; check the persisted "
                           "capture file (browser_capture_status) for the record.",
            }

    def clear_capture(self) -> None:
        """Clear all captured network requests and reset order tracking.

        Only ever called on explicit request — the persisted JSONL mirror on
        disk is left intact so cleared traffic is still recoverable.
        """
        self._persist_flush()
        self.requests.clear()
        self._request_order.clear()

    # ============================================================= CONSOLE/DEBUG
    def console(self, clear: bool = False) -> list[dict]:
        self._drain_perf_log()
        out = list(self.console_logs)
        if clear:
            self.console_logs.clear()
        return out

    def errors(self, clear: bool = False) -> list[dict]:
        self._drain_perf_log()
        out = list(self.page_errors)
        if clear:
            self.page_errors.clear()
        return out

    def metrics(self) -> dict:
        d = self.start()
        try:
            d.execute_cdp_cmd("Performance.enable", {})
        except Exception:
            pass
        res = d.execute_cdp_cmd("Performance.getMetrics", {})
        return {m["name"]: m["value"] for m in res.get("metrics", [])}

    # ========================================================= BROWSER MGMT
    def launch_browser(self):
        self.start()

    def close_browser(self, force: bool = False) -> str:
        return self.close(force=force)

    def restart_browser(self, force: bool = False) -> str:
        # Preserve session before restart — eb5c03bb hardening
        try:
            self.auto_save_session()
        except Exception:
            pass
        # close() guard still applies; restart is explicit so default to forced
        # only when caller signals intent.  Loop's coding fallback never calls
        # restart, so forcing here does not reintroduce the regression.
        res = self.close(force=True)
        self.start()
        return res

    def browser_info(self) -> dict:
        d = self.start()
        caps = d.capabilities
        return {
            "browser": caps.get("browserName", "chrome"),
            "version": caps.get("browserVersion", caps.get("version", "?")),
            "url": d.current_url,
            "title": d.title,
            "window_handles": len(d.window_handles),
        }

    def emulate(self, user_agent: str = "", geo=None, tz: str = "", locale: str = "") -> str:
        d = self.start()
        done = []
        if user_agent:
            d.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": user_agent})
            done.append("ua")
        if geo is not None:
            d.execute_cdp_cmd("Emulation.setGeolocationOverride", geo)
            done.append("geo")
        if tz:
            d.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": tz})
            done.append("tz")
        if locale:
            try:
                d.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": locale})
                done.append("locale")
            except Exception:
                pass
        return ",".join(done) or "(nothing set)"

    def set_user_agent(self, user_agent: str) -> str:
        return self.emulate(user_agent=user_agent)

    def set_geolocation(self, latitude: float, longitude: float) -> str:
        return self.emulate(geo={"latitude": latitude, "longitude": longitude, "accuracy": 1})

    def set_timezone(self, timezone: str) -> str:
        return self.emulate(tz=timezone)

    def set_language(self, language: str) -> str:
        return self.emulate(locale=language)

    # ================================================================ DOWNLOAD
    def _enable_downloads(self, download_dir: str | None = None) -> str:
        path = download_dir or str(Path.cwd() / "downloads")
        Path(path).mkdir(parents=True, exist_ok=True)
        try:
            self.start().execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": path,
            })
        except Exception:
            pass
        return path

    def wait_for_download(self, timeout: float = 30.0, download_dir: str | None = None) -> str:
        dl_path = self._enable_downloads(download_dir)
        before = set(Path(dl_path).iterdir()) if Path(dl_path).exists() else set()
        deadline = time.time() + timeout
        while time.time() < deadline:
            after = set(Path(dl_path).iterdir())
            done = [f for f in (after - before) if f.suffix != ".crdownload"]
            if done:
                p = str(done[0])
                self._downloads.append({"path": p, "time": time.time()})
                return p
            time.sleep(0.5)
        raise TimeoutError(f"Download timeout ({timeout}s)")

    def get_downloads(self) -> list[dict]:
        return list(self._downloads)

    def download_file(self, url: str, path: str) -> str:
        import base64
        d = self.start()
        result = d.execute_script(
            "return fetch(arguments[0]).then(r=>r.blob()).then(b=>"
            "new Promise(res=>{var fr=new FileReader();"
            "fr.onloadend=()=>res(fr.result);fr.readAsDataURL(b);}));",
            url,
        )
        if result and result.startswith("data:"):
            _, data = result.split(",", 1)
            Path(path).write_bytes(base64.b64decode(data))
            self._downloads.append({"path": path, "time": time.time()})
            return path
        raise RuntimeError("download_file: fetch sonucu decode edilemedi")

    # =========================================================== ACCESSIBILITY
    def get_accessibility_tree(self) -> dict:
        return self.start().execute_script(f"return ({_A11Y_JS.strip()});") or {}

    def get_accessible_elements(self) -> list[dict]:
        """Flat list of visible interactive elements with role + name."""
        d = self.start()
        rows = d.execute_script(f"return ({_ELEMENTS_JS.strip()});") or []
        self.last_attrs = {r["i"]: r["i"] for r in rows}
        out = []
        for r in rows:
            if not r.get("vis"):
                continue
            name = (r.get("label") or r.get("aria_label") or r.get("txt")
                    or r.get("ph") or r.get("name") or "")
            out.append({
                "index": r["i"],
                "role": r.get("role", r["tag"]),
                "name": name,
                "tag": r["tag"],
                "type": r.get("type", ""),
                "disabled": r.get("disabled", False),
                "checked": r.get("checked"),
            })
        return out

    # =========================================================== AGENT HELPERS
    def observe(self) -> dict:
        """High-level page summary for agent consumption."""
        d = self.start()
        rows = d.execute_script(f"return ({_ELEMENTS_JS.strip()});") or []
        self.last_attrs = {r["i"]: r["i"] for r in rows}
        visible = [r for r in rows if r.get("vis")]
        buttons = [r for r in visible if r["tag"] == "button" or r.get("role") == "button"]
        inputs  = [r for r in visible if r["tag"] in ("input", "textarea", "select")]
        links   = [r for r in visible if r["tag"] == "a"]
        body = d.find_element("css selector", "body")
        self._drain_perf_log()
        return {
            "url": d.current_url,
            "title": d.title,
            "text_snippet": (body.text or "")[:600],
            "interactive_count": len(visible),
            "buttons": [
                {"index": r["i"], "text": r.get("txt", ""), "disabled": r.get("disabled")}
                for r in buttons[:10]
            ],
            "inputs": [
                {
                    "index": r["i"],
                    "type": r.get("type", ""),
                    "label": r.get("label") or r.get("aria_label") or r.get("ph", ""),
                    "disabled": r.get("disabled"),
                }
                for r in inputs[:10]
            ],
            "links": [
                {"index": r["i"], "text": r.get("txt", ""), "href": r.get("href", "")}
                for r in links[:10]
            ],
            "page_errors": list(self.page_errors[-5:]),
            "hint": "Call browser_elements for the full indexed list before clicking/typing.",
        }

    def inspect_element(self, selector: str = "", index: int | None = None) -> dict:
        sel = self._resolve_selector(index, selector)
        d = self.start()
        el = d.find_element("css selector", sel)
        info = self._el_info(el, sel)
        info["outer_html"] = (el.get_attribute("outerHTML") or "")[:500]
        info["computed_style"] = d.execute_script(
            "var s=window.getComputedStyle(arguments[0]);"
            "return {display:s.display,visibility:s.visibility,opacity:s.opacity};",
            el,
        )
        return info

    def find_interactive_elements(self) -> list[dict]:
        return self.get_accessible_elements()

    def get_page_state(self) -> dict:
        """URL + title + forms + console errors."""
        d = self.start()
        forms = d.execute_script(
            "return Array.from(document.forms).map(f=>({id:f.id,action:f.action,"
            "method:f.method,fields:Array.from(f.elements).map(e=>({tag:e.tagName.toLowerCase(),"
            "name:e.name,type:e.type,value:e.value}))}));"
        ) or []
        self._drain_perf_log()
        return {
            "url": d.current_url,
            "title": d.title,
            "forms": forms[:5],
            "page_errors": list(self.page_errors[-10:]),
            "console_errors": [c for c in self.console_logs[-20:] if c.get("type") == "error"],
        }

    def click_text(self, text: str) -> str:
        """Click first visible element whose text matches (injection-safe XPath)."""
        d = self.start()
        safe = _xpath_escape(text)
        for xp in (
            f'//button[contains(normalize-space(.), {safe})]',
            f'//a[contains(normalize-space(.), {safe})]',
            f'//*[@role="button"][contains(normalize-space(.), {safe})]',
            f'//*[normalize-space(text())={safe}]',
        ):
            for el in d.find_elements("xpath", xp):
                if el.is_displayed() and el.is_enabled():
                    try:
                        el.click()
                    except Exception:
                        d.execute_script("arguments[0].click();", el)
                    self._record_action("click_text", text=text)
                    return f"OK: clicked element with text '{text}'"
        raise RuntimeError(f"Visible element with text '{text}' not found")

    def type_into_label(self, label_or_text: str, value: str) -> str:
        """Find input by label/placeholder and fill it."""
        info = (self.find_by_label(label_or_text)
                or self.find_by_placeholder(label_or_text))
        if info is None:
            raise RuntimeError(f"Input for '{label_or_text}' not found")
        self.fill(info["selector"], value)
        return f"OK: filled '{label_or_text}'"

    def extract_structured(self, selector: str | None = None) -> list:
        """Extract table / list data."""
        d = self.start()
        if selector:
            return d.execute_script(
                "var el=document.querySelector(arguments[0]);"
                "if(!el)return[];"
                "if(el.tagName==='TABLE'){"
                "var rows=[];el.querySelectorAll('tr').forEach(function(tr){"
                "rows.push(Array.from(tr.querySelectorAll('td,th')).map(function(c){return c.innerText.trim();}));});"
                "return rows;}"
                "return Array.from(el.querySelectorAll('li')).map(function(li){return{text:li.innerText.trim()};});",
                selector,
            ) or []
        return d.execute_script(
            "return Array.from(document.querySelectorAll('table')).slice(0,3).map(function(t){"
            "var rows=[];t.querySelectorAll('tr').forEach(function(tr){"
            "rows.push(Array.from(tr.querySelectorAll('td,th')).map(function(c){return c.innerText.trim();}));});"
            "return rows;});"
        ) or []

    def verify_condition(self, condition: str) -> dict:
        """Verify conditions against current page state with exact pattern matching.
        Supports explicit prefixes like 'url:', 'text:', 'selector:', 'title:', quoted strings, or keyword matching.
        """
        d = self.start()
        cur_url = d.current_url or ""
        title = d.title or ""
        body_txt = ""
        try:
            body_txt = d.find_element("css selector", "body").text or ""
        except Exception:
            pass

        cond = condition.strip()
        cond_lower = cond.lower()

        # 1. Explicit syntax: url: or url contains
        if cond_lower.startswith("url:") or cond_lower.startswith("url contains "):
            val = cond.split(":", 1)[-1] if cond_lower.startswith("url:") else cond[13:]
            val = val.strip().strip("'\"")
            ok = val.lower() in cur_url.lower()
            return {"ok": ok, "type": "url_check", "detail": f"URL '{cur_url}' {'contains' if ok else 'does NOT contain'} '{val}'"}

        # 2. Explicit syntax: text: or text contains
        if cond_lower.startswith("text:") or cond_lower.startswith("text contains "):
            val = cond.split(":", 1)[-1] if cond_lower.startswith("text:") else cond[14:]
            val = val.strip().strip("'\"")
            ok = val in body_txt
            return {"ok": ok, "type": "text_check", "detail": f"Text '{val}' {'present' if ok else 'NOT present'} on page"}

        # 3. Explicit syntax: selector: or element:
        if cond_lower.startswith("selector:") or cond_lower.startswith("element:"):
            sel = cond.split(":", 1)[1].strip()
            ok = self.is_visible(sel)
            return {"ok": ok, "type": "element_check", "detail": f"Selector '{sel}' {'visible' if ok else 'NOT visible'}"}

        # 4. Quoted phrases: "some phrase"
        if ('"' in cond and cond.count('"') >= 2) or ("'" in cond and cond.count("'") >= 2):
            import re
            quotes = re.findall(r'["\']([^"\']+)["\']', cond)
            if quotes:
                results = [q in body_txt or q.lower() in cur_url.lower() or q in title for q in quotes]
                all_ok = all(results)
                return {"ok": all_ok, "type": "quoted_text_check", "detail": f"Phrases {quotes} -> {results}"}

        # 5. Fallback keyword check
        words = [w.strip('"\'.,!?') for w in cond.split() if len(w) > 3]
        found_words = [w for w in words if w.lower() in body_txt.lower() or w.lower() in cur_url.lower()]
        all_found = len(found_words) == len(words) and len(words) > 0
        return {
            "ok": all_found,
            "type": "keyword_heuristic",
            "matched_keywords": found_words,
            "missing_keywords": [w for w in words if w not in found_words],
            "detail": f"Matched {len(found_words)}/{len(words)} keywords on page"
        }


    # ===================================================== ELEMENT LIFECYCLE
    def get_element_id(self, selector: str) -> str:
        """Return a stable JS-side identity string for the element (outerHTML hash)."""
        d = self.start()
        try:
            result = d.execute_script(
                "var el = document.querySelector(arguments[0]);"
                "if (!el) return null;"
                "return el.tagName + '#' + (el.id||'') + '.' + (el.className||'').replace(/\\s+/g,'.')"
                "+ '[' + el.getBoundingClientRect().x + ',' + el.getBoundingClientRect().y + ']';",
                selector,
            )
            return str(result) if result else f"element not found: {selector}"
        except Exception as e:
            return f"error: {e}"

    def is_attached(self, selector: str) -> bool:
        """Return True if the element is currently attached to the DOM."""
        try:
            result = self.start().execute_script(
                "var el = document.querySelector(arguments[0]);"
                "return el !== null && document.contains(el);",
                selector,
            )
            return bool(result)
        except Exception:
            return False

    def is_stable(self, selector: str, check_ms: int = 200) -> bool:
        """Return True if the element's bounding rect did not change over check_ms milliseconds."""
        d = self.start()
        try:
            rect1 = d.execute_script(
                "var el=document.querySelector(arguments[0]);"
                "if(!el)return null;"
                "var r=el.getBoundingClientRect();"
                "return {x:r.x,y:r.y,w:r.width,h:r.height};",
                selector,
            )
            time.sleep(check_ms / 1000)
            rect2 = d.execute_script(
                "var el=document.querySelector(arguments[0]);"
                "if(!el)return null;"
                "var r=el.getBoundingClientRect();"
                "return {x:r.x,y:r.y,w:r.width,h:r.height};",
                selector,
            )
            if rect1 is None or rect2 is None:
                return False
            return (rect1["x"] == rect2["x"] and rect1["y"] == rect2["y"]
                    and rect1["w"] == rect2["w"] and rect1["h"] == rect2["h"])
        except Exception:
            return False

    def wait_for_stable(self, selector: str, timeout: float = 5.0, check_ms: int = 150) -> str:
        """Wait until the element stops moving/resizing."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_stable(selector, check_ms):
                return f"OK: {selector} is stable"
            time.sleep(0.1)
        raise TimeoutError(f"Element '{selector}' did not become stable after {timeout}s")

    def wait_for_element_change(self, selector: str, timeout: float = 10.0) -> str:
        """Wait until the element's outerHTML changes (useful after React re-renders)."""
        d = self.start()
        try:
            initial = d.execute_script(
                "var el=document.querySelector(arguments[0]);"
                "return el ? el.outerHTML : null;",
                selector,
            )
        except Exception:
            initial = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                current = d.execute_script(
                    "var el=document.querySelector(arguments[0]);"
                    "return el ? el.outerHTML : null;",
                    selector,
                )
                if current != initial:
                    return f"OK: {selector} changed"
            except Exception:
                pass
            time.sleep(0.3)
        raise TimeoutError(f"Element '{selector}' did not change after {timeout}s")

    def wait_for_attribute(self, selector: str, attribute: str, value: str,
                           timeout: float = 10.0) -> str:
        """Wait until element[attribute] == value."""
        d = self.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                cur = d.execute_script(
                    "var el=document.querySelector(arguments[0]);"
                    "return el ? el.getAttribute(arguments[1]) : null;",
                    selector, attribute,
                )
                if str(cur) == value:
                    return f"OK: {selector}[{attribute}]=={value!r}"
            except Exception:
                pass
            time.sleep(0.3)
        raise TimeoutError(
            f"Attribute '{attribute}' of '{selector}' never became '{value}' after {timeout}s"
        )

    def wait_for_value(self, selector: str, value: str, timeout: float = 10.0) -> str:
        """Wait until an input's value property equals the expected value."""
        d = self.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                cur = d.execute_script(
                    "var el=document.querySelector(arguments[0]);"
                    "return el ? el.value : null;",
                    selector,
                )
                if str(cur) == value:
                    return f"OK: value=={value!r}"
            except Exception:
                pass
            time.sleep(0.3)
        raise TimeoutError(f"Input '{selector}' value never became '{value}' after {timeout}s")

    # ================================================= ACTION VERIFICATION
    def verify_element_visible(self, selector: str) -> dict:
        """Check whether the element is visible."""
        visible = self.is_visible(selector)
        return {"ok": visible, "selector": selector,
                "detail": "visible" if visible else "not visible / not in DOM"}

    def verify_text(self, text: str) -> dict:
        """Check whether the given text appears anywhere on the page."""
        try:
            body = self.start().find_element("css selector", "body").text or ""
            found = text in body
            return {"ok": found, "detail": f"text '{text}' {'found' if found else 'NOT found'} on page"}
        except Exception as e:
            return {"ok": False, "detail": f"error: {e}"}

    def verify_url(self, expected: str) -> dict:
        """Check whether the current URL matches or contains the expected string."""
        cur = self.start().current_url or ""
        ok = expected in cur or cur == expected
        return {"ok": ok, "current_url": cur,
                "detail": f"URL {'matches' if ok else 'does NOT match'} '{expected}'"}

    def verify_element_value(self, selector: str, value: str) -> dict:
        """Check whether an input's current value equals the expected value."""
        try:
            el = self.start().find_element("css selector", selector)
            cur = el.get_attribute("value") or ""
            ok = cur == value
            return {"ok": ok, "current": cur, "expected": value,
                    "detail": "value matches" if ok else f"value is '{cur}', expected '{value}'"}
        except Exception as e:
            return {"ok": False, "detail": f"error: {e}"}

    def verify_action(self, checks: dict) -> dict:
        """
        Run a set of verification checks and return a combined result.

        checks = {
            "url_contains": "dashboard",
            "text_present": "Saved successfully",
            "selector_visible": ".success-msg",
            "selector_value": {"selector": "#name", "value": "Alice"}
        }
        """
        results = {}
        all_ok = True

        if "url_contains" in checks:
            r = self.verify_url(checks["url_contains"])
            results["url"] = r
            if not r["ok"]:
                all_ok = False

        if "text_present" in checks:
            r = self.verify_text(checks["text_present"])
            results["text"] = r
            if not r["ok"]:
                all_ok = False

        if "selector_visible" in checks:
            r = self.verify_element_visible(checks["selector_visible"])
            results["element"] = r
            if not r["ok"]:
                all_ok = False

        if "selector_value" in checks:
            sv = checks["selector_value"]
            r = self.verify_element_value(sv["selector"], sv["value"])
            results["value"] = r
            if not r["ok"]:
                all_ok = False

        return {"ok": all_ok, "checks": results}

    # ===================================================== ACTION HISTORY
    def _record_action(self, name: str, **kwargs) -> None:
        """Append an action record to history (called by session methods internally)."""
        try:
            self._last_action_ts = time.time()
        except Exception:
            pass
        record = {
            "action": name,
            "timestamp": time.time(),
            "params": kwargs,
            "url": self._driver.current_url if self._driver else None,
        }
        self._action_history.append(record)
        # keep last 500
        del self._action_history[:-500]

    def get_action_history(self) -> list[dict]:
        return list(self._action_history)

    def get_last_action(self) -> dict | None:
        return self._action_history[-1] if self._action_history else None

    def undo_last_action(self) -> str:
        """
        Attempt to undo the last action.
        Supports: goto (go back), fill/type (clear the field).
        """
        last = self.get_last_action()
        if last is None:
            return "No action to undo"
        name = last["action"]
        params = last.get("params", {})
        if name in ("goto", "open"):
            self.back()
            return f"OK: went back (undid {name})"
        if name in ("fill", "type_text"):
            sel = params.get("selector", "")
            if sel:
                self.clear_input(sel)
                return f"OK: cleared input {sel} (undid {name})"
        if name == "check_box":
            sel = params.get("selector", "")
            was = params.get("checked", True)
            if sel:
                self.check_box(sel, checked=not was)
                return f"OK: toggled checkbox back (undid {name})"
        return f"Cannot undo action '{name}' automatically"

    def clear_action_history(self) -> None:
        self._action_history.clear()

    # ================================================= ADVANCED NETWORK
    def get_request_details(self, request_id: str) -> dict:
        """Return full request record including headers, body, timing."""
        return self.get_network_request(request_id) or {}

    def get_response_details(self, request_id: str) -> dict:
        """Return response-side fields of a captured request."""
        r = self.get_network_request(request_id) or {}
        return {
            "id": r.get("id", request_id),
            "url": r.get("url"),
            "response_status": r.get("response_status"),
            "response_headers": r.get("response_headers", {}),
            "mime": r.get("mime"),
            "remote_address": r.get("remote_address"),
            "from_cache": r.get("from_cache"),
            "redirect_chain": r.get("redirect_chain"),
            "error": r.get("error"),
        }

    def get_request_body(self, request_id: str) -> str:
        """Return the POST/PUT body of a request."""
        r = self.get_network_request(request_id) or {}
        return r.get("post_data", "") or ""

    def get_response_body(self, request_id: str) -> str:
        """Return the response body of a request (auto-captured or live CDP)."""
        try:
            result = self.response_body(request_id)
            if result.get("source") == "unavailable":
                return f"error: {result.get('error')} | {result.get('hint', '')}"
            data = str(result.get("body", ""))
            if result.get("base64Encoded") and not data.startswith("(base64) "):
                return "(base64) " + data
            return data
        except Exception as e:
            return f"error: {e}"

    def get_api_requests(self, limit: int = 40) -> list[dict]:
        """Return only XHR / fetch requests (resource_type XHR or Fetch)."""
        self._drain_perf_log()

        # Use _request_order for proper chronological ordering
        rows = [
            self.requests[rid] for rid in self._request_order
            if rid in self.requests
            and (self.requests[rid].get("resource_type") or "").lower() in ("xhr", "fetch", "")
            and self.requests[rid].get("url", "")
        ]

        # If resource_type is empty, fall back to content-type heuristic
        api_rows = [
            r for r in rows
            if (r.get("resource_type") or "").lower() in ("xhr", "fetch")
            or "application/json" in (r.get("response_headers") or {}).get("content-type", "")
            or "application/json" in (r.get("headers") or {}).get("content-type", "")
        ]
        pool = api_rows or rows
        return pool if limit <= 0 else pool[-limit:]

    def find_request(self, url_pattern: str | None = None,
                     method: str | None = None) -> dict | None:
        """Find the most recent request matching url_pattern and/or method."""
        self._drain_perf_log()

        # Search in reverse chronological order (most recent first)
        for rid in reversed(self._request_order):
            r = self.requests.get(rid)
            if not r:
                continue
            if url_pattern:
                url = r.get("url", "").lower()
                if url_pattern.lower() not in url and not fnmatch.fnmatch(url, url_pattern.lower()):
                    continue
            if method and r.get("method", "").upper() != method.upper():
                continue
            return r
        return None

    def wait_for_api(self, url_pattern: str, timeout: float = 15.0) -> dict | None:
        """Wait for an XHR/fetch request matching url_pattern to complete (has response_status)."""
        low = url_pattern.lower()
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._drain_perf_log()
            # Search in reverse chronological order (most recent first)
            for rid in reversed(self._request_order):
                r = self.requests.get(rid)
                if not r:
                    continue
                url = r.get("url", "").lower()
                if (low in url or fnmatch.fnmatch(url, low)) and r.get("response_status") is not None:
                    return r
            time.sleep(0.3)
        return None

    def replay_request(self, request_id: str) -> str:
        """Re-execute a captured request via browser fetch() and return the response text."""
        r = self.get_network_request(request_id)
        if not r:
            raise RuntimeError(f"Request ID '{request_id}' not found in capture")
        url = r.get("url", "")
        method = r.get("method", "GET")
        headers = r.get("headers", {})
        body = r.get("post_data", "")
        d = self.start()
        headers_js = json.dumps(headers)
        body_js = json.dumps(body) if body else "undefined"
        options_js = f"{{method:{json.dumps(method)},headers:{headers_js}"
        if body:
            options_js += f",body:{body_js}"
        options_js += "}"
        result = d.execute_script(
            f"return fetch({json.dumps(url)}, {options_js})"
            ".then(r=>r.text()).catch(e=>('ERROR:'+e));",
        )
        return str(result)[:4000]

    # ================================================= WEBSOCKET
    def start_websocket_recording(self) -> None:
        """Enable WebSocket frame capture via CDP performance log."""
        self._ws_messages: list[dict] = getattr(self, "_ws_messages", [])
        self._ws_connections: dict[str, dict] = getattr(self, "_ws_connections", {})
        self._ws_recording = True
        # Network.enable is already active; WS events come through performance log

    def _drain_ws_log(self) -> None:
        """Parse WebSocket events from the performance log.

        NOTE: WebSocket events share the same CDP performance-log buffer as
        network/console events, and ``get_log()`` is a destructive read. All
        event categories are therefore parsed by the single shared drain
        (:meth:`_drain_perf_log`); this method just triggers it so WS events
        are never stolen by (or steal from) other consumers.
        """
        self._drain_perf_log()

    def get_websocket_connections(self) -> list[dict]:
        self._drain_ws_log()
        return list(getattr(self, "_ws_connections", {}).values())

    def get_websocket_messages(self, connection_id: str | None = None,
                               limit: int = 100) -> list[dict]:
        self._drain_ws_log()
        msgs = list(getattr(self, "_ws_messages", []))
        if connection_id:
            msgs = [m for m in msgs if m.get("connection_id") == connection_id]
        return msgs if limit <= 0 else msgs[-limit:]

    def wait_for_websocket_message(self, pattern: str, timeout: float = 15.0) -> dict | None:
        """Wait for a WebSocket message whose payload contains pattern."""
        self._ws_recording = True
        if not hasattr(self, "_ws_messages"):
            self._ws_messages = []
        if not hasattr(self, "_ws_connections"):
            self._ws_connections = {}
        low = pattern.lower()
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._drain_ws_log()
            for m in reversed(getattr(self, "_ws_messages", [])):
                if low in (m.get("payload") or "").lower():
                    return m
            time.sleep(0.3)
        return None

    # ================================================= PAGE INTELLIGENCE
    _DETECT_JS = r"""
(function(){
  var result = {
    url: window.location.href,
    title: document.title,
    forms: [],
    inputs: [],
    buttons: [],
    links: [],
    modals: [],
    popups: [],
    errors: [],
    iframes: 0,
    captcha: false
  };

  // Forms
  Array.from(document.forms).forEach(function(f){
    result.forms.push({
      id: f.id||null, action: f.action||null, method: f.method||'get',
      field_count: f.elements.length
    });
  });

  // Inputs
  document.querySelectorAll('input:not([type=hidden]),textarea,select').forEach(function(el){
    var r = el.getBoundingClientRect();
    if(r.width===0 && r.height===0) return;
    result.inputs.push({
      type: el.type||el.tagName.toLowerCase(),
      name: el.name||el.id||el.getAttribute('aria-label')||el.placeholder||'',
      placeholder: el.placeholder||'',
      required: el.required||false,
      disabled: el.disabled||false
    });
  });

  // Buttons
  document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"]').forEach(function(el){
    var r = el.getBoundingClientRect();
    if(r.width===0 && r.height===0) return;
    result.buttons.push({
      text: (el.innerText||el.value||'').trim().slice(0,60),
      disabled: el.disabled||el.getAttribute('aria-disabled')==='true'||false,
      type: el.type||null
    });
  });

  // Links
  document.querySelectorAll('a[href]').forEach(function(el){
    var r = el.getBoundingClientRect();
    if(r.width===0 && r.height===0) return;
    result.links.push({
      text: (el.innerText||'').trim().slice(0,60),
      href: el.href||''
    });
    if(result.links.length>30){return;}
  });

  // Modals / dialogs
  document.querySelectorAll('[role="dialog"],[role="alertdialog"],.modal,.dialog').forEach(function(el){
    var st=window.getComputedStyle(el);
    if(st.display==='none'||st.visibility==='hidden') return;
    result.modals.push({
      id:el.id||null,
      aria_label:el.getAttribute('aria-label')||null,
      text:(el.innerText||'').trim().slice(0,80)
    });
  });

  // Popups (tooltips, popovers)
  document.querySelectorAll('[role="tooltip"],[role="status"],[role="alert"],.toast,.snackbar,.popup,.popover').forEach(function(el){
    var st=window.getComputedStyle(el);
    if(st.display==='none'||st.visibility==='hidden') return;
    result.popups.push({text:(el.innerText||'').trim().slice(0,80)});
  });

  // Error messages
  var errSelectors = [
    '.error,.errors,.error-message,.alert-danger,.invalid-feedback',
    '[role="alert"]',
    '[aria-invalid="true"]',
    '.field-error,.form-error,.validation-error'
  ];
  errSelectors.forEach(function(sel){
    try {
      document.querySelectorAll(sel).forEach(function(el){
        var txt=(el.innerText||'').trim();
        if(txt && result.errors.indexOf(txt)<0) result.errors.push(txt.slice(0,100));
      });
    } catch(e){}
  });

  // iframes
  result.iframes = document.querySelectorAll('iframe,frame').length;

  // Captcha detection
  var captchaPatterns = ['recaptcha','hcaptcha','captcha','cf-turnstile','challenge-form'];
  var bodyHtml = document.body ? document.body.innerHTML.toLowerCase() : '';
  result.captcha = captchaPatterns.some(function(p){return bodyHtml.includes(p);});

  return result;
})()
"""

    def analyze_page(self) -> dict:
        """Full structural page analysis: forms, inputs, buttons, links, modals, errors, captcha."""
        return self.start().execute_script(f"return ({self._DETECT_JS.strip()});") or {}

    def detect_forms(self) -> list[dict]:
        return self.analyze_page().get("forms", [])

    def detect_buttons(self) -> list[dict]:
        return self.analyze_page().get("buttons", [])

    def detect_links(self) -> list[dict]:
        return self.analyze_page().get("links", [])

    def detect_inputs(self) -> list[dict]:
        return self.analyze_page().get("inputs", [])

    def detect_modals(self) -> list[dict]:
        return self.analyze_page().get("modals", [])

    def detect_popups(self) -> list[dict]:
        return self.analyze_page().get("popups", [])

    def detect_errors(self) -> list[str]:
        return self.analyze_page().get("errors", [])

    def detect_captcha(self) -> bool:
        return self.analyze_page().get("captcha", False)

    # ================================================================ SESSION SNAPSHOT/RESTORE
    def export_session(self, path: str | None = None) -> dict:
        """Capture current browser session state (URL, cookies, localStorage, sessionStorage).
        
        This snapshot can be used to restore the session after a browser restart or MCP server crash.
        
        Args:
            path: Optional file path to save snapshot as JSON. If provided, writes to file.
        
        Returns:
            Dictionary containing full session state
        """
        d = self.start()

        snapshot = {
            "timestamp": datetime.datetime.now().isoformat(),
            "url": d.current_url,
            "title": d.title,
            "cookies": self.get_cookies(),
            "localStorage": self.get_local_storage(),
            "sessionStorage": self.get_session_storage(),
            "viewport": {
                "width": d.execute_script("return window.innerWidth;"),
                "height": d.execute_script("return window.innerHeight;"),
            },
        }

        if path:
            p = Path(path).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

        return snapshot

    def restore_session(self, snapshot: dict | str) -> str:
        """Restore browser session from a previously captured snapshot.
        
        Args:
            snapshot: Either a dict (from export_session) or a path to JSON file
        
        Returns:
            Status message describing what was restored
        """
        # Load from file if string path provided
        if isinstance(snapshot, str):
            p = Path(snapshot).resolve()
            if not p.exists():
                return f"ERROR: snapshot file not found: {snapshot}"
            try:
                snapshot = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                return f"ERROR: failed to parse snapshot JSON: {e}"

        if not isinstance(snapshot, dict):
            return "ERROR: snapshot must be a dict or path to JSON file"

        d = self.start()
        restored_items = []

        # 1. Restore cookies (must be done before navigating to target URL for same-domain cookies)
        cookies = snapshot.get("cookies", [])
        if cookies:
            try:
                # Clear existing cookies first
                d.delete_all_cookies()
                for cookie in cookies:
                    # Add cookie (SeleniumBase handles the format)
                    try:
                        d.add_cookie(cookie)
                    except Exception as e:
                        # Skip cookies that can't be set (expired, wrong domain, etc.)
                        pass
                restored_items.append(f"{len(cookies)} cookies")
            except Exception as e:
                restored_items.append(f"cookies (partial, error: {e})")

        # 2. Navigate to the URL
        url = snapshot.get("url", "")
        if url and url not in ("about:blank", "data:,", "chrome://", "chrome-error://"):
            try:
                d.get(url)
                restored_items.append(f"URL: {url}")
            except Exception as e:
                return f"ERROR: failed to navigate to {url}: {e}"
        else:
            restored_items.append("URL: (skipped, no valid URL in snapshot)")

        # 3. Restore localStorage
        local_storage = snapshot.get("localStorage", {})
        if local_storage:
            try:
                for key, value in local_storage.items():
                    self.set_local_storage_item(key, str(value))
                restored_items.append(f"{len(local_storage)} localStorage items")
            except Exception as e:
                restored_items.append(f"localStorage (error: {e})")

        # 4. Restore sessionStorage
        session_storage = snapshot.get("sessionStorage", {})
        if session_storage:
            try:
                for key, value in session_storage.items():
                    self.set_session_storage_item(key, str(value))
                restored_items.append(f"{len(session_storage)} sessionStorage items")
            except Exception as e:
                restored_items.append(f"sessionStorage (error: {e})")

        # 5. Restore viewport size
        viewport = snapshot.get("viewport", {})
        if viewport.get("width") and viewport.get("height"):
            try:
                d.set_window_size(viewport["width"], viewport["height"])
                restored_items.append(f"viewport: {viewport['width']}x{viewport['height']}")
            except Exception:
                pass

        summary = "Session restored: " + ", ".join(restored_items)
        snapshot_time = snapshot.get("timestamp", "unknown time")
        return f"{summary}\n(Snapshot from: {snapshot_time})"

    def auto_snapshot_path(self, workspace: Path | str | None = None) -> Path:
        """Return the default auto-snapshot path for this session.
        
        This is used for automatic session backup before browser restart/close.
        """
        if workspace:
            base = Path(workspace)
        else:
            base = Path.cwd()

        snapshot_dir = base / ".samet" / "browser_sessions"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        return snapshot_dir / "last_session.json"

    def auto_save_session(self, workspace: Path | str | None = None) -> str:
        """Automatically save current session to default snapshot path.
        
        This is called automatically before browser restart in the MCP server.
        """
        try:
            path = self.auto_snapshot_path(workspace)
            self.export_session(str(path))
            return f"Session auto-saved to {path}"
        except Exception as e:
            return f"Failed to auto-save session: {e}"

    def auto_restore_session(self, workspace: Path | str | None = None) -> str:
        """Automatically restore session from default snapshot path if it exists.

        This is called automatically after browser restart in the MCP server.
        """
        try:
            path = self.auto_snapshot_path(workspace)
            if not path.exists():
                return "No auto-saved session found"
            return self.restore_session(str(path))
        except Exception as e:
            return f"Failed to auto-restore session: {e}"

    # ============================================================ JS TRACE / CALL STACK
    # "like'e tıkla, hangi js dosyasında hangi fonksiyon sırayla çalıştı?"
    # sorusunu yanıtlamak için.  No Debugger domain required — window.__sametTrace
    # içindeki wrapper'lar Error().stack ile dosya:satır bilgisini toplar.
    def _ensure_trace(self) -> bool:
        d = self.start()
        try:
            d.execute_script(_TRACE_JS)
            return True
        except Exception:
            return False

    def inject_trace(self) -> dict:
        ok = self._ensure_trace()
        d = self.start()
        try:
            st = d.execute_script("return (window.__sametTrace && window.__sametTrace.__patched) ? true : false;")
        except Exception:
            st = False
        return {"ok": ok, "patched": bool(st)}

    def start_js_trace(self, clear: bool = True) -> dict:
        self._ensure_trace()
        d = self.start()
        try:
            res = d.execute_script("return window.__sametTraceStart(arguments[0]);", bool(clear))
            return res if isinstance(res, dict) else {"ok": True, "enabled": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}

    def stop_js_trace(self) -> dict:
        d = self.start()
        try:
            res = d.execute_script("return window.__sametTraceStop();")
            return res if isinstance(res, dict) else {"ok": True, "enabled": False}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}

    def clear_js_trace(self) -> dict:
        self._ensure_trace()
        d = self.start()
        try:
            res = d.execute_script("return window.__sametTraceClear();")
            return res if isinstance(res, dict) else {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}

    def get_js_trace(self) -> dict:
        self._ensure_trace()
        self._drain_perf_log()
        d = self.start()
        try:
            data = d.execute_script("return window.__sametTraceGet();")
            if not isinstance(data, dict):
                return {"enabled": False, "calls": [], "listeners": []}
            # Normalise stacks into parsed file:line hints
            calls = data.get("calls") or []
            for c in calls:
                st = str(c.get("stack") or "")
                # Pull file:line tokens — keep first two hops that look like http/file
                import re as _re
                hits = _re.findall(r"(https?://[^\s|]+\.(?:js|mjs|ts|tsx|jsx)[^\s|]*:\d+:\d+|/[^\s|]+\.(?:js|mjs|ts|tsx|jsx)[^\s|]*:\d+:\d+|\(.*?:\d+:\d+\))", st)
                if hits:
                    c["files"] = hits[:4]
            return data
        except Exception as e:
            return {"enabled": False, "calls": [], "listeners": [], "error": f"{type(e).__name__}: {e}"[:400]}

    def get_event_listeners(self, selector: str = "", index: int | None = None) -> dict:
        """Listeners bound to one element — CDP DOMDebugger.getEventListeners + JS fallback."""
        self._ensure_trace()
        sel = self._resolve_selector(index, selector) if (selector or index is not None) else ""
        d = self.start()
        cdp_listeners: list[dict] = []
        # 1) Try CDP DOMDebugger if element selector is given
        if sel:
            try:
                doc = d.execute_cdp_cmd("DOM.getDocument", {"depth": 0})
                root_id = doc.get("root", {}).get("nodeId")
                if root_id:
                    q = d.execute_cdp_cmd("DOM.querySelector", {"nodeId": root_id, "selector": sel})
                    node_id = q.get("nodeId") or 0
                    if node_id:
                        rr = d.execute_cdp_cmd("DOMDebugger.getEventListeners", {"objectId": str(node_id)})
                        # Some Chrome versions expect nodeId in that call; fallback
                        if not rr.get("listeners"):
                            try:
                                rr = d.execute_cdp_cmd("DOMDebugger.getEventListeners", {"nodeId": node_id})
                            except Exception:
                                pass
                        for li in rr.get("listeners") or []:
                            cdp_listeners.append({
                                "type": li.get("type"),
                                "useCapture": li.get("useCapture"),
                                "handler": (li.get("handler") or {}).get("description", "")[:200],
                                "source": "cdp",
                            })
            except Exception as e:
                cdp_listeners.append({"type": "cdp_error", "error": f"{type(e).__name__}: {e}"[:200]})

        # 2) JS-side registry (window.__sametTrace.listeners) filtered by tag/id
        js_listeners: list[dict] = []
        try:
            data = d.execute_script("return (window.__sametTrace && window.__sametTrace.listeners) ? window.__sametTrace.listeners.slice(-300) : [];")
            if isinstance(data, list):
                # If selector given, filter by matching tag#id substring in the registry
                if sel:
                    try:
                        info = d.execute_script(
                            "var el=document.querySelector(arguments[0]); if(!el) return null;"
                            "return {tag: el.tagName.toLowerCase(), id: el.id||'', cls: (el.className||'').toString().slice(0,80)};",
                            sel,
                        ) or {}
                        tag = str(info.get("tag") or "").lower()
                        eid = str(info.get("id") or "")
                        js_listeners = [x for x in data if tag in str(x.get("tag","")).lower() or (eid and eid in str(x.get("id","")))]
                        if not js_listeners:
                            js_listeners = data[-30:]
                    except Exception:
                        js_listeners = data[-30:]
                else:
                    js_listeners = data[-80:]
        except Exception:
            pass

        # 3) Live probing: ask the element itself via JS (getEventListeners is DevTools-only, not page JS)
        probe = {}
        if sel:
            try:
                probe = d.execute_script(
                    "var el=document.querySelector(arguments[0]); if(!el) return {found:false};"
                    "var html=(el.outerHTML||'').slice(0,600);"
                    "var attrs={onclick: !!el.onclick, onmousedown: !!el.onmousedown};"
                    "return {found:true, tag: el.tagName.toLowerCase(), id: el.id||'', html: html, handlers: attrs};",
                    sel,
                ) or {}
            except Exception:
                probe = {}

        return {"selector": sel, "cdp": cdp_listeners, "js_registry": js_listeners, "probe": probe}

    def _build_replay_snippet(self, reqs: list[dict]) -> dict:
        """From network records captured after a click, build a copy-paste console JS snippet."""
        if not reqs:
            return {"js": "", "warning": "Click sonrasi hic network istegi yakalanmadi — replay icin kaynak yok."}
        # Filter to actionable HTTP requests (skip doc navigations / preflights)
        actionable = []
        for r in reqs:
            url = str(r.get("url") or "")
            method = str(r.get("method") or "GET").upper()
            if not url or url.startswith("data:") or url.startswith("chrome"):
                continue
            actionable.append(r)
        if not actionable:
            return {"js": "", "warning": "Yakalanan istekler arasinda replay'e uygun HTTP istegi yok."}
        # Gather warnings about auth
        warnings: list[str] = []
        # Heuristic: CSRF tokens / xsrf / anti-forgery often required
        # We expose what we captured as headers so the user can see vs guess.
        snippets: list[str] = []
        snippets.append("// == Samet replay — like click zinciri ==")
        snippets.append("// Sayfada oturum acikken F12 Console'a yapistir ve Enter'a bas.")
        snippets.append("// Like butonuna BASMADAN ayni istek(ler) tekrar gonderilir.")
        snippets.append("// Not: site HttpOnly/Csrf korumasi kullaniyorsa tarayici cookie'leri")
        snippets.append("// otomatik gonderilir; fetch'te credentials:'include' korunur.")
        snippets.append("")
        for idx, r in enumerate(actionable, 1):
            rid = str(r.get("id") or "")
            full = self.requests.get(rid) or r  # prefer full record for headers/body
            url = str(full.get("url") or r.get("url") or "")
            method = str(full.get("method") or r.get("method") or "GET").upper()
            headers = dict(full.get("headers") or {})
            post_data = full.get("post_data") or full.get("postData") or r.get("post_data") or ""
            # Keep only meaningful headers; drop noisy chrome internals
            drop_prefixes = ("sec-ch-", "sec-fetch", ":")
            keep_headers = {k: v for k, v in headers.items()
                            if not any(k.lower().startswith(p) for p in drop_prefixes)
                            and k.lower() not in ("host", "connection", "content-length")}
            # Detect CSRF-ish requirement
            for k in keep_headers:
                lk = k.lower()
                if "csrf" in lk or "xsrf" in lk or "x-csrftoken" in lk or "requestverificationtoken" in lk:
                    warnings.append(f"Istek {idx}: CSRF header'i var ({k}) — sayfada gecerli token'la calisir, expiry olabilir.")
            # Body
            body_expr = "undefined"
            body_comment = ""
            if post_data:
                # Try to keep it as JSON if it parses
                try:
                    parsed = json.loads(post_data) if isinstance(post_data, str) else post_data
                    body_expr = f"JSON.stringify({json.dumps(parsed, ensure_ascii=False)})"
                except Exception:
                    body_expr = json.dumps(str(post_data), ensure_ascii=False)
                    if len(str(post_data)) > 400:
                        body_comment = " // uzun body kirpilmis olabilir — network dump'ina bak"
            snippets.append(f"// --- #{idx} {method} {url[:120]} ---")
            snippets.append(f"await (async () => {{")
            snippets.append(f"  const url = {json.dumps(url, ensure_ascii=False)};")
            snippets.append(f"  const method = {json.dumps(method)};")
            snippets.append(f"  const headers = {json.dumps(keep_headers, ensure_ascii=False, indent=4)};")
            if body_expr != "undefined":
                snippets.append(f"  const body = {body_expr};{body_comment}")
            else:
                snippets.append(f"  const body = undefined;")
            # Try to auto-discover csrf token from page if header was x-csrf-token style
            snippets.append(f"  // CSRF fallback: sayfadaki meta/input'tan dene (varsa header'i doldur)")
            snippets.append(f"  try {{")
            snippets.append(f"    const m = document.querySelector('meta[name=csrf-token], meta[name=_csrf]');")
            snippets.append(f"    const t = (m && m.content) || (document.querySelector('input[name=_csrf], input[name=csrfToken]')||{{}}).value || '';")
            snippets.append(f"    if (t) {{")
            snippets.append(f"      if (!headers['x-csrf-token'] && !headers['X-CSRF-Token']) headers['x-csrf-token'] = t;")
            snippets.append(f"      if (!headers['X-XSRF-TOKEN'] && !headers['x-xsrf-token']) headers['X-XSRF-TOKEN'] = t;")
            snippets.append(f"    }}")
            snippets.append(f"  }} catch(e){{}}")
            snippets.append(f"  const opts = {{ method, headers, credentials: 'include' }};")
            snippets.append(f"  if (body !== undefined) opts.body = body;")
            snippets.append(f"  console.log('[replay #{idx}]', method, url, opts);")
            snippets.append(f"  const r = await fetch(url, opts);")
            snippets.append(f"  const text = await r.text();")
            snippets.append(f"  console.log('[replay #{idx} response]', r.status, r.statusText);")
            snippets.append(f"  try {{ console.log(JSON.parse(text)); }} catch(e) {{ console.log(text.slice(0,1200)); }}")
            snippets.append(f"  return {{status: r.status, body: text.slice(0,4000)}};")
            snippets.append(f"}})();")
            if idx < len(actionable):
                snippets.append("")
        snippets.append("")
        snippets.append("// Tum istekleri sirasiyla calistirmak icin yukaridaki bloklari sirayla Enter'la.")
        js = "\n".join(snippets)
        warn = " | ".join(warnings) if warnings else ""
        return {"js": js, "warnings": warn, "count": len(actionable)}

    def get_replay_snippet(self, selector: str = "", index: int | None = None) -> dict:
        """Build a console snippet from captured network diff."""
        self._drain_perf_log()
        sel = ""
        if selector or index is not None:
            try:
                sel = self._resolve_selector(index, selector)
            except Exception:
                sel = str(selector or index or "")
        trace: dict = {}
        try:
            trace = self.get_js_trace()
        except Exception:
            pass
        # If a real trace_click happened before, prefer its slice; otherwise use recents
        recent: list[dict] = []
        last_ids: set[str] = set()
        try:
            # Use the most recent click's network slice when available
            if getattr(self, "_last_trace_network", None):
                for r in self._last_trace_network:
                    rid = str(r.get("id") or "")
                    full = self.requests.get(rid)
                    recent.append(full or r)
                    if rid:
                        last_ids.add(rid)
        except Exception:
            pass
        if not recent:
            try:
                for rid in self._request_order[-12:]:
                    r = self.requests.get(rid)
                    if r:
                        recent.append(r)
            except Exception:
                recent = []
        snippet = self._build_replay_snippet(recent if recent else [])
        # Also build a DOM-event replay (dispatches click without using the handler chain)
        dom_replay = ""
        if sel:
            sel_js = json.dumps(sel, ensure_ascii=False)
            dom_replay = (
                f"// DOM click replay (alternatif — ayni listener zincirini tetikler):\n"
                f"(() => {{ const el = document.querySelector({sel_js}); if(!el) return console.error('Element not found: '+{sel_js});\n"
                f"  el.scrollIntoView({{behavior:'instant',block:'center'}});\n"
                f"  el.dispatchEvent(new PointerEvent('pointerdown', {{bubbles:true}}));\n"
                f"  el.dispatchEvent(new MouseEvent('mousedown', {{bubbles:true}}));\n"
                f"  el.dispatchEvent(new PointerEvent('pointerup', {{bubbles:true}}));\n"
                f"  el.dispatchEvent(new MouseEvent('mouseup', {{bubbles:true}}));\n"
                f"  el.dispatchEvent(new MouseEvent('click', {{bubbles:true}}));\n"
                f"  console.log('[replay] click dispatched on', el); }})();\n"
            )
        combined_js = (snippet.get("js") or "") + ("\n" + dom_replay if dom_replay else "")
        return {"ok": True, "selector": sel, "trace": trace, "snippet": combined_js,
                "fetch_snippet": snippet.get("js",""), "dom_snippet": dom_replay,
                "warnings": snippet.get("warnings",""), "count": snippet.get("count", 0),
                "hint": "fetch_snippet: like'a basmadan ayni API'yi cagirir (tercih). dom_snippet: butonu kodla tiklatir."}

    def trace_click(self, selector: str = "", index: int | None = None, wait_sec: float = 2.5) -> dict:
        """One-shot: arm trace, click, collect JS call chain + network diff, return explainable report."""
        sel = self._resolve_selector(index, selector) if (selector or index is not None) else ""
        if not sel:
            return {"ok": False, "error": "selector veya index gerekli — once browser_elements cagir."}
        self._ensure_trace()
        self.start_js_trace(clear=True)
        self._drain_perf_log()
        base_len = len(self._request_order)
        base_trace_len = 0
        try:
            base = self.start().execute_script("return (window.__sametTrace && window.__sametTrace.calls) ? window.__sametTrace.calls.length : 0;")
            base_trace_len = int(base or 0)
        except Exception:
            pass
        # --- click ---
        try:
            self.click(sel)
        except Exception as e:
            # still collect whatever we have
            pass
        # --- settle ---
        time.sleep(max(0.3, float(wait_sec or 0)))
        self._drain_perf_log()
        trace = self.get_js_trace()
        try:
            self.stop_js_trace()
        except Exception:
            pass
        # Network diff since click
        new_reqs = []
        try:
            for rid in self._request_order[base_len:]:
                r = self.requests.get(rid)
                if r:
                    new_reqs.append({k: r.get(k) for k in ("id","seq_index","type","method","url","response_status","mime","time_str") if r.get(k) is not None})
        except Exception:
            pass
        calls = trace.get("calls") or []
        # Only the slice that belongs to this click (best-effort)
        click_calls = calls[base_trace_len:] if base_trace_len < len(calls) else calls
        # Build human narrative for the agent to relay
        lines: list[str] = []
        lines.append(f"Element: {sel}  —  {len(click_calls)} JS olayı yakalandı, {len(new_reqs)} yeni network isteği")
        if sel:
            try:
                probe = self.get_event_listeners(sel)
                cdp = probe.get("cdp") or []
                jsr = probe.get("js_registry") or []
                if cdp or jsr:
                    lines.append(f"Bagli listener'lar: CDP={len(cdp)} JS-registry={len(jsr)}")
            except Exception:
                pass
        for i, c in enumerate(click_calls[:40], 1):
            kind = c.get("kind","?")
            tgt = c.get("target") or c.get("event") or ""
            url = c.get("url") or ""
            files = c.get("files") or []
            file_hint = (" <- " + " | ".join(files[:2])) if files else ""
            if kind == "listener":
                lines.append(f"{i}. [listener:{tgt}] tetiklendi{file_hint}")
            elif kind in ("fetch","xhr"):
                lines.append(f"{i}. [{kind} {c.get('method','?')} {url[:90]}]{file_hint}")
            elif kind in ("fetch:response","xhr:response"):
                lines.append(f"{i}.   -> yanit status={c.get('status')}{file_hint}")
            elif kind == "timeout":
                lines.append(f"{i}. [setTimeout delay={c.get('delay')}] icindeki callback{file_hint}")
            elif kind == "rAF":
                lines.append(f"{i}. [requestAnimationFrame] callback{file_hint}")
            elif kind == "console:error":
                lines.append(f"{i}. [console.error] {str(c.get('text',''))[:100]}{file_hint}")
            else:
                lines.append(f"{i}. [{kind} {tgt}]{file_hint}")
        if len(click_calls) > 40:
            lines.append(f"... {len(click_calls)-40} olay daha (get_js_trace ile tamami alinabilir)")
        if new_reqs:
            lines.append("Network (tiklamadan sonra):")
            for r in new_reqs[:12]:
                lines.append(f"  - {r.get('method','?')} {str(r.get('url',''))[:110]} -> {r.get('response_status','?')} ({r.get('mime','')})")
        # Page errors / console errors that happened in this window
        try:
            errs = list(self.page_errors[-5:])
            if errs:
                lines.append("Sayfa hatalari (son 5): " + " | ".join(str(e.get('text',''))[:80] for e in errs))
        except Exception:
            pass
        # Build replay JS from the precise network diff of THIS click
        replay = {}
        try:
            replay = self._build_replay_snippet([self.requests.get(str(r.get("id"))) or r for r in new_reqs])
        except Exception:
            replay = {"js": ""}
        # Remember for get_replay_snippet fallback
        try:
            self._last_trace_network = list(new_reqs)
            self._last_trace_calls = list(click_calls)
        except Exception:
            pass
        # Append replay snippet to human narrative so the model hands it over without a second call
        if replay.get("js"):
            lines.append("")
            lines.append("=== Konsolda calistirilacak replay kodu (butona basmadan ayni isi yapar) ===")
            lines.append(replay["js"])
            if replay.get("warnings"):
                lines.append(f"UYARI: {replay['warnings']}")
        explain = "\n".join(lines)
        return {
            "ok": True,
            "selector": sel,
            "explain": explain,
            "calls": click_calls,
            "calls_total": len(calls),
            "listeners": trace.get("listeners") or [],
            "network": new_reqs,
            "replay_js": replay.get("js", ""),
            "replay_warnings": replay.get("warnings", ""),
            "hint": "Replay kodunu kendi bilgisayarinda site acikken F12 > Console'a yapistir. Ayrica browser_get_replay_snippet() ile tekrar alabilirsin.",
        }


    # ======================================================= GHOSTWIRE (single-Chrome adapter)
    def _gw_close_ws(self) -> None:
        ws = getattr(self, "_gw_ws", None)
        self._gw_ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _gw_cleanup_locked(self) -> None:
        # called with state_lock held from reset()
        try:
            oracle = getattr(self, "_gw_oracle", None)
            if oracle is not None:
                try:
                    oracle.close()
                except Exception:
                    pass
        except Exception:
            pass
        # clear tracer state but keep object for reuse when possible
        try:
            tracer = getattr(self, "_gw_tracer", None)
            if tracer is not None:
                try:
                    with tracer.lock:
                        tracer.captures.clear()
                        tracer.pairs.clear()
                    tracer.hooks.clear()
                    tracer.ret_bps.clear()
                    tracer.by_location.clear()
                except Exception:
                    pass
        except Exception:
            pass
        # drop engine/probe refs — they are bound to the old driver
        for attr in ("_gw_engine", "_gw_tracer", "_gw_oracle", "_gw_live", "_gw_crypto", "_gw_strings", "_gw_vm", "_gw_origin", "_gw_dataflow", "_gw_watcher"):
            try:
                setattr(self, attr, None)
            except Exception:
                pass

    def ensure_ghostwire(self, blackbox: str = "") -> dict:
        """Idempotent lazy init of the Ghostwire adapter on the current Chrome.

        Returns a small status dict.  Safe to call repeatedly; reuses the
        existing adapter when the session is still alive.
        """
        with self.state_lock:
            if GhostwireEngine is None or Tracer is None:
                raise RuntimeError("Ghostwire modules not available (import failed)")
            d = self.start()
            # (re)create WS bridge if needed
            if getattr(self, "_gw_ws", None) is None or not getattr(self._gw_ws, "alive", False):
                if CdpWsBridge is not None:
                    try:
                        self._gw_ws = CdpWsBridge(d)
                    except Exception as e:
                        import logging as _lg
                        _lg.getLogger(__name__).debug("CdpWsBridge create failed: %s", e)
                        self._gw_ws = None
            if getattr(self, "_gw_engine", None) is not None and self._is_session_alive():
                # ensure Debugger/HeapProfiler domains are enabled (in case driver was started pre-gw)
                try:
                    self._enable_cdp_domains(d)
                except Exception:
                    pass
                if blackbox:
                    try:
                        pats = [p.strip() for p in blackbox.split(",") if p.strip()]
                        d.execute_cdp_cmd("Debugger.setBlackboxPatterns", {"patterns": pats})
                    except Exception:
                        pass
                return {"ok": True, "reused": True, "ws_alive": bool(getattr(self._gw_ws, "alive", False))}
            # fresh engine + probes
            engine = GhostwireEngine(d, ws_bridge=getattr(self, "_gw_ws", None))
            tracer = Tracer(engine)
            tracer.attach(engine)
            oracle = Oracle(engine, tracer)
            live = LiveObjects(engine)
            crypto = CryptoLogger(engine)
            strings = StringDumper(engine)
            vm = VMTracer(engine)
            origin = OriginTracer(engine)
            dataflow = DataflowTracer(engine)
            watcher = ScriptWatcher(engine, fetch_source=True)
            try:
                watcher.attach(engine)
            except Exception:
                pass
            self._gw_engine = engine
            self._gw_tracer = tracer
            self._gw_oracle = oracle
            self._gw_live = live
            self._gw_crypto = crypto
            self._gw_strings = strings
            self._gw_vm = vm
            self._gw_origin = origin
            self._gw_dataflow = dataflow
            self._gw_watcher = watcher
            try:
                self._enable_cdp_domains(d)
            except Exception:
                pass
            if blackbox:
                try:
                    pats = [p.strip() for p in blackbox.split(",") if p.strip()]
                    d.execute_cdp_cmd("Debugger.setBlackboxPatterns", {"patterns": pats})
                except Exception:
                    pass
            return {"ok": True, "reused": False, "ws_alive": bool(getattr(self._gw_ws, "alive", False))}

    def _gw_require(self):
        if getattr(self, "_gw_engine", None) is None:
            self.ensure_ghostwire()
        eng = getattr(self, "_gw_engine", None)
        if eng is None:
            raise RuntimeError("Ghostwire engine not initialised")
        return eng

    # -- thin wrappers used by MCP gw_* tools ---------------------------------
    def gw_targets(self) -> list:
        eng = self._gw_require()
        return eng.targets()

    def gw_eval(self, expression: str, target_url: str = "") -> Any:
        eng = self._gw_require()
        sid = eng.resolve_session(target_url or None)
        r = eng.send("Runtime.evaluate", {"expression": GW + expression, "returnByValue": True, "silent": True}, session_id=sid)
        if r.get("exceptionDetails"):
            details = r["exceptionDetails"]
            exc = (details.get("exception") or {}).get("description") or details.get("text") or "exception"
            raise RuntimeError(exc)
        res = r.get("result", {})
        # returnByValue already unwraps; handle object refs
        if "value" in res:
            return res["value"]
        return res.get("description") or res.get("type")

    def gw_hook(self, expression: str, target_url: str = "", capture_returns: bool = False, label: str = "") -> dict:
        self._gw_require()
        tracer = getattr(self, "_gw_tracer", None)
        tracer.hook(expression, target_url=target_url or None, capture_returns=capture_returns, label=label or None)
        return {"ok": True, "expression": expression, "label": label or expression, "capture_returns": bool(capture_returns), "target_url": target_url or "page"}

    def gw_captures(self, limit: int = 50) -> list[dict]:
        tracer = getattr(self, "_gw_tracer", None)
        if tracer is None:
            return []
        with tracer.lock:
            return list(tracer.captures[-int(limit):])

    def gw_corpus(self, label: str = "", limit: int = 20, full: bool = False) -> dict:
        oracle = getattr(self, "_gw_oracle", None)
        tracer = getattr(self, "_gw_tracer", None)
        if oracle is None or tracer is None:
            self.ensure_ghostwire()
            oracle = getattr(self, "_gw_oracle", None)
            tracer = getattr(self, "_gw_tracer", None)
        if not label:
            with tracer.lock:
                return {k: len(v) for k, v in tracer.pairs.items()}
        pairs = oracle.corpus(label)
        total = len(pairs)
        shown = pairs[: int(limit)] if not full else pairs

        def _clip(v, n=200):
            s = json.dumps(v, ensure_ascii=False, default=str)
            return s if len(s) <= n else s[:n] + "…"

        if full:
            return {"label": label, "total": total, "shown": len(shown), "pairs": shown}
        clipped = []
        for p in shown:
            clipped.append({"input": json.loads(_clip(p.get("input"), 400)) if isinstance(p.get("input"), str) else p.get("input"), "output": p.get("output"), "output_type": p.get("output_type"), "unserialized": p.get("unserialized")})
            # fallback: keep original input if clip broke json
            if isinstance(clipped[-1]["input"], str) and clipped[-1]["input"].startswith("<"):
                clipped[-1]["input"] = p.get("input")
        return {"label": label, "total": total, "shown": len(clipped), "pairs": clipped}

    def gw_verify(self, real_fn_expr: str, candidate: str, label: str = "", fresh_inputs: str = "", target_url: str = "", sample: int = 200, max_mismatches: int = 5) -> dict:
        oracle = getattr(self, "_gw_oracle", None)
        if oracle is None:
            self.ensure_ghostwire()
            oracle = getattr(self, "_gw_oracle", None)
        fresh = None
        if fresh_inputs and str(fresh_inputs).strip():
            try:
                fresh = json.loads(fresh_inputs)
            except Exception as e:
                raise RuntimeError(f"fresh_inputs must be JSON array-of-arglists: {e}")
        return oracle.verify(real_fn_expr, candidate, label=label or None, fresh_inputs=fresh, target_url=target_url or None, sample=int(sample), max_mismatches=int(max_mismatches))

    def gw_objects(self, value: str = "", constructor: str = "", key: str = "", target_url: str = "", limit: int = 20) -> list[dict]:
        eng = self._gw_require()
        sid = eng.resolve_session(target_url or None)
        snap = take_snapshot(eng, sid)
        return snap.find_objects(value=value or None, constructor=constructor or None, key=key or None, limit=int(limit))

    def gw_strings(self, decoder: str, start: int = 0, count: int = 256, extra_args: str = "", target_url: str = "") -> dict:
        dumper = getattr(self, "_gw_strings", None)
        if dumper is None:
            self.ensure_ghostwire()
            dumper = getattr(self, "_gw_strings", None)
        extra = None
        if extra_args and str(extra_args).strip():
            try:
                extra = json.loads(extra_args)
            except Exception as e:
                raise RuntimeError(f"extra_args must be JSON array: {e}")
        return dumper.dump(decoder, start=int(start), count=int(count), extra_args=extra, target_url=target_url or None)

    def gw_vm(self, url_substr: str, line: int, watch: str = "", column: int = 0, iterations: int = 300, trigger: str = "", target_url: str = "") -> dict:
        vm = getattr(self, "_gw_vm", None)
        if vm is None:
            self.ensure_ghostwire()
            vm = getattr(self, "_gw_vm", None)
        watch_list = [w.strip() for w in watch.split(",") if w.strip()] if watch else []
        return vm.watch(url_substr, int(line), column=int(column), watch=watch_list, iterations=int(iterations), trigger=trigger or None, target_url=target_url or None)

    def gw_crypto(self, seconds: float = 4.0, target_url: str = "") -> list[dict]:
        cry = getattr(self, "_gw_crypto", None)
        if cry is None:
            self.ensure_ghostwire()
            cry = getattr(self, "_gw_crypto", None)
        return cry.capture(duration=float(seconds), target_url=target_url or None)

    def gw_heapdiff(self, trigger: str, target_url: str = "", wait_ms: int = 300) -> dict:
        eng = self._gw_require()
        sid = eng.resolve_session(target_url or None)
        before = take_snapshot(eng, sid)
        eng.send("Runtime.evaluate", {"expression": GW + trigger, "awaitPromise": True, "silent": True}, session_id=sid)
        time.sleep(max(0, int(wait_ms)) / 1000.0)
        after = take_snapshot(eng, sid)
        return diff_snapshots(before, after)

    def gw_patch(self, value: str = "", constructor: str = "", key: str = "", node_id: int = 0, set_props: str = "", apply: str = "", target_url: str = "") -> dict:
        live = getattr(self, "_gw_live", None)
        if live is None:
            self.ensure_ghostwire()
            live = getattr(self, "_gw_live", None)
        assign = None
        if set_props and str(set_props).strip():
            try:
                assign = json.loads(set_props)
            except Exception as e:
                raise RuntimeError(f"set_props must be JSON object: {e}")
        nid = int(node_id) if node_id else None
        return live.patch(node_id=nid, value=value or None, constructor=constructor or None, key=key or None, assign=assign, apply=apply or None, target_url=target_url or None)

    def gw_read(self, value: str = "", constructor: str = "", key: str = "", node_id: int = 0, target_url: str = "") -> dict:
        live = getattr(self, "_gw_live", None)
        if live is None:
            self.ensure_ghostwire()
            live = getattr(self, "_gw_live", None)
        nid = int(node_id) if node_id else None
        return live.read(node_id=nid, value=value or None, constructor=constructor or None, key=key or None, target_url=target_url or None)

    def gw_origin(self, value: str, enter: str, trigger: str = "", target_url: str = "", blackbox: str = "", max_steps: int = 300) -> dict:
        tracer = getattr(self, "_gw_origin", None)
        if tracer is None:
            self.ensure_ghostwire()
            tracer = getattr(self, "_gw_origin", None)
        bb = [p.strip() for p in blackbox.split(",") if p.strip()] if blackbox else None
        return tracer.trace(value, enter, trigger=trigger or None, target_url=target_url or None, blackbox=bb, max_steps=int(max_steps))

    def gw_follow(self, producer: str, trigger: str = "", target_url: str = "", blackbox: str = "") -> dict:
        tracer = getattr(self, "_gw_dataflow", None)
        if tracer is None:
            self.ensure_ghostwire()
            tracer = getattr(self, "_gw_dataflow", None)
        bb = [p.strip() for p in blackbox.split(",") if p.strip()] if blackbox else None
        return tracer.follow(producer, trigger=trigger or None, target_url=target_url or None, blackbox=bb)

    def gw_scripts(self, search: str = "", dynamic_only: bool = False, full: bool = False) -> dict:
        watcher = getattr(self, "_gw_watcher", None)
        if watcher is None:
            self.ensure_ghostwire()
            watcher = getattr(self, "_gw_watcher", None)
        if dynamic_only:
            items = watcher.dynamic_only()
        elif search:
            items = watcher.search(search)
        else:
            with watcher.lock:
                items = list(watcher.scripts)
        total = len(items)
        cap = None if full else 40
        shown = items if cap is None else items[:cap]
        # truncate source
        out = []
        for s in shown:
            rec = dict(s)
            if not full and rec.get("source") and len(rec["source"]) > 300:
                rec["source"] = rec["source"][:300] + "…"
            out.append(rec)
        return {"total": total, "shown": len(out), "scripts": out}

    def gw_network(self, url_substr: str = "", full_bodies: bool = False) -> list[dict]:
        # cross-target alias — for single-target this is the existing network capture
        self._drain_perf_log()
        rows = self.get_network_requests(pattern=url_substr or "", limit=60)
        if not full_bodies:
            for r in rows:
                if r.get("response_body") and len(str(r["response_body"])) > 400:
                    r = dict(r)
                    r["response_body"] = str(r["response_body"])[:400] + "…"
        return rows

    def gw_navigate(self, url: str) -> dict:
        d = self.open(url)
        return {"url": getattr(d, "current_url", url), "title": getattr(d, "title", "")}

    def gw_save(self, path: str) -> dict:
        eng = self._gw_require()
        tracer = getattr(self, "_gw_tracer", None)
        watcher = getattr(self, "_gw_watcher", None)
        with tracer.lock if tracer else threading.Lock():
            caps = list(tracer.captures) if tracer else []
            corpora = {k: list(v) for k, v in (tracer.pairs.items() if tracer else {})}
        with watcher.lock if watcher else threading.Lock():
            scripts = list(watcher.scripts) if watcher else []
        dump = {"targets": eng.targets(), "scripts": scripts, "captures": caps, "corpora": corpora, "network": list(self.requests.values())[:200]}
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {"ok": True, "path": str(p), "targets": len(dump["targets"]), "captures": len(caps)}

    def gw_close(self) -> dict:
        # close oracle scratch + clear tracer, keep browser alive
        try:
            oracle = getattr(self, "_gw_oracle", None)
            if oracle is not None:
                oracle.close()
        except Exception:
            pass
        try:
            tracer = getattr(self, "_gw_tracer", None)
            if tracer is not None:
                with tracer.lock:
                    tracer.captures.clear()
                    tracer.pairs.clear()
        except Exception:
            pass
        return {"ok": True}

    def gw_attach(self, url: str, blackbox: str = "") -> dict:
        d = self.open(url)
        st = self.ensure_ghostwire(blackbox=blackbox or "")
        return {"ok": True, "url": getattr(d, "current_url", url), "title": getattr(d, "title", ""), "ghostwire": st}


# ---------------------------------------------------------------------------
INSTALL_HINT = (
    "Browser tools unavailable (SeleniumBase UC mode). Install with:\n"
    "  pip install seleniumbase\n"
    "Chrome must also be installed (UC mode uses real Chrome). "
    "Meanwhile you can verify via shell commands instead."
)

