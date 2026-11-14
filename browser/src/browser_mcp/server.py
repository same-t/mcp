"""samet-browser MCP server — full browser API over stdio.

Every browser capability is exposed here over MCP (stdio), backed by
SeleniumBase UC mode (Driver(uc=True) + CDP network capture).

IMPORTANT: Always call browser_elements FIRST before clicking/typing.
The agent must use element indices, not guessed selectors.

Run standalone:
    python -m browser_mcp.server
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .browser_session import BrowserSession, INSTALL_HINT

mcp = FastMCP("samet-browser")

_bs: BrowserSession | None = None


def _bs_get() -> BrowserSession:
    global _bs
    if _bs is None:
        _bs = BrowserSession()
        # Durable network capture lands in <workspace>/.samet/network/ so a
        # crash, an MCP restart or a FIFO eviction never loses traffic.
        try:
            from .util import _workspace

            _bs.set_persist_workspace(_workspace())
        except Exception:
            pass
    return _bs


import anyio


def _sync(fn, *args):
    return anyio.to_thread.run_sync(lambda: fn(*args))


def _err_msg(e: Exception) -> str:
    """Format a tool error; only mention INSTALL_HINT for real setup problems.

    Transient page errors (timeouts, navigation failures, etc.) must NOT
    destroy the session: resetting would kill cookies/storage and wipe all
    captured network traffic mid-task.
    """
    text = f"{type(e).__name__}: {e}"
    low = text.lower()
    if "seleniumbase" in low or "no chrome" in low or "chromedriver" in low and "not" in low:
        return f"{text} | {INSTALL_HINT}"
    return text


# =============================================================================
# NAVIGATION
# =============================================================================

@mcp.tool()
async def browser_goto(url: str) -> str:
    """Navigate to the given URL. Returns final URL + title as JSON."""
    bs = _bs_get()
    try:
        return await _sync(bs.goto, url)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def browser_open(url: str) -> str:
    """Open a URL in SeleniumBase UC-mode Chrome and return page title + visible text.
    Network capture is automatic; after interacting with a flow, dump real requests with browser_network."""
    bs = _bs_get()
    try:
        d = await _sync(bs.open, url)
        cur = (d.current_url or "").lower()
        # eb5c03bb hardening — if Chrome landed on an internal page, retry once
        # from the auto-saved session (covers the "oturum uçtu" regression).
        if cur.startswith("chrome://") or cur == "about:blank":
            try:
                snap = await _sync(bs.auto_restore_session)
                if "restored" in snap.lower():
                    d = await _sync(bs.open, url)
                    cur = (d.current_url or "").lower()
            except Exception:
                pass
            if cur.startswith("chrome://") or cur == "about:blank":
                return (
                    f"RETRY: open landed on internal page ({d.current_url or cur}). "
                    "Attempted auto_restore_session and retry. If still on chrome://, "
                    "the browser session was reset — cookies were re-applied from the "
                    "auto-saved snapshot where available. Retry browser_open or restore a specific snapshot via browser_restore_session."
                )
        title = d.title
        body = bs.body_text(d)
        return f"URL: {d.current_url}\nTitle: {title}\n--- visible text ---\n{body}"
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def browser_back() -> str:
    """Go to the previous page. Returns new URL + title."""
    return await _sync(_bs_get().back)


@mcp.tool()
async def browser_forward() -> str:
    """Go to the next page. Returns new URL + title."""
    return await _sync(_bs_get().forward)


@mcp.tool()
async def browser_reload() -> str:
    """Reload the current page. Returns URL + title."""
    return await _sync(_bs_get().reload)


@mcp.tool()
async def browser_get_url() -> str:
    """Return the current page URL."""
    return await _sync(_bs_get().get_url)


@mcp.tool()
async def browser_get_title() -> str:
    """Return the current page title."""
    return await _sync(_bs_get().get_title)


@mcp.tool()
async def browser_wait_for_url(pattern: str, timeout: float = 10.0) -> str:
    """Wait until the current URL contains or matches the given pattern."""
    try:
        return await _sync(_bs_get().wait_for_url, pattern, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_wait_for_load_state(state: str = "load", timeout: float = 15.0) -> str:
    """Wait for the page to reach the given load state: 'load', 'domcontentloaded', or 'networkidle'."""
    try:
        return await _sync(_bs_get().wait_for_load_state, state, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_navigation_history() -> str:
    """Return the last 10 navigation history entries as JSON."""
    result = await _sync(_bs_get().navigation_history)
    return json.dumps(result, ensure_ascii=False)


# =============================================================================
# OBSERVATION — Elements + DOM
# =============================================================================

@mcp.tool()
async def browser_elements() -> str:
    """List ALL interactive elements on the current page with index numbers, labels, roles, and positions.
    ALWAYS call this before clicking/typing — use the returned index in browser_click/browser_type.
    Each entry includes: index, tag, type, id, name, label, aria_label, role, placeholder, text, href, disabled, checked, visible, rect."""
    bs = _bs_get()
    try:
        rows = await _sync(bs.elements)
    except Exception as e:
        raise RuntimeError(f"element listesi alinamadi: {e}")
    if not rows:
        return "Sayfada etkilesimli element yok."
    lines = []
    for r in rows:
        vis = "" if r.get("vis") else " [GIZLI]"
        dis = " [DISABLED]" if r.get("disabled") else ""
        occ = " [OCCLUDED/OVERLAID]" if r.get("occluded") else ""
        scr = " [NEEDS SCROLL]" if r.get("needs_scroll") else ""
        desc = f"<{r['tag']}"
        if r.get("type"):
            desc += f" type={r['type']}"
        if r.get("id"):
            desc += f" #{r['id']}"
        if r.get("name"):
            desc += f" name={r['name']}"
        if r.get("ph"):
            desc += f" placeholder='{r['ph']}'"
        if r.get("label"):
            desc += f" label='{r['label']}'"
        if r.get("aria_label"):
            desc += f" aria-label='{r['aria_label']}'"
        if r.get("role") and r["role"] not in (r["tag"], ""):
            desc += f" role={r['role']}"
        desc += ">"
        label = f" '{r['txt']}'" if r.get("txt") else ""
        href = f" → {r['href'][:60]}" if r.get("href") else ""
        rect = r.get("rect", {})
        pos = f" [{rect.get('x')},{rect.get('y')} {rect.get('w')}×{rect.get('h')}]" if rect else ""
        lines.append(f"[{r['i']}] {desc}{label}{href}{vis}{dis}{occ}{scr}{pos}")
    return "\n".join(lines)



@mcp.tool()
async def browser_snapshot(js: str = "") -> str:
    """Re-read current page: URL, title, visible text, plus optional JS expression result.
    Use as verification evidence."""
    bs = _bs_get()

    def snap():
        d = bs.start()
        extra = ""
        if js.strip():
            try:
                extra = f"\n--- js result ---\n{d.execute_script('return ' + js.strip())}"
            except Exception as e:
                extra = f"\n--- js error ---\n{e}"
        return f"URL: {d.current_url}\nTitle: {d.title}\n--- visible text ---\n{bs.body_text(d)}{extra}"

    return await _sync(snap)


@mcp.tool()
async def browser_find(selector: str) -> str:
    """Find a single element by CSS selector. Returns element details as JSON."""
    result = await _sync(_bs_get().find, selector)
    return json.dumps(result, ensure_ascii=False) if result else f"Element not found: {selector}"


@mcp.tool()
async def browser_find_all(selector: str) -> str:
    """Find all elements matching the CSS selector. Returns JSON array."""
    result = await _sync(_bs_get().find_all, selector)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_find_by_text(text: str) -> str:
    """Find an element by its visible text content. Returns JSON."""
    result = await _sync(_bs_get().find_by_text, text)
    return json.dumps(result, ensure_ascii=False) if result else f"No element found with text: {text}"


@mcp.tool()
async def browser_find_by_role(role: str, name: str = "") -> str:
    """Find an element by ARIA role and optional accessible name. Returns JSON."""
    result = await _sync(_bs_get().find_by_role, role, name or None)
    return json.dumps(result, ensure_ascii=False) if result else f"No element found with role={role}"


@mcp.tool()
async def browser_find_by_label(label: str) -> str:
    """Find an input element by its label text. Returns JSON."""
    result = await _sync(_bs_get().find_by_label, label)
    return json.dumps(result, ensure_ascii=False) if result else f"No input found for label: {label}"


@mcp.tool()
async def browser_find_by_placeholder(text: str) -> str:
    """Find an input element by placeholder text. Returns JSON."""
    result = await _sync(_bs_get().find_by_placeholder, text)
    return json.dumps(result, ensure_ascii=False) if result else f"No input found with placeholder: {text}"


@mcp.tool()
async def browser_get_element(selector: str = "", index: int | None = None) -> str:
    """Get detailed info about an element by CSS selector or index from browser_elements."""
    try:
        result = await _sync(_bs_get().get_element_detail, selector, index)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        raise RuntimeError(f"get_element failed: {e}")


@mcp.tool()
async def browser_get_dom(selector: str = "") -> str:
    """Return the outer HTML of an element (or the whole page if no selector)."""
    return await _sync(_bs_get().get_dom, selector or None)


@mcp.tool()
async def browser_get_text(selector: str = "") -> str:
    """Return visible text content of an element or the whole page body."""
    return await _sync(_bs_get().get_text, selector or None)


@mcp.tool()
async def browser_get_attribute(selector: str, attribute: str) -> str:
    """Return the value of an HTML attribute on the matched element."""
    result = await _sync(_bs_get().get_attribute, selector, attribute)
    return str(result) if result is not None else f"Attribute '{attribute}' not found on {selector}"


@mcp.tool()
async def browser_is_visible(selector: str) -> str:
    """Check if an element is visible. Returns 'true' or 'false'."""
    result = await _sync(_bs_get().is_visible, selector)
    return str(result).lower()


@mcp.tool()
async def browser_is_enabled(selector: str) -> str:
    """Check if an element is enabled (not disabled). Returns 'true' or 'false'."""
    result = await _sync(_bs_get().is_enabled, selector)
    return str(result).lower()


@mcp.tool()
async def browser_is_checked(selector: str) -> str:
    """Check if a checkbox or radio button is checked. Returns 'true' or 'false'."""
    result = await _sync(_bs_get().is_checked, selector)
    return str(result).lower()


@mcp.tool()
async def browser_get_outer_html(selector: str) -> str:
    """Return the outerHTML of the matched element."""
    return await _sync(_bs_get().get_outer_html, selector)


@mcp.tool()
async def browser_get_inner_html(selector: str) -> str:
    """Return the innerHTML of the matched element."""
    return await _sync(_bs_get().get_inner_html, selector)


# =============================================================================
# INTERACTION — Mouse
# =============================================================================

@mcp.tool()
async def browser_click(selector: str = "", index: int | None = None) -> str:
    """Click a page element. PREFERRED: pass index from browser_elements (reliable); selector as CSS fallback."""
    bs = _bs_get()
    try:
        await _sync(bs.click, selector, index)
    except Exception as e:
        raise RuntimeError(f"click failed: {str(e).splitlines()[0][:200]}")
    target = f"#{index}" if index is not None else selector
    return f"OK: clicked {target}. Use browser_snapshot or browser_elements to verify."


@mcp.tool()
async def browser_double_click(selector: str = "", index: int | None = None) -> str:
    """Double-click an element. Pass index from browser_elements or a CSS selector."""
    try:
        await _sync(_bs_get().double_click, selector, index)
    except Exception as e:
        raise RuntimeError(f"double_click failed: {e}")
    return f"OK: double-clicked {'#' + str(index) if index is not None else selector}"


@mcp.tool()
async def browser_right_click(selector: str = "", index: int | None = None) -> str:
    """Right-click (context menu) an element."""
    try:
        await _sync(_bs_get().right_click, selector, index)
    except Exception as e:
        raise RuntimeError(f"right_click failed: {e}")
    return f"OK: right-clicked {'#' + str(index) if index is not None else selector}"


@mcp.tool()
async def browser_hover(selector: str = "", index: int | None = None) -> str:
    """Move the mouse over an element to trigger hover effects."""
    try:
        await _sync(_bs_get().hover, selector, index)
    except Exception as e:
        raise RuntimeError(f"hover failed: {e}")
    return "OK: hovered"


@mcp.tool()
async def browser_move_mouse(x: int, y: int) -> str:
    """Move the mouse cursor to the given absolute coordinates."""
    await _sync(_bs_get().move_mouse, x, y)
    return f"OK: mouse moved to ({x}, {y})"


@mcp.tool()
async def browser_mouse_down() -> str:
    """Press and hold the left mouse button."""
    await _sync(_bs_get().mouse_down)
    return "OK: mouse button held"


@mcp.tool()
async def browser_mouse_up() -> str:
    """Release the left mouse button."""
    await _sync(_bs_get().mouse_up)
    return "OK: mouse button released"


@mcp.tool()
async def browser_drag(source_selector: str, target_selector: str) -> str:
    """Drag an element from source to target using CSS selectors."""
    try:
        await _sync(_bs_get().drag, source_selector, target_selector)
    except Exception as e:
        raise RuntimeError(f"drag failed: {e}")
    return f"OK: dragged {source_selector} → {target_selector}"


@mcp.tool()
async def browser_scroll(x: int = 0, y: int = 500) -> str:
    """Scroll the page by (x, y) pixels. Positive y scrolls down."""
    await _sync(_bs_get().scroll, x, y)
    return f"OK: scrolled by ({x}, {y})"


@mcp.tool()
async def browser_scroll_to(selector: str = "", index: int | None = None) -> str:
    """Scroll the element into view."""
    try:
        await _sync(_bs_get().scroll_to, selector, index)
    except Exception as e:
        raise RuntimeError(f"scroll_to failed: {e}")
    return "OK: scrolled to element"


@mcp.tool()
async def browser_click_text(text: str) -> str:
    """Click the first visible element whose text matches the given string (no selector needed)."""
    try:
        return await _sync(_bs_get().click_text, text)
    except Exception as e:
        raise RuntimeError(str(e))


# =============================================================================
# INPUT — Keyboard / Form
# =============================================================================

@mcp.tool()
async def browser_type(selector: str = "", text: str = "", index: int | None = None) -> str:
    """Organic human-like typing: types each character individually with randomized delays (40–180ms per char),
    burst patterns, and micro-pauses — exactly like a real user. NO JS injection, NO clipboard paste.
    Preferred for bot-sensitive forms (registration, login, date pickers).
    Pass index from browser_elements or CSS selector."""
    bs = _bs_get()
    try:
        await _sync(bs.type_text, selector, text, index)
    except Exception as e:
        raise RuntimeError(f"type failed: {str(e).splitlines()[0][:200]}")
    target = f"#{index}" if index is not None else selector
    return f"OK: typed into {target}."


@mcp.tool()
async def browser_fill(selector: str = "", text: str = "", index: int | None = None) -> str:
    """Organic character-by-character fill with human-like timing. No JS value injection, no clipboard.
    Each keystroke is sent as a genuine OS keyboard event. Works for React/Vue controlled inputs,
    contenteditable, ProseMirror, standard inputs, and textareas. Bot-detection safe."""
    try:
        await _sync(_bs_get().fill, selector, text, index)
    except Exception as e:
        raise RuntimeError(f"fill failed: {e}")
    target = f"#{index}" if index is not None else selector
    return f"OK: filled {target}"


@mcp.tool()
async def browser_clear(selector: str = "", index: int | None = None) -> str:
    """Clear an input field."""
    try:
        await _sync(_bs_get().clear_input, selector, index)
    except Exception as e:
        raise RuntimeError(f"clear failed: {e}")
    return "OK: cleared"


@mcp.tool()
async def browser_press(key: str, selector: str = "", index: int | None = None) -> str:
    """Press a keyboard key. Examples: 'enter', 'tab', 'escape', 'space', 'arrow_down'.
    Optionally target a specific element by selector or index."""
    try:
        await _sync(_bs_get().press_key, key, selector, index)
    except Exception as e:
        raise RuntimeError(f"press failed: {e}")
    return f"OK: pressed '{key}'"


@mcp.tool()
async def browser_key_down(key: str) -> str:
    """Hold a key down (useful for modifier keys like shift, ctrl)."""
    await _sync(_bs_get().key_down, key)
    return f"OK: key_down '{key}'"


@mcp.tool()
async def browser_key_up(key: str) -> str:
    """Release a held key."""
    await _sync(_bs_get().key_up, key)
    return f"OK: key_up '{key}'"


@mcp.tool()
async def browser_hotkey(keys: list[str]) -> str:
    """Send a keyboard combination such as ['ctrl', 'c'] or ['ctrl', 'shift', 't']."""
    try:
        await _sync(lambda: _bs_get().hotkey(*keys))
    except Exception as e:
        raise RuntimeError(f"hotkey failed: {e}")
    return f"OK: hotkey {'+'.join(keys)}"


@mcp.tool()
async def browser_focus(selector: str = "", index: int | None = None) -> str:
    """Give keyboard focus to an element."""
    try:
        await _sync(_bs_get().focus, selector, index)
    except Exception as e:
        raise RuntimeError(f"focus failed: {e}")
    return "OK: focused"


@mcp.tool()
async def browser_select_option(selector: str = "", value: str = "", by: str = "auto", index: int | None = None) -> str:
    """Select an option from a <select> element.
    by: 'auto' (tries value then visible text), 'value', 'text', or 'index'.
    """
    try:
        msg = await _sync(_bs_get().select_option, selector, value, by, index)
        return f"OK: {msg}"
    except Exception as e:
        raise RuntimeError(f"select_option failed: {e}")



@mcp.tool()
async def browser_check(selector: str = "", index: int | None = None) -> str:
    """Check a checkbox or radio button."""
    try:
        await _sync(_bs_get().check_box, selector, index, True)
    except Exception as e:
        raise RuntimeError(f"check failed: {e}")
    return "OK: checked"


@mcp.tool()
async def browser_uncheck(selector: str = "", index: int | None = None) -> str:
    """Uncheck a checkbox."""
    try:
        await _sync(_bs_get().check_box, selector, index, False)
    except Exception as e:
        raise RuntimeError(f"uncheck failed: {e}")
    return "OK: unchecked"


@mcp.tool()
async def browser_upload_file(selector: str, path: str, index: int | None = None) -> str:
    """Upload a file to a file input element."""
    try:
        await _sync(_bs_get().upload_file, selector, path, index)
    except Exception as e:
        raise RuntimeError(f"upload_file failed: {e}")
    return f"OK: file '{path}' set on input"


@mcp.tool()
async def browser_submit(selector: str = "", index: int | None = None) -> str:
    """Submit the form containing the specified element."""
    try:
        await _sync(_bs_get().submit_form, selector, index)
    except Exception as e:
        raise RuntimeError(f"submit failed: {e}")
    return "OK: form submitted"


@mcp.tool()
async def browser_get_input_value(selector: str = "", index: int | None = None) -> str:
    """Return the current value of an input field."""
    try:
        return await _sync(_bs_get().get_input_value, selector, index)
    except Exception as e:
        raise RuntimeError(f"get_input_value failed: {e}")


@mcp.tool()
async def browser_type_into(label_or_text: str, value: str) -> str:
    """Find the best matching input by label or placeholder text and fill it (no selector needed)."""
    try:
        return await _sync(_bs_get().type_into_label, label_or_text, value)
    except Exception as e:
        raise RuntimeError(str(e))


# =============================================================================
# SCREENSHOT / VIEWPORT
# =============================================================================

@mcp.tool()
async def browser_screenshot(path: str = "") -> str:
    """Save a screenshot of the current page (PNG). Returns the file path."""
    from .util import screenshot_path
    bs = _bs_get()
    p = screenshot_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    await _sync(bs.screenshot, str(p))
    return f"OK: screenshot saved to {p}"


@mcp.tool()
async def browser_screenshot_element(selector: str = "", path: str = "", index: int | None = None) -> str:
    """Save a screenshot of a specific element."""
    from .util import screenshot_path
    p = screenshot_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _sync(_bs_get().screenshot_element, selector, str(p), index)
    except Exception as e:
        raise RuntimeError(f"screenshot_element failed: {e}")
    return f"OK: element screenshot saved to {p}"


@mcp.tool()
async def browser_screenshot_full_page(path: str = "") -> str:
    """Save a full-page screenshot (including content below the fold)."""
    from .util import screenshot_path
    p = screenshot_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _sync(_bs_get().screenshot_full_page, str(p))
    except Exception as e:
        raise RuntimeError(f"screenshot_full_page failed: {e}")
    return f"OK: full-page screenshot saved to {p}"


@mcp.tool()
async def browser_get_viewport() -> str:
    """Return the current viewport size as JSON {width, height}."""
    result = await _sync(_bs_get().get_viewport)
    return json.dumps(result)


@mcp.tool()
async def browser_set_viewport(width: int, height: int) -> str:
    """Set the browser viewport size."""
    await _sync(_bs_get().set_viewport, width, height)
    return f"OK: viewport set to {width}×{height}"


# =============================================================================
# JAVASCRIPT
# =============================================================================

@mcp.tool()
async def browser_evaluate(script: str) -> str:
    """Execute JavaScript in the browser and return the result."""
    try:
        result = await _sync(_bs_get().evaluate, script)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        raise RuntimeError(f"evaluate failed: {e}")


@mcp.tool()
async def browser_evaluate_on_element(selector: str, script: str, index: int | None = None) -> str:
    """Execute JavaScript with the matched element passed as 'arguments[0]'."""
    try:
        result = await _sync(_bs_get().evaluate_on_element, selector, script, index)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        raise RuntimeError(f"evaluate_on_element failed: {e}")


# =============================================================================
# WAIT
# =============================================================================

@mcp.tool()
async def browser_wait(seconds: float) -> str:
    """Wait for the given number of seconds."""
    await _sync(_bs_get().wait_sec, seconds)
    return f"OK: waited {seconds}s"


@mcp.tool()
async def browser_wait_for_selector(selector: str, timeout: float = 10.0) -> str:
    """Wait until the CSS selector matches an element on the page."""
    try:
        return await _sync(_bs_get().wait_for_selector, selector, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_wait_for_visible(selector: str, timeout: float = 10.0) -> str:
    """Wait until the element matching the selector becomes visible."""
    try:
        return await _sync(_bs_get().wait_for_visible, selector, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_wait_for_hidden(selector: str, timeout: float = 10.0) -> str:
    """Wait until the element matching the selector is hidden or removed from the DOM."""
    try:
        return await _sync(_bs_get().wait_for_hidden, selector, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_wait_for_text(text: str, timeout: float = 10.0) -> str:
    """Wait until the given text appears anywhere on the page."""
    try:
        return await _sync(_bs_get().wait_for_text_on_page, text, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_wait_for_network_idle(timeout: float = 10.0) -> str:
    """Wait until the page has finished loading and network is idle."""
    try:
        return await _sync(_bs_get().wait_for_network_idle, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_wait_for_function(script: str, timeout: float = 10.0) -> str:
    """Wait until the given JavaScript expression returns a truthy value."""
    try:
        return await _sync(_bs_get().wait_for_function, script, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


# =============================================================================
# TABS
# =============================================================================

@mcp.tool()
async def browser_new_tab(url: str = "") -> str:
    """Open a new browser tab, optionally navigating to the given URL."""
    return await _sync(_bs_get().new_tab, url or None)


@mcp.tool()
async def browser_close_tab() -> str:
    """Close the currently active tab."""
    await _sync(_bs_get().close_tab)
    return "OK: tab closed"


@mcp.tool()
async def browser_list_tabs() -> str:
    """List all open tabs with their index, handle, URL, and title."""
    result = await _sync(_bs_get().list_tabs)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_switch_tab(index: int | None = None, handle: str = "") -> str:
    """Switch to a tab by index (0-based) or window handle."""
    return await _sync(_bs_get().switch_tab, index, handle or None)


@mcp.tool()
async def browser_get_active_tab() -> str:
    """Return info about the currently active tab."""
    result = await _sync(_bs_get().get_active_tab)
    return json.dumps(result, ensure_ascii=False)


# =============================================================================
# FRAMES
# =============================================================================

@mcp.tool()
async def browser_list_frames() -> str:
    """List all iframes/frames on the current page."""
    result = await _sync(_bs_get().list_frames)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_switch_frame(selector: str) -> str:
    """Switch context into an iframe matched by CSS selector."""
    await _sync(_bs_get().switch_frame, selector)
    return f"OK: switched to frame {selector}"


@mcp.tool()
async def browser_switch_main_frame() -> str:
    """Switch back to the main (top-level) page context."""
    await _sync(_bs_get().switch_main_frame)
    return "OK: back to main frame"


@mcp.tool()
async def browser_frame_find(frame_selector: str, element_selector: str) -> str:
    """Find an element inside an iframe."""
    result = await _sync(_bs_get().frame_find, frame_selector, element_selector)
    return json.dumps(result, ensure_ascii=False) if result else "Element not found in frame"


@mcp.tool()
async def browser_frame_click(frame_selector: str, element_selector: str) -> str:
    """Click an element inside an iframe."""
    await _sync(_bs_get().frame_click, frame_selector, element_selector)
    return "OK: clicked element in frame"


@mcp.tool()
async def browser_frame_type(frame_selector: str, element_selector: str, text: str) -> str:
    """Type text into an input inside an iframe."""
    await _sync(_bs_get().frame_type, frame_selector, element_selector, text)
    return "OK: typed into element in frame"


# =============================================================================
# DIALOG
# =============================================================================

@mcp.tool()
async def browser_accept_dialog() -> str:
    """Accept an alert / confirm / prompt dialog."""
    try:
        return await _sync(_bs_get().accept_dialog)
    except Exception as e:
        raise RuntimeError(f"accept_dialog failed: {e}")


@mcp.tool()
async def browser_dismiss_dialog() -> str:
    """Dismiss an alert / confirm dialog."""
    try:
        return await _sync(_bs_get().dismiss_dialog)
    except Exception as e:
        raise RuntimeError(f"dismiss_dialog failed: {e}")


@mcp.tool()
async def browser_get_dialog() -> str:
    """Return the text of an active dialog, or null if none."""
    result = await _sync(_bs_get().get_dialog)
    return json.dumps(result, ensure_ascii=False)


# =============================================================================
# COOKIES
# =============================================================================

@mcp.tool()
async def browser_get_cookies() -> str:
    """Return all browser cookies as JSON."""
    result = await _sync(_bs_get().get_cookies)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_set_cookie(name: str, value: str, domain: str = "", path: str = "/") -> str:
    """Set a browser cookie."""
    await _sync(_bs_get().set_cookie, name, value, domain or None, path)
    return f"OK: cookie '{name}' set"


@mcp.tool()
async def browser_delete_cookie(name: str) -> str:
    """Delete a browser cookie by name."""
    await _sync(_bs_get().delete_cookie, name)
    return f"OK: cookie '{name}' deleted"


@mcp.tool()
async def browser_clear_cookies() -> str:
    """Clear all browser cookies."""
    await _sync(_bs_get().clear_cookies)
    return "OK: all cookies cleared"


@mcp.tool()
async def browser_export_cookies(path: str = "") -> str:
    """Save all cookies to a JSON file (requests-compatible). Proves logged-in state."""
    from .util import cookie_path
    bs = _bs_get()
    p = cookie_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = await _sync(bs.export_cookies, str(p))
    return json.dumps({"ok": True, "file": str(p), "cookie_count": n})


# =============================================================================
# STORAGE
# =============================================================================

@mcp.tool()
async def browser_get_local_storage() -> str:
    """Return all localStorage key-value pairs as JSON."""
    result = await _sync(_bs_get().get_local_storage)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_get_local_storage_item(key: str) -> str:
    """Return a single localStorage value."""
    result = await _sync(_bs_get().get_local_storage_item, key)
    return str(result) if result is not None else "null"


@mcp.tool()
async def browser_set_local_storage_item(key: str, value: str) -> str:
    """Set a localStorage item."""
    return await _sync(_bs_get().set_local_storage_item, key, value)


@mcp.tool()
async def browser_remove_local_storage_item(key: str) -> str:
    """Remove a localStorage item."""
    return await _sync(_bs_get().remove_local_storage_item, key)


@mcp.tool()
async def browser_get_session_storage() -> str:
    """Return all sessionStorage key-value pairs as JSON."""
    result = await _sync(_bs_get().get_session_storage)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_set_session_storage_item(key: str, value: str) -> str:
    """Set a sessionStorage item."""
    return await _sync(_bs_get().set_session_storage_item, key, value)


# =============================================================================
# NETWORK
# =============================================================================

@mcp.tool()
async def browser_start_network_recording(clear: bool = False) -> str:
    """(Re)arm network capture.

    Capture is ON from the moment the browser starts and a background thread
    drains Chrome's CDP log continuously, so this is only needed after an
    explicit browser_stop_network_recording(). Previously captured traffic is
    KEPT unless clear=True.
    """
    status = await _sync(_bs_get().start_network_recording, clear)
    return json.dumps(status, ensure_ascii=False, default=str)


@mcp.tool()
async def browser_stop_network_recording() -> str:
    """Pause storing network records (console/error capture keeps working)."""
    status = await _sync(_bs_get().stop_network_recording)
    return json.dumps(status, ensure_ascii=False, default=str)


@mcp.tool()
async def browser_set_persist_workspace(workspace: str = "") -> str:
    """Point durable network capture mirrors at <workspace>.

    JSONL mirror goes to ``<workspace>/.samet/network/`` and the sandbox
    traffic mirror to ``<workspace>/network_logs.json`` (or
    ``<workspace>/sandbox/network_logs.json`` when the workspace is the repo
    root).  Called automatically by the agent's browser proxy so every
    session's traffic lands in its own isolated sandbox.  Safe to call
    repeatedly — a no-op when the workspace is unchanged.
    """
    try:
        from pathlib import Path as _P
        p = _P(workspace).resolve() if workspace else None
        out = await _sync(_bs_get().set_persist_workspace, p)
        return json.dumps({"ok": True, "workspace": str(out)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


@mcp.tool()
async def browser_capture_status() -> str:
    """Diagnose the network capture pipeline: is it REALLY listening and recording?

    Returns whether the background CDP drain thread is alive, how long ago it
    last drained, how many requests/websocket frames are captured, the durable
    JSONL mirror path, the sandbox mirror path, and body-fetch success/failure
    counts. Call this first whenever network traffic seems to be missing.
    """
    status = await _sync(_bs_get().capture_status)
    return json.dumps(status, ensure_ascii=False, default=str)


@mcp.tool()
async def browser_load_persisted_capture(path: str = "") -> str:
    """Reload network capture from the durable JSONL mirror on disk.

    Every captured request is mirrored to `.samet/network/capture-*.jsonl` as it
    happens, so traffic survives an MCP restart, a browser crash or a FIFO
    eviction. Pass an explicit path, or leave empty for the newest file.
    """
    result = await _sync(_bs_get().load_persisted_capture, path or None)
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
async def browser_get_network_requests(pattern: str = "", limit: int = 40) -> str:
    """Return captured network requests as JSON. Filter by URL pattern. limit=0 → all."""
    rows = await _sync(_bs_get().get_network_requests, pattern, limit)
    return json.dumps(rows, ensure_ascii=False, default=str)


@mcp.tool()
async def browser_get_network_request(request_id: str) -> str:
    """Return a single captured network request by its ID."""
    result = await _sync(_bs_get().get_network_request, request_id)
    return json.dumps(result, ensure_ascii=False, default=str) if result else "Not found"


@mcp.tool()
async def browser_get_network_responses(limit: int = 40) -> str:
    """Return captured network responses (requests that have a status code) as JSON."""
    rows = await _sync(_bs_get().get_network_responses, limit)
    return json.dumps(rows, ensure_ascii=False, default=str)


@mcp.tool()
async def browser_wait_for_request(url_pattern: str, timeout: float = 10.0) -> str:
    """Wait for a network request matching the URL pattern. Returns the request record."""
    result = await _sync(_bs_get().wait_request, url_pattern, timeout)
    return json.dumps(result, ensure_ascii=False, default=str) if result else "timeout: no matching request"


@mcp.tool()
async def browser_wait_for_response(url_pattern: str, timeout: float = 10.0) -> str:
    """Wait for a network response matching the URL pattern. Returns the request record."""
    result = await _sync(_bs_get().wait_for_response, url_pattern, timeout)
    return json.dumps(result, ensure_ascii=False, default=str) if result else "timeout: no matching response"


@mcp.tool()
async def browser_block_url(pattern: str) -> str:
    """Block all network requests matching the URL pattern."""
    await _sync(_bs_get().block_url, pattern)
    return f"OK: blocked '{pattern}'"


@mcp.tool()
async def browser_unblock_url(pattern: str) -> str:
    """Remove a URL block."""
    await _sync(_bs_get().unblock_url, pattern)
    return f"OK: unblocked '{pattern}'"


def _format_network_request_detail(req: dict) -> str:
    url = req.get("url", "")
    method = req.get("method", "GET")
    status = req.get("response_status")
    mime = req.get("mime", "")
    ts = req.get("time_str") or ""
    headers = req.get("headers", {})
    post_data = req.get("post_data", "")
    query = req.get("query", "")
    resp_headers = req.get("response_headers", {})

    status_str = f"{status}" if status is not None else "pending/sent"
    if mime:
        status_str += f" ({mime})"
    if req.get("error"):
        status_str += f" [ERROR: {req['error']}]"

    lines = [
        f"=== REQUEST DETAILS [{req.get('id', '?')}] ===",
        f"URL: {url}",
        f"Method: {method}",
        f"Type: {req.get('type', 'request')}",
    ]
    if req.get("seq_index"):
        lines.append(f"Index: {req['seq_index']}")
    if ts:
        lines.append(f"Time: {ts}")
    lines.append(f"Status: {status_str}")
    if req.get("remote_address"):
        lines.append(f"Remote: {req['remote_address']}")
    if req.get("from_cache"):
        lines.append("Served from cache: yes")
    if req.get("initiator"):
        lines.append(f"Initiator: {req['initiator']}")
    if req.get("redirect_chain"):
        lines.append("\n--- REDIRECT CHAIN ---")
        for hop in req["redirect_chain"]:
            lines.append(
                f"{hop.get('status')} {hop.get('url')}"
                + (f"  ->  {hop['location']}" if hop.get("location") else "")
            )

    lines.append("\n--- HEADERS ---")
    if headers:
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
    else:
        lines.append("(empty)")

    body = req.get("response_body")
    if body:
        lines.append("\n--- RESPONSE BODY (auto-captured) ---")
        lines.append(str(body))
    else:
        why = req.get("response_body_error")
        lines.append(
            "\n--- RESPONSE BODY ---\n"
            + (f"(not captured — {why})\n" if why else "(not captured yet)\n")
            + "Bodies are auto-captured while the response loads. Call "
              "browser_network again if the response is still in flight, or "
              "browser_response_body for an on-demand refetch."
        )

    if query:
        lines.extend(["\n--- QUERY PARAMETERS ---", query])

    lines.append("\n--- PAYLOAD / POST DATA ---")
    if post_data:
        try:
            parsed = json.loads(post_data)
            lines.append(json.dumps(parsed, ensure_ascii=False, indent=2))
        except Exception:
            lines.append(str(post_data))
    elif req.get("post_data_truncated"):
        lines.append(
            "(request HAD a body but Chrome did not deliver it — it exceeded "
            "maxPostDataSize or was streamed)"
        )
    else:
        lines.append("(empty)")

    if resp_headers:
        lines.append("\n--- RESPONSE HEADERS ---")
        for k, v in resp_headers.items():
            lines.append(f"{k}: {v}")

    return "\n".join(lines)


@mcp.tool()
async def browser_network(
    pattern: str = "",
    limit: int = 40,
    request_id: str = "",
    include_headers: bool = False,
    dump_path: str = "",
) -> str:
    """List or inspect captured CDP network traffic.

    Capture filter: only HTTP requests (type="request"), JavaScript
    (type="js") and WebSockets (type="wss") are recorded. Images, media,
    fonts, CSS and documents are NOT captured at all — the buffer stays clean.

    Workflows:
      1. Overview List: Call `browser_network(limit=0)` to get ALL captured requests with NO limit.
         Use `pattern` to filter by URL fragment (e.g. pattern="twitch.tv").
         Format: `[seq_index | request_id] METHOD URL (time) -> status=STATUS_CODE (mime)`
      2. Full Detail: Call `browser_network(request_id="<id or index>")` to get 100% COMPLETE,
         untruncated headers, URL, parameters, POST payload, and response headers for one request.
      3. File Export: Call `browser_network(dump_path="network.json")` to export records to file.
         Existing files are NEVER overwritten: new records are APPENDED below
         the existing ones and duplicates are skipped.

    IMPORTANT: After completing any browser action (form submit, button click, navigation), ALWAYS
    call browser_network(limit=0) to capture ALL network requests with no truncation, then inspect
    each relevant request with browser_network(request_id="<index>") before writing any code.
    """
    bs = _bs_get()

    # 1. Single request full inspection mode (100% untruncated)
    if request_id:
        req = await _sync(bs.get_network_request, request_id)
        if not req:
            return f"No captured request found with id/index '{request_id}'."
        return _format_network_request_detail(req)

    # limit<=0 means ALL requests (no cap) — network_summary handles it natively.
    rows = await _sync(bs.network_summary, pattern, limit)
    if not rows:
        # Empty is ambiguous ("nothing happened" vs "capture is broken"), so
        # answer with the actual pipeline state instead of a guess.
        st = await _sync(bs.capture_status)
        return (
            f"No captured traffic matching '{pattern}'.\n"
            f"Capture status: recording={st.get('recording')} "
            f"drain_thread_alive={st.get('drain_thread_alive')} "
            f"driver_started={st.get('driver_started')} "
            f"total_captured={st.get('captured_requests')} "
            f"last_drain={st.get('seconds_since_last_drain')}s ago\n"
            f"Persisted mirror: {st.get('persist_file')}\n"
            + ("Capture IS live — interact with the flow first, then dump again."
               if st.get("drain_thread_alive") else
               "Capture is NOT live: open a page (browser_open) or call "
               "browser_start_network_recording, then retry.")
        )

    # 2. Save to file mode — APPEND/merge, never overwrite.
    # Existing records are preserved; new records are appended below them
    # ("altına ekstra"). Dedupe by request id so repeated dumps don't grow
    # the file with duplicates.
    if dump_path:
        p = Path(dump_path).resolve()
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
                # Unparsable/corrupt file: back it up, start fresh.
                import time as _time

                ts = _time.strftime("%Y%m%d-%H%M%S")
                bak = p.with_name(f"{p.stem}.corrupt-{ts}{p.suffix}")
                try:
                    bak.write_bytes(p.read_bytes())
                except Exception:
                    return (
                        f"ERROR: '{p}' exists but is not a valid JSON array and "
                        "could not be backed up. Use a different dump_path."
                    )
                existing = []

        seen_ids = {
            str(x.get("id")) for x in existing if isinstance(x, dict) and x.get("id")
        }
        new_items = []
        for i, r in enumerate(rows, 1):
            rid = str(r.get("id") or "")
            if rid and rid in seen_ids:
                continue  # already captured previously — skip duplicate
            item = dict(r)
            item["index"] = len(existing) + len(new_items) + 1
            if r.get("post_data"):
                try:
                    item["post_data_json"] = json.loads(r["post_data"])
                except Exception:
                    pass
            new_items.append(item)
            if rid:
                seen_ids.add(rid)

        p.write_text(
            json.dumps(existing + new_items, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return (
            f"OK: Appended {len(new_items)} new record(s) below {len(existing)} "
            f"existing one(s) to {p} (total {len(existing) + len(new_items)})."
        )

    # 3. Clean compact list view with seq_index, ID, Type, Method, URL, Time, Status
    lines = []
    for i, r in enumerate(rows, 1):
        idx = r.get("seq_index", i)
        mime_str = f" ({r.get('mime')})" if r.get("mime") else ""
        err_str = f" [ERROR: {r['error']}]" if r.get("error") else ""
        time_str = f" [{r['time_str']}]" if r.get("time_str") else ""
        type_str = f"<{r.get('type', 'request')}>" if r.get("type") else ""
        line = f"[{idx} | {r.get('id')}] {type_str} {r.get('method', '?')} {r.get('url', '')}{time_str} -> status={r.get('response_status')}{mime_str}{err_str}"
        if include_headers and r.get("headers"):
            line += "\n  headers: " + json.dumps(r["headers"], ensure_ascii=False)
            if r.get("post_data"):
                line += "\n  payload: " + str(r["post_data"])
        lines.append(line)

    lines.append(f"\nTotal: {len(rows)} requests shown. Use browser_network(request_id='<index>') for full untruncated headers & payload.")
    return "\n".join(lines)


@mcp.tool()
async def browser_response_body(request_id: str, save_path: str = "") -> str:
    """Fetch the actual response BODY of a captured request (id from browser_network).
    Response bodies are now AUTO-CAPTURED at load time and included in network dumps
    (field 'response_body') — this tool is only needed for on-demand refetching.
    Never guess response shapes — read them. Pass save_path to save untruncated body to a file."""
    bs = _bs_get()
    try:
        body = await _sync(bs.response_body, request_id)
    except Exception as e:
        msg = str(e).splitlines()[0][:300]  # selenium stacktraces are huge
        return (
            f"ERROR: body unavailable ({msg}). Note: bodies are auto-captured "
            "during loading — check 'response_body' in browser_network output/dump first. "
            "If truly missing, redo the flow and dump again."
        )
    if not isinstance(body, dict) or body.get("source") == "unavailable" or body.get("body") is None:
        detail = body.get("error", "") if isinstance(body, dict) else ""
        hint = body.get("hint", "") if isinstance(body, dict) else ""
        return (
            "ERROR: no body available for this request "
            f"({detail or 'evicted from Chrome buffer or opaque response'}). {hint}"
        ).strip()

    raw_data = str(body.get("body", ""))
    is_base64 = bool(body.get("base64Encoded"))
    source = body.get("source", "")

    if save_path:
        p = Path(save_path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        if is_base64:
            import base64
            p.write_bytes(base64.b64decode(raw_data))
        else:
            p.write_text(raw_data, encoding="utf-8")
        return f"OK: Response body ({len(raw_data)} chars) saved to {p}"

    prefix = "(base64) " if is_base64 else ""
    tail = "" if len(raw_data) <= 60_000 else (
        f"\n…[{len(raw_data) - 60_000} more chars — pass save_path to get the full body]"
    )
    return f"[source={source}] {prefix}{raw_data[:60_000]}{tail}"


# =============================================================================
# CONSOLE / DEBUG
# =============================================================================

@mcp.tool()
async def browser_get_console_logs() -> str:
    """Return all captured browser console logs (log/warn/error/info). Very useful for debugging agent actions."""
    result = await _sync(_bs_get().console, False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_clear_console() -> str:
    """Clear captured console logs."""
    await _sync(_bs_get().console, True)
    return "OK: console logs cleared"


@mcp.tool()
async def browser_get_page_errors() -> str:
    """Return all JavaScript / page errors captured since session start."""
    result = await _sync(_bs_get().errors, False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_get_performance_metrics() -> str:
    """Return Chrome performance metrics (TaskDuration, JSHeapUsedSize, etc.) as JSON."""
    try:
        result = await _sync(_bs_get().metrics)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        raise RuntimeError(f"performance_metrics failed: {e}")


# =============================================================================
# BROWSER MANAGEMENT
# =============================================================================

@mcp.tool()
async def browser_launch() -> str:
    """Ensure the browser is started (auto-started on first use, but this forces it)."""
    await _sync(_bs_get().launch_browser)
    return "OK: browser launched"


@mcp.tool()
async def browser_close(force: bool = False) -> str:
    """Close the browser and release all resources. Refused when used <30s after the last live-browser action unless force=true (protects authenticated sessions from the coding fallback)."""
    res = await _sync(_bs_get().close_browser, force)
    return str(res)


@mcp.tool()
async def browser_restart(force: bool = True) -> str:
    """Restart the browser (close and reopen). Auto-saves the session before restart."""
    res = await _sync(_bs_get().restart_browser, force)
    return str(res)


@mcp.tool()
async def browser_info() -> str:
    """Return browser info: name, version, current URL, open tabs count."""
    try:
        result = await _sync(_bs_get().browser_info)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        raise RuntimeError(f"browser_info failed: {e}")


@mcp.tool()
async def browser_set_user_agent(user_agent: str) -> str:
    """Override the browser User-Agent string."""
    return await _sync(_bs_get().set_user_agent, user_agent)


@mcp.tool()
async def browser_set_geolocation(latitude: float, longitude: float) -> str:
    """Override the browser geolocation."""
    return await _sync(_bs_get().set_geolocation, latitude, longitude)


@mcp.tool()
async def browser_set_timezone(timezone: str) -> str:
    """Override the browser timezone (e.g. 'America/New_York')."""
    return await _sync(_bs_get().set_timezone, timezone)


@mcp.tool()
async def browser_set_language(language: str) -> str:
    """Override the browser locale/language (e.g. 'tr-TR')."""
    return await _sync(_bs_get().set_language, language)


# =============================================================================
# DOWNLOAD
# =============================================================================

@mcp.tool()
async def browser_wait_for_download(timeout: float = 30.0, download_dir: str = "") -> str:
    """Wait for a file download to complete and return the file path."""
    try:
        path = await _sync(_bs_get().wait_for_download, timeout, download_dir or None)
        return f"OK: downloaded to {path}"
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_get_downloads() -> str:
    """List files downloaded during this session."""
    result = await _sync(_bs_get().get_downloads)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_download_file(url: str, path: str) -> str:
    """Download a file from the given URL and save to path (uses browser fetch)."""
    try:
        saved = await _sync(_bs_get().download_file, url, path)
        return f"OK: downloaded to {saved}"
    except Exception as e:
        raise RuntimeError(f"download_file failed: {e}")


# =============================================================================
# ACCESSIBILITY
# =============================================================================

@mcp.tool()
async def browser_get_accessibility_tree() -> str:
    """Return the ARIA accessibility tree of the current page as JSON.
    Agents can use this to find elements by role+name without CSS selectors."""
    try:
        result = await _sync(_bs_get().get_accessibility_tree)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        raise RuntimeError(f"accessibility_tree failed: {e}")


@mcp.tool()
async def browser_get_accessible_elements() -> str:
    """Return a flat list of ALL visible interactive elements with role, name, tag, type, disabled, checked.
    Use this for natural-language navigation: find 'Login' button by role=button, name='Login'."""
    try:
        rows = await _sync(_bs_get().get_accessible_elements)
        if not rows:
            return "No accessible elements found."
        lines = []
        for r in rows:
            dis = " [DISABLED]" if r.get("disabled") else ""
            chk = f" checked={r['checked']}" if r.get("checked") is not None else ""
            lines.append(
                f"[{r['index']}] role={r['role']} name='{r['name']}' tag={r['tag']}"
                + (f" type={r['type']}" if r.get("type") else "")
                + dis + chk
            )
        return "\n".join(lines)
    except Exception as e:
        raise RuntimeError(f"get_accessible_elements failed: {e}")


# =============================================================================
# AGENT HELPERS — High-level agent-oriented API
# =============================================================================

@mcp.tool()
async def browser_observe() -> str:
    """Return a high-level page summary for the agent: URL, title, text snippet, buttons, inputs, links, and errors.
    Use this as the FIRST step after every navigation or interaction to understand the current state."""
    try:
        result = await _sync(_bs_get().observe)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        raise RuntimeError(f"observe failed: {e}")


@mcp.tool()
async def browser_inspect(selector: str = "", index: int | None = None) -> str:
    """Inspect a single element in depth: all attributes, computed style, outer HTML."""
    try:
        result = await _sync(_bs_get().inspect_element, selector, index)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        raise RuntimeError(f"inspect failed: {e}")


@mcp.tool()
async def browser_find_interactive_elements() -> str:
    """Return all visible interactive elements (alias for browser_get_accessible_elements with indices)."""
    try:
        rows = await _sync(_bs_get().find_interactive_elements)
        return json.dumps(rows, ensure_ascii=False, indent=2)
    except Exception as e:
        raise RuntimeError(f"find_interactive_elements failed: {e}")


@mcp.tool()
async def browser_get_page_state() -> str:
    """Return a comprehensive page state: URL, title, all forms with their fields, JS errors, and console errors."""
    try:
        result = await _sync(_bs_get().get_page_state)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        raise RuntimeError(f"get_page_state failed: {e}")


@mcp.tool()
async def browser_extract(selector: str = "") -> str:
    """Extract structured data (tables, lists) from the page or a specific element."""
    try:
        result = await _sync(_bs_get().extract_structured, selector or None)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        raise RuntimeError(f"extract failed: {e}")


@mcp.tool()
async def browser_verify(condition: str) -> str:
    """Verify a natural-language condition against the current page.
    Examples: 'url contains dashboard', 'text "Welcome" present', 'login button visible'.
    Returns {ok: bool, detail: str}."""
    try:
        result = await _sync(_bs_get().verify_condition, condition)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        raise RuntimeError(f"verify failed: {e}")



# =============================================================================
# ELEMENT LIFECYCLE / STABILITY
# =============================================================================

@mcp.tool()
async def browser_get_element_id(selector: str) -> str:
    """Return a stable identity string for an element (tag+id+class+position).
    Useful for tracking elements across React re-renders."""
    return await _sync(_bs_get().get_element_id, selector)


@mcp.tool()
async def browser_is_attached(selector: str) -> str:
    """Check if an element is currently attached to the DOM. Returns 'true' or 'false'."""
    result = await _sync(_bs_get().is_attached, selector)
    return str(result).lower()


@mcp.tool()
async def browser_is_stable(selector: str, check_ms: int = 200) -> str:
    """Check if an element's bounding rect is stable (not animating/moving). Returns 'true' or 'false'."""
    result = await _sync(_bs_get().is_stable, selector, check_ms)
    return str(result).lower()


@mcp.tool()
async def browser_wait_for_stable(selector: str, timeout: float = 5.0) -> str:
    """Wait until the element stops moving or resizing (useful before clicking animated elements)."""
    try:
        return await _sync(_bs_get().wait_for_stable, selector, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_wait_for_element_change(selector: str, timeout: float = 10.0) -> str:
    """Wait until the element's DOM content changes (useful after React/Vue re-renders)."""
    try:
        return await _sync(_bs_get().wait_for_element_change, selector, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_wait_for_attribute(selector: str, attribute: str, value: str,
                                     timeout: float = 10.0) -> str:
    """Wait until element[attribute] equals the expected value.
    Example: wait_for_attribute('#btn', 'aria-disabled', 'false')."""
    try:
        return await _sync(_bs_get().wait_for_attribute, selector, attribute, value, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


@mcp.tool()
async def browser_wait_for_value(selector: str, value: str, timeout: float = 10.0) -> str:
    """Wait until an input's value property equals the expected value."""
    try:
        return await _sync(_bs_get().wait_for_value, selector, value, timeout)
    except TimeoutError as e:
        raise RuntimeError(str(e))


# =============================================================================
# ACTION VERIFICATION
# =============================================================================

@mcp.tool()
async def browser_verify_element_visible(selector: str) -> str:
    """Verify that the element is currently visible. Returns {ok, selector, detail}."""
    result = await _sync(_bs_get().verify_element_visible, selector)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_verify_text(text: str) -> str:
    """Verify that the given text appears anywhere on the page. Returns {ok, detail}."""
    result = await _sync(_bs_get().verify_text, text)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_verify_url(expected: str) -> str:
    """Verify that the current URL contains or matches the expected string. Returns {ok, current_url, detail}."""
    result = await _sync(_bs_get().verify_url, expected)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_verify_element_value(selector: str, value: str) -> str:
    """Verify that an input element's value equals the expected value. Returns {ok, current, expected, detail}."""
    result = await _sync(_bs_get().verify_element_value, selector, value)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def browser_verify_action(
    url_contains: str = "",
    text_present: str = "",
    selector_visible: str = "",
    selector: str = "",
    selector_value: str = "",
) -> str:
    """Run multiple verification checks after an action and return a combined result.
    Provide any combination of: url_contains, text_present, selector_visible, selector+selector_value.
    Returns {ok, checks: {url?, text?, element?, value?}}."""
    checks: dict = {}
    if url_contains:
        checks["url_contains"] = url_contains
    if text_present:
        checks["text_present"] = text_present
    if selector_visible:
        checks["selector_visible"] = selector_visible
    if selector and selector_value:
        checks["selector_value"] = {"selector": selector, "value": selector_value}
    result = await _sync(_bs_get().verify_action, checks)
    return json.dumps(result, ensure_ascii=False, indent=2)


# =============================================================================
# ACTION HISTORY
# =============================================================================

@mcp.tool()
async def browser_get_action_history() -> str:
    """Return all browser actions performed in this session as JSON.
    Each entry: {action, timestamp, params, url}. Great for agent self-inspection."""
    result = await _sync(_bs_get().get_action_history)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
async def browser_get_last_action() -> str:
    """Return the most recent browser action as JSON, or 'null' if nothing recorded."""
    result = await _sync(_bs_get().get_last_action)
    return json.dumps(result, ensure_ascii=False, default=str) if result else "null"


@mcp.tool()
async def browser_undo_last_action() -> str:
    """Attempt to undo the last action: goto→back, fill/type→clear, check→uncheck."""
    return await _sync(_bs_get().undo_last_action)


@mcp.tool()
async def browser_clear_action_history() -> str:
    """Clear the action history log."""
    await _sync(_bs_get().clear_action_history)
    return "OK: action history cleared"


# =============================================================================
# ADVANCED NETWORK
# =============================================================================

@mcp.tool()
async def browser_get_request_details(request_id: str) -> str:
    """Return the full captured request record (headers, post_data, resource_type, timestamps) as JSON."""
    result = await _sync(_bs_get().get_request_details, request_id)
    return json.dumps(result, ensure_ascii=False, default=str) if result else "Not found"


@mcp.tool()
async def browser_get_response_details(request_id: str) -> str:
    """Return the response-side fields of a captured request (status, headers, mime) as JSON."""
    result = await _sync(_bs_get().get_response_details, request_id)
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
async def browser_get_request_body(request_id: str) -> str:
    """Return the POST/PUT body of a captured request."""
    return await _sync(_bs_get().get_request_body, request_id)


@mcp.tool()
async def browser_get_response_body_adv(request_id: str) -> str:
    """Return the response body of a captured request via CDP (up to 8000 chars, base64-aware)."""
    return await _sync(_bs_get().get_response_body, request_id)


@mcp.tool()
async def browser_get_api_requests(limit: int = 40) -> str:
    """Return only XHR/fetch API requests from the captured traffic.
    Uses resource_type or content-type heuristics to filter out images/scripts/css."""
    rows = await _sync(_bs_get().get_api_requests, limit)
    return json.dumps(rows, ensure_ascii=False, default=str)


@mcp.tool()
async def browser_find_request(url_pattern: str = "", method: str = "") -> str:
    """Find the most recent captured request matching a URL pattern and/or HTTP method (GET, POST, etc.).
    Returns the full request record as JSON."""
    result = await _sync(_bs_get().find_request, url_pattern or None, method or None)
    return json.dumps(result, ensure_ascii=False, default=str) if result else "No matching request found"


@mcp.tool()
async def browser_wait_for_api(url_pattern: str, timeout: float = 15.0) -> str:
    """Wait for an API call matching url_pattern to complete (has a response status). Returns the request record."""
    result = await _sync(_bs_get().wait_for_api, url_pattern, timeout)
    return json.dumps(result, ensure_ascii=False, default=str) if result else "timeout: no matching API call"


@mcp.tool()
async def browser_replay_request(request_id: str) -> str:
    """Re-execute a previously captured request via browser fetch() with the same URL/headers/body.
    Returns the response body. Powerful for API exploration without opening a new session."""
    try:
        return await _sync(_bs_get().replay_request, request_id)
    except Exception as e:
        raise RuntimeError(f"replay_request failed: {e}")


# =============================================================================
# WEBSOCKET
# =============================================================================

@mcp.tool()
async def browser_start_websocket_recording() -> str:
    """Start capturing WebSocket frames from CDP performance logs.
    Must be called before the WebSocket connection is opened for best results."""
    await _sync(_bs_get().start_websocket_recording)
    return "OK: WebSocket recording started"


@mcp.tool()
async def browser_get_websocket_connections() -> str:
    """List all WebSocket connections observed since recording started.
    Each entry: {id, url, timestamp, closed}."""
    result = await _sync(_bs_get().get_websocket_connections)
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
async def browser_get_websocket_messages(connection_id: str = "", limit: int = 100) -> str:
    """Return captured WebSocket messages, optionally filtered by connection_id.
    Each entry: {connection_id, direction (sent/recv), payload, timestamp, opcode}."""
    result = await _sync(_bs_get().get_websocket_messages, connection_id or None, limit)
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
async def browser_wait_for_websocket_message(pattern: str, timeout: float = 15.0) -> str:
    """Wait for a WebSocket message whose payload contains the given pattern.
    Returns the message record as JSON."""
    result = await _sync(_bs_get().wait_for_websocket_message, pattern, timeout)
    return json.dumps(result, ensure_ascii=False, default=str) if result else "timeout: no matching WS message"


# =============================================================================
# PAGE INTELLIGENCE
# =============================================================================

@mcp.tool()
async def browser_analyze_page() -> str:
    """Full structural page analysis — the most important tool for a new page.

    Returns a comprehensive JSON report:
    {
      url, title,
      forms:   [{id, action, method, field_count}],
      inputs:  [{type, name, placeholder, required, disabled}],
      buttons: [{text, disabled, type}],
      links:   [{text, href}],
      modals:  [{id, aria_label, text}],
      popups:  [{text}],
      errors:  [str],
      iframes: int,
      captcha: bool
    }

    Use immediately after browser_goto / browser_open for a complete page overview
    before deciding how to interact."""
    try:
        result = await _sync(_bs_get().analyze_page)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        raise RuntimeError(f"analyze_page failed: {e}")


@mcp.tool()
async def browser_detect_forms() -> str:
    """Detect all forms on the page: id, action, method, and field count."""
    result = await _sync(_bs_get().detect_forms)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_detect_buttons() -> str:
    """Detect all visible buttons: text and disabled state."""
    result = await _sync(_bs_get().detect_buttons)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_detect_links() -> str:
    """Detect all visible links (up to 30): text and href."""
    result = await _sync(_bs_get().detect_links)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_detect_inputs() -> str:
    """Detect all visible input fields: type, name, placeholder, required, disabled."""
    result = await _sync(_bs_get().detect_inputs)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_detect_modals() -> str:
    """Detect any open modals or dialogs ([role=dialog], .modal, etc.) with their text."""
    result = await _sync(_bs_get().detect_modals)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_detect_popups() -> str:
    """Detect visible toasts, snackbars, alerts, and popups on the page."""
    result = await _sync(_bs_get().detect_popups)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_detect_errors() -> str:
    """Detect visible error messages (.error, .alert-danger, [aria-invalid], etc.).
    Returns a list of error text strings."""
    result = await _sync(_bs_get().detect_errors)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_detect_captcha() -> str:
    """Detect whether a CAPTCHA is present on the page (reCAPTCHA, hCaptcha, Cloudflare Turnstile).
    Returns 'true' or 'false'."""
    result = await _sync(_bs_get().detect_captcha)
    return str(result).lower()


# =============================================================================
# SESSION SNAPSHOT / RESTORE
# =============================================================================

@mcp.tool()
async def browser_export_session(path: str = "") -> str:
    """Capture current browser session state (URL, cookies, localStorage, sessionStorage).
    
    This snapshot can restore the session after browser restart or MCP crash. Essential for
    long-running automation tasks that might need to survive restarts.
    
    Args:
        path: Optional file path to save snapshot. If empty, returns snapshot as JSON string.
    
    Returns:
        If path provided: confirmation message with saved path
        If no path: JSON string of snapshot data
    """
    try:
        snapshot = await _sync(_bs_get().export_session, path or None)
        if path:
            return f"OK: Session snapshot saved to {path}"
        else:
            return json.dumps(snapshot, ensure_ascii=False, indent=2)
    except Exception as e:
        raise RuntimeError(f"export_session failed: {e}")


@mcp.tool()
async def browser_restore_session(snapshot_or_path: str) -> str:
    """Restore browser session from a previously captured snapshot.
    
    Use this after MCP server restart or browser crash to continue from where you left off.
    Restores: URL, cookies, localStorage, sessionStorage, viewport size.
    
    Args:
        snapshot_or_path: Either JSON string of snapshot data or path to snapshot file
    
    Returns:
        Status message describing what was restored
    """
    try:
        # Try to parse as JSON first (direct snapshot)
        try:
            snapshot = json.loads(snapshot_or_path)
            result = await _sync(_bs_get().restore_session, snapshot)
        except json.JSONDecodeError:
            # Treat as file path
            result = await _sync(_bs_get().restore_session, snapshot_or_path)
        return result
    except Exception as e:
        raise RuntimeError(f"restore_session failed: {e}")


@mcp.tool()
async def browser_auto_save_session() -> str:
    """Automatically save current session to default location (.samet/browser_sessions/last_session.json).
    
    This is useful before intentionally restarting the browser or ending a session.
    The auto-saved session can be restored with browser_auto_restore_session().
    """
    try:
        result = await _sync(_bs_get().auto_save_session)
        return result
    except Exception as e:
        raise RuntimeError(f"auto_save_session failed: {e}")


@mcp.tool()
async def browser_auto_restore_session() -> str:
    """Automatically restore session from default location if it exists.

    Use this immediately after browser restart to continue from previous state.
    If no auto-saved session exists, returns a message without error.
    """
    try:
        result = await _sync(_bs_get().auto_restore_session)
        return result
    except Exception as e:
        raise RuntimeError(f"auto_restore_session failed: {e}")


# =============================================================================
# JS TRACE / CALL STACK — "like'e tikla, arkada hangi dosyada hangi
# fonksiyon sirasiyla calisti?" sorusunu yanitlar.  No Debugger domain
# required: window.__sametTrace wrapper'lari Error().stack ile dosya:satir
# bilgisini toplar ve browser_network ile korele edilir.
# =============================================================================

@mcp.tool()
async def browser_inject_trace() -> str:
    """Inject (or re-inject) the JS trace instrumentation into the current page.
    Idempotent; call once after navigation. trace_click will auto-inject if needed."""
    result = await _sync(_bs_get().inject_trace)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_start_js_trace(clear: bool = True) -> str:
    """Arm the JS trace collector. clear=False keeps previous calls."""
    result = await _sync(_bs_get().start_js_trace, clear)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_stop_js_trace() -> str:
    """Disarm the JS trace collector (keeps captured calls)."""
    result = await _sync(_bs_get().stop_js_trace)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_clear_js_trace() -> str:
    """Clear all captured JS trace calls and listener registry."""
    result = await _sync(_bs_get().clear_js_trace)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_get_js_trace() -> str:
    """Return captured JS trace: {enabled, calls:[{kind,target,stack,files,url,...}], listeners:[...]}.
    Each call carries an Error().stack and parsed file:line hints (files:[])."""
    result = await _sync(_bs_get().get_js_trace)
    # Pretty narrative + full JSON so both LLM and user can read it
    calls = result.get("calls") or []
    lines = [f"JS trace: enabled={result.get('enabled')}  calls={len(calls)}  listeners={len(result.get('listeners') or [])}"]
    for i, c in enumerate(calls[-80:], 1):
        kind = c.get("kind", "?")
        tgt = c.get("target") or c.get("event") or ""
        url = c.get("url") or ""
        files = c.get("files") or []
        fh = (" <- " + " | ".join(files[:2])) if files else ""
        if kind == "listener":
            lines.append(f"{i}. [listener:{tgt}]{fh}")
        elif kind in ("fetch", "xhr"):
            lines.append(f"{i}. [{kind} {c.get('method','?')} {str(url)[:100]}]{fh}")
        elif kind in ("fetch:response", "xhr:response"):
            lines.append(f"{i}.   -> status={c.get('status')}{fh}")
        elif kind == "timeout":
            lines.append(f"{i}. [setTimeout delay={c.get('delay')}]{fh}")
        elif kind == "console:error":
            lines.append(f"{i}. [console.error] {str(c.get('text',''))[:100]}{fh}")
        else:
            lines.append(f"{i}. [{kind} {tgt}]{fh}")
    narrative = "\n".join(lines)
    return narrative + "\n\n--- full JSON ---\n" + json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_get_event_listeners(selector: str = "", index: int | None = None) -> str:
    """Which JS listeners are bound to this element? selector or index (from browser_elements).
    Combines CDP DOMDebugger + in-page registry + live probe. Returns {selector, cdp, js_registry, probe}."""
    result = await _sync(_bs_get().get_event_listeners, selector, index)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_trace_click(selector: str = "", index: int | None = None, wait_sec: float = 2.5) -> str:
    """One-shot: arm trace, CLICK the element, wait wait_sec, then explain the JS chain.

    The answer to 'like'e tikla ve arkada hangi js'de hangi fonksiyon sirayla calisti?':
    - JS listener invocations with file:line (from Error().stack)
    - fetch/XHR triggered by the click with URLs + status
    - network diff (new requests since click)
    - bound listeners on the element
    - replay_js: console snippet to replay the same request(s) without clicking

    Preferred: pass index from browser_elements. Returns {ok, selector, explain, calls, network, replay_js, hint}."""
    result = await _sync(_bs_get().trace_click, selector, index, wait_sec)
    if isinstance(result, dict) and result.get("explain"):
        # Return the human narrative first (LLM reads top), full JSON after
        head = result.pop("explain")
        return head + "\n\n--- full JSON ---\n" + json.dumps(result, ensure_ascii=False, indent=2, default=str)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
async def browser_get_replay_snippet(selector: str = "", index: int | None = None) -> str:
    """Return a console JS snippet that replays the last traced like-click WITHOUT clicking.

    Builds fetch() calls from the captured network (method/url/headers/body) + a DOM
    dispatch alternative. Paste into your own browser's F12 Console on the same site
    (with your own session cookies) to trigger the same action programmatically.
    Call trace_click first for the precise network slice."""
    result = await _sync(_bs_get().get_replay_snippet, selector, index)
    if isinstance(result, dict) and result.get("snippet"):
        head = result.get("snippet", "")
        meta = {k: v for k, v in result.items() if k != "snippet"}
        return head + "\n\n--- meta ---\n" + json.dumps(meta, ensure_ascii=False, indent=2, default=str)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


# =============================================================================
# GHOSTWIRE — stealth runtime instrumentation (Debugger/HeapProfiler)
# Requires Chrome with ensure_ghostwire(); all tools degrade gracefully when
# WS bridge is unavailable (hint in error).  Do NOT spawn a second Chrome.
# =============================================================================

def _gw_clip(obj: Any, n: int = 600) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


@mcp.tool()
async def gw_attach(url: str, headless: bool = True, proxy: str = "", blackbox: str = "") -> str:
    """Launch/reuse Chrome and enable Ghostwire (Debugger + HeapProfiler).

    Does NOT spawn a second Chrome — reuses SeleniumBase UC Chrome.
    Navigates to `url` and ensures Ghostwire is ready. `blackbox` is a
    comma-separated list of regex patterns for Debugger.setBlackboxPatterns.
    headless/proxy are accepted for API compat and currently ignored.
    """
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_attach, url, blackbox)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_targets() -> str:
    """List attached CDP targets (page/worker/iframe) — gw_attach first."""
    bs = _bs_get()
    try:
        rows = await _sync(bs.gw_targets)
        return json.dumps(rows, ensure_ascii=False, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_eval(expression: str, target_url: str = "") -> str:
    """Evaluate JS via Ghostwire (/*gw*/-prefixed, silent). Cross-target via target_url substring."""
    bs = _bs_get()
    try:
        val = await _sync(bs.gw_eval, expression, target_url)
        return json.dumps(val, ensure_ascii=False, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_hook(expression: str, target_url: str = "", capture_returns: bool = False, label: str = "") -> str:
    """Invisible hook via Debugger.setBreakpointOnFunctionCall (no wrapping).

    `expression` must evaluate to a function (e.g. "window.fetch", "app.decrypt").
    With `capture_returns=True`, return values are paired into a corpus under `label`.
    """
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_hook, expression, target_url, capture_returns, label)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_captures(limit: int = 50) -> str:
    """Recent arg-only hook captures [{session, fn, args, stack}]."""
    bs = _bs_get()
    try:
        rows = await _sync(bs.gw_captures, limit)
        return json.dumps(rows, ensure_ascii=False, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_corpus(label: str = "", limit: int = 20, full: bool = False) -> str:
    """Corpus view: no label → {label: count}; with label → {label, total, shown, pairs}."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_corpus, label, limit, full)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_verify(real_fn_expr: str, candidate: str, label: str = "", fresh_inputs: str = "", target_url: str = "", sample: int = 200, max_mismatches: int = 5) -> str:
    """Verify a candidate re-implementation against corpus + live ground truth.

    `fresh_inputs` is a JSON string: '[["a",1],["b",2]]' (array of arglists).
    Runs candidate in an isolated page, compares with _equal, returns
    {verified, tested, matched, mismatches, coverage_notes}.
    """
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_verify, real_fn_expr, candidate, label, fresh_inputs, target_url, sample, max_mismatches)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_objects(value: str = "", constructor: str = "", key: str = "", target_url: str = "", limit: int = 20) -> str:
    """Heap search via fresh snapshot: find objects by value / constructor / key. Returns retaining paths."""
    bs = _bs_get()
    try:
        rows = await _sync(bs.gw_objects, value, constructor, key, target_url, limit)
        return json.dumps(rows, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_strings(decoder: str, start: int = 0, count: int = 256, extra_args: str = "", target_url: str = "") -> str:
    """Drive the page's real string decoder over [start, start+count) (extra_args JSON array). Stops after 48 misses."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_strings, decoder, start, count, extra_args, target_url)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_vm(url_substr: str, line: int, watch: str = "", column: int = 0, iterations: int = 300, trigger: str = "", target_url: str = "") -> str:
    """Break at urlRegex:line (1-based) and log watch expressions per hit (VM dispatch tracing)."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_vm, url_substr, line, watch, column, iterations, trigger, target_url)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_crypto(seconds: float = 4.0, target_url: str = "") -> str:
    """Breakpoint crypto.subtle.* for `seconds` and decode caller scope buffers (hex+utf8). Returns [{method, caller, inputs}]."""
    bs = _bs_get()
    try:
        rows = await _sync(bs.gw_crypto, seconds, target_url)
        return json.dumps(rows, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_heapdiff(trigger: str, target_url: str = "", wait_ms: int = 300) -> str:
    """Snapshot → eval trigger (awaitPromise) → snapshot → diff {new_strings, new_objects_by_constructor, new_buffers, totals}."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_heapdiff, trigger, target_url, wait_ms)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_patch(value: str = "", constructor: str = "", key: str = "", node_id: int = 0, set_props: str = "", apply: str = "", target_url: str = "") -> str:
    """Patch a live heap object (found by value/ctor/key or node_id): set_props JSON object via Object.assign, or apply JS fn with this bound."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_patch, value, constructor, key, node_id, set_props, apply, target_url)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_origin(value: str, enter: str, trigger: str = "", target_url: str = "", blackbox: str = "", max_steps: int = 300) -> str:
    """Find which function return first introduces `value` in the heap (stepInto with blackbox). `enter` is a function expr."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_origin, value, enter, trigger, target_url, blackbox, max_steps)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_follow(producer: str, trigger: str = "", target_url: str = "", blackbox: str = "") -> str:
    """Follow producer's return value into the consumer frame (stepOut + scope scan). Reports {producer, returned, async, consumer}."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_follow, producer, trigger, target_url, blackbox)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_scripts(search: str = "", dynamic_only: bool = False, full: bool = False) -> str:
    """Scripts seen via Debugger.scriptParsed (eval/new Function included). Filter by search or dynamic_only; full includes source."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_scripts, search, dynamic_only, full)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_network(url_substr: str = "", full_bodies: bool = False) -> str:
    """Cross-target network view (delegates to the durable capture). Filter by url_substr; full_bodies includes response_body."""
    bs = _bs_get()
    try:
        rows = await _sync(bs.gw_network, url_substr, full_bodies)
        return json.dumps(rows, ensure_ascii=False, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_navigate(url: str) -> str:
    """Navigate the page (gw-flavoured alias for browser_goto)."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_navigate, url)
        return json.dumps(res, ensure_ascii=False, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_save(path: str) -> str:
    """Save {targets, scripts, captures, corpora, network} to path as JSON."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_save, path)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_close() -> str:
    """Clear Ghostwire tracer/oracle state (keeps the browser alive)."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_close)
        return json.dumps(res, ensure_ascii=False, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


@mcp.tool()
async def gw_read(value: str = "", constructor: str = "", key: str = "", node_id: int = 0, target_url: str = "") -> str:
    """Read scalar properties of a live heap object found by value/ctor/key or node_id."""
    bs = _bs_get()
    try:
        res = await _sync(bs.gw_read, value, constructor, key, node_id, target_url)
        return json.dumps(res, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        raise RuntimeError(_err_msg(e))


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

