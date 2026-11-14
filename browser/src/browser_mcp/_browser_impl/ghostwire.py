"""Ghostwire adapter for SeleniumBase UC Chrome — MIT attribution.

Ported from https://github.com/sofianeelhor/ghostwire (MIT).

Original ghostwire spawns Chrome with --remote-debugging-pipe (fd 3/4) and
talks CDP over a NUL-delimited pipe.  samet-browser already owns Chrome via
SeleniumBase (chromedriver + WebDriver).  This adapter reuses that Chrome:

  * Sends  → driver.execute_cdp_cmd (synchronous, any thread)
  * Events → CdpWsBridge (parallel WebSocket to webSocketDebuggerUrl) when
            available, otherwise degraded (polling / hint).
  * Targets → single main target; Target.createTarget for oracle isolation.

Only Debugger / HeapProfiler domains are added here; Network drain stays in
BrowserSession.  No new hard dependency.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

# -- sentinel ---------------------------------------------------------------
GW = "/*gw*/"
ARGS = GW + "JSON.stringify(Array.prototype.slice.call(arguments))"

DEFAULT_BLACKBOX = [
    r"/node_modules/",
    r"react-dom",
    r"react\.production",
    r"react\.development",
    r"vue\.",
    r"angular",
    r"zone\.js",
    r"polyfill",
    r"webpack",
    r"jquery",
    r"lodash",
    r"\.min\.js",
]

SUBTLE_METHODS = (
    "digest",
    "encrypt",
    "decrypt",
    "sign",
    "verify",
    "deriveBits",
    "deriveKey",
    "importKey",
    "exportKey",
    "wrapKey",
    "unwrapKey",
    "generateKey",
)

_DECODE = (
    "function(){"
    "var u;"
    "if(this instanceof ArrayBuffer)u=new Uint8Array(this);"
    "else if(ArrayBuffer.isView(this))u=new Uint8Array(this.buffer,this.byteOffset,this.byteLength);"
    "else return null;"
    "var s=u.subarray(0,64);"
    "var hex=Array.prototype.map.call(s,function(x){return ('0'+x.toString(16)).slice(-2)}).join('');"
    "var utf8='';try{utf8=new TextDecoder().decode(s)}catch(e){}"
    "return {bytes:u.length,hex:hex,utf8:utf8};}"
)

VALUE_TYPES = ("string", "concatenated string", "sliced string", "number")
STRONG_EDGES = ("property", "internal", "element", "context")
NAMED_EDGES = ("property", "internal", "context", "shortcut", "hidden")
TYPED_ARRAYS = (
    "ArrayBuffer",
    "SharedArrayBuffer",
    "DataView",
    "Int8Array",
    "Uint8Array",
    "Uint8ClampedArray",
    "Int16Array",
    "Uint16Array",
    "Int32Array",
    "Uint32Array",
    "Float32Array",
    "Float64Array",
    "BigInt64Array",
    "BigUint64Array",
)


# ---------------------------------------------------------------------------
# Engine adapter
# ---------------------------------------------------------------------------
class GhostwireEngine:
    """Single-Chrome adapter: send via execute_cdp_cmd, events via WS bridge."""

    def __init__(self, driver, ws_bridge=None):
        self.driver = driver
        self.ws = ws_bridge
        # handlers dict is authoritative; WS forwards into it
        self.handlers: dict[str, list] = {}
        self._ws_forwarders: dict[str, Any] = {}
        self.lock = threading.RLock()
        self.page_session = None
        self.sessions: dict[str, dict] = {}
        self.private: set[str] = set()
        self._attach_ws_forwarders()

    # so engine.cdp.handlers works (origin/dataflow monkey-patch)
    @property
    def cdp(self):
        return self

    def _attach_ws_forwarders(self):
        if self.ws is None or not getattr(self.ws, "alive", False):
            return
        for method in (
            "Debugger.paused",
            "Debugger.scriptParsed",
            "HeapProfiler.addHeapSnapshotChunk",
            "Target.attachedToTarget",
            "Target.detachedFromTarget",
            "Target.targetInfoChanged",
        ):

            def _make(m):
                def fwd(params, session_id=None):
                    lst = list(self.handlers.get(m, []))
                    for h in lst:
                        try:
                            h(params, session_id)
                        except TypeError:
                            try:
                                h(params)
                            except Exception:
                                pass
                        except Exception as e:
                            log.debug("handler %s failed: %s", m, e)

                return fwd

            fwd = _make(method)
            self._ws_forwarders[method] = fwd
            try:
                self.ws.on(method, fwd)
            except Exception:
                pass

    # -- CDP ops ------------------------------------------------------------
    def send(self, method: str, params=None, session_id=None):
        # session_id is ignored for single-target; CDP still accepts it as param
        # for Target domain we pass through
        p = dict(params or {})
        # Selenium's execute_cdp_cmd signature is (cmd, params)
        return self.driver.execute_cdp_cmd(method, p)

    def on(self, method: str, handler):
        self.handlers.setdefault(method, []).append(handler)

    def off(self, method: str, handler):
        lst = self.handlers.get(method) or []
        try:
            lst.remove(handler)
        except ValueError:
            pass

    def resolve_session(self, target_url=None):
        # single target — ignore scoping, return None (main)
        return None

    def targets(self):
        try:
            url = self.driver.current_url or ""
        except Exception:
            url = ""
        return [(None, "page", url)]

    def open_isolated_page(self):
        # Try CDP Target.createTarget (works even without WS)
        try:
            res = self.send("Target.createTarget", {"url": "about:blank"})
            tid = res.get("targetId")
            if tid:
                self.private.add(tid)
                try:
                    att = self.send("Target.attachToTarget", {"targetId": tid, "flatten": True})
                    sid = att.get("sessionId")
                    if sid:
                        return sid
                except Exception:
                    pass
                return tid
        except Exception as e:
            log.debug("open_isolated_page CDP failed: %s", e)
        # fallback: window.open
        try:
            self.driver.execute_script("window.open('about:blank','_blank');")
            return "window_open"
        except Exception:
            return None

    def close_target(self, sid):
        if not sid or sid == "window_open":
            return
        # sid may be targetId or sessionId — try both
        for meth in ("Target.closeTarget",):
            try:
                self.send(meth, {"targetId": sid})
                return
            except Exception:
                pass

    def close(self):
        # WS closed by BrowserSession; nothing else
        pass


# ---------------------------------------------------------------------------
# Tracer — invisible breakpoint hooks
# ---------------------------------------------------------------------------
class Tracer:
    def __init__(self, engine: GhostwireEngine):
        self.captures: list[dict] = []
        self.pairs: dict[str, list[dict]] = {}
        self.lock = threading.Lock()
        self.verbose = False
        self.engine = engine
        self.hooks: list[dict] = []
        self.ret_bps: dict[str, str] = {}
        self.by_location: dict[tuple, dict] = {}

    def attach(self, engine: GhostwireEngine):
        self.engine = engine
        engine.on("Debugger.paused", self._paused)
        return self

    def hook(self, expression, target_url=None, capture_returns=False, label=None):
        sid = self.engine.resolve_session(target_url)
        obj = self.engine.send(
            "Runtime.evaluate", {"expression": GW + expression, "silent": True}, session_id=sid
        ).get("result", {})
        if obj.get("type") != "function" or not obj.get("objectId"):
            raise RuntimeError(f"{expression!r} is not a function in {target_url or 'page'}: {obj}")
        self.engine.send("Debugger.setBreakpointOnFunctionCall", {"objectId": obj["objectId"]}, session_id=sid)
        hook = {"label": label or expression, "session": sid, "returns": capture_returns, "ret_set": False}
        if capture_returns:
            loc = self._location(obj["objectId"], sid)
            if loc:
                self.by_location[(sid, loc.get("scriptId"), loc.get("lineNumber"), loc.get("columnNumber"))] = hook
            with self.lock:
                self.pairs.setdefault(hook["label"], [])
        self.hooks.append(hook)
        return self

    def _location(self, object_id, sid):
        try:
            for ip in self.engine.send(
                "Runtime.getProperties", {"objectId": object_id, "ownProperties": False}, session_id=sid
            ).get("internalProperties", []):
                if ip.get("name") == "[[FunctionLocation]]":
                    return (ip.get("value") or {}).get("value")
        except Exception:
            return None

    def _paused(self, params, session_id=None):
        frames = params.get("callFrames", [])
        hit = set(params.get("hitBreakpoints") or [])
        labels = [self.ret_bps[b] for b in hit if b in self.ret_bps]
        if labels and frames:
            self._pair(labels[0], frames[0], session_id)
            return self._resume(session_id)
        if frames:
            hook = self._match(frames[0], session_id)
            if hook and hook["returns"]:
                if not hook["ret_set"]:
                    self._set_return_bps(hook, frames[0], session_id)
                return self._resume(session_id)
            self._args(frames, session_id)
        self._resume(session_id)

    def _match(self, frame, sid):
        loc = frame.get("functionLocation") or {}
        hook = self.by_location.get((sid, loc.get("scriptId"), loc.get("lineNumber"), loc.get("columnNumber")))
        if hook:
            return hook
        pending = [h for h in self.hooks if h["returns"] and not h["ret_set"] and h["session"] == sid]
        return pending[0] if len(pending) == 1 else None

    def _set_return_bps(self, hook, frame, sid):
        loc = frame.get("functionLocation")
        if not loc:
            hook["ret_set"] = True
            return
        try:
            locations = self.engine.send(
                "Debugger.getPossibleBreakpoints", {"start": loc, "restrictToFunction": True}, session_id=sid
            ).get("locations", [])
        except Exception:
            locations = []
        count = 0
        for rl in (l for l in locations if l.get("type") == "return"):
            try:
                bp = self.engine.send("Debugger.setBreakpoint", {"location": rl}, session_id=sid).get("breakpointId")
            except Exception:
                bp = None
            if bp:
                self.ret_bps[bp] = hook["label"]
                count += 1
        hook["ret_set"] = True
        if not count:
            log.debug("[gw] no return location for %r", hook["label"])

    def _pair(self, label, frame, sid):
        try:
            args = self.engine.send(
                "Debugger.evaluateOnCallFrame",
                {"callFrameId": frame["callFrameId"], "expression": ARGS, "returnByValue": True},
                session_id=sid,
            ).get("result", {}).get("value")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
        except Exception as e:
            args = f"<args error: {e}>"
        rv = frame.get("returnValue") or {}
        rec: dict[str, Any] = {"input": args, "output": rv.get("value"), "output_type": rv.get("type")}
        if "value" not in rv and rv.get("type"):
            rec["output"], rec["unserialized"] = None, rv.get("description") or rv.get("subtype") or rv.get("type")
        with self.lock:
            self.pairs.setdefault(label, []).append(rec)
        if self.verbose:
            log.debug("[pair] %s(%r) -> %r", label, rec["input"], rec["output"])

    def _args(self, frames, sid):
        frame = frames[0]
        rec: dict[str, Any] = {"session": sid, "fn": frame.get("functionName") or "<anonymous>"}
        try:
            rec["args"] = self.engine.send(
                "Debugger.evaluateOnCallFrame",
                {"callFrameId": frame["callFrameId"], "expression": ARGS, "returnByValue": True},
                session_id=sid,
            ).get("result", {}).get("value")
        except Exception as e:
            rec["args"] = f"<eval error: {e}>"
        rec["stack"] = [f.get("functionName") or "<anon>" for f in frames[:5]]
        with self.lock:
            self.captures.append(rec)
        if self.verbose:
            log.debug("[hook] %s(%r)", rec["fn"], rec["args"])

    def _resume(self, sid):
        try:
            self.engine.send("Debugger.resume", session_id=sid)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Heap snapshot
# ---------------------------------------------------------------------------
def take_snapshot(engine: GhostwireEngine, session_id=None, numeric=True, timeout=60):
    sid = session_id if session_id is not None else engine.page_session
    chunks: list[str] = []

    def collect(params, s=None):
        chunks.append(params.get("chunk") or "")

    engine.on("HeapProfiler.addHeapSnapshotChunk", collect)
    try:
        engine.send("HeapProfiler.enable", session_id=sid)
        engine.send(
            "HeapProfiler.takeHeapSnapshot",
            {"reportProgress": False, "captureNumericValue": numeric},
            session_id=sid,
        )
        deadline = time.time() + timeout
        while True:
            try:
                data = json.loads("".join(chunks))
                break
            except ValueError:
                if time.time() > deadline:
                    raise RuntimeError("heap snapshot did not assemble within timeout")
                time.sleep(0.05)
    finally:
        engine.off("HeapProfiler.addHeapSnapshotChunk", collect)
    return Snapshot(data)


def _has_user_retainer(snapshot: Snapshot, node: int) -> bool:
    for frm, edge_type, _ in snapshot.retainers(node):
        if edge_type in ("property", "element") and snapshot.type(frm) in ("object", "closure"):
            return True
    return False


def diff_snapshots(before: Snapshot, after: Snapshot, max_items=30):
    old_ids = {before.node_id(n) for n in range(before.node_count)}
    strings: list[str] = []
    constructors: dict[str, int] = {}
    buffers: list[dict] = []
    for node in range(after.node_count):
        if after.node_id(node) in old_ids:
            continue
        kind = after.type(node)
        if kind in ("string", "concatenated string", "sliced string"):
            value = after.resolve_string(node)
            if value and len(value) > 3 and _has_user_retainer(after, node):
                strings.append(value)
        elif kind == "object":
            name = after.name(node)
            constructors[name] = constructors.get(name, 0) + 1
            if name in TYPED_ARRAYS:
                buffers.append({"type": name, "size": after.self_size(node)})
    seen: set[str] = set()
    unique: list[str] = []
    for value in sorted(strings, key=len, reverse=True):
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return {
        "new_strings": unique[:max_items],
        "new_objects_by_constructor": dict(sorted(constructors.items(), key=lambda kv: -kv[1])[:max_items]),
        "new_buffers": sorted(buffers, key=lambda b: -b["size"])[:max_items],
        "totals": {"distinct_strings": len(unique), "objects": sum(constructors.values()), "buffers": len(buffers)},
    }


class Snapshot:
    def __init__(self, data):
        meta = data["snapshot"]["meta"]
        self.nodes, self.edges, self.strings = data["nodes"], data["edges"], data["strings"]
        self.node_count = data["snapshot"]["node_count"]
        node_fields, edge_fields = meta["node_fields"], meta["edge_fields"]
        self.node_width, self.edge_width = len(node_fields), len(edge_fields)
        self.f_type, self.f_name, self.f_id, self.f_size, self.f_edges = (
            node_fields.index("type"),
            node_fields.index("name"),
            node_fields.index("id"),
            node_fields.index("self_size"),
            node_fields.index("edge_count"),
        )
        self.e_type, self.e_name, self.e_to = (
            edge_fields.index("type"),
            edge_fields.index("name_or_index"),
            edge_fields.index("to_node"),
        )
        self.node_type_names = meta["node_types"][0]
        self.edge_type_names = meta["edge_types"][0]
        self._edge_offset = None
        self._retainers = None

    def name(self, node):
        return self.strings[self.nodes[node * self.node_width + self.f_name]]

    def type(self, node):
        return self.node_type_names[self.nodes[node * self.node_width + self.f_type]]

    def node_id(self, node):
        return self.nodes[node * self.node_width + self.f_id]

    def self_size(self, node):
        return self.nodes[node * self.node_width + self.f_size]

    def edge_count(self, node):
        return self.nodes[node * self.node_width + self.f_edges]

    def _index_edges(self):
        if self._edge_offset is not None:
            return
        offset = [0] * self.node_count
        running = 0
        for node in range(self.node_count):
            offset[node] = running
            running += self.nodes[node * self.node_width + self.f_edges]
        self._edge_offset = offset

    def edges_of(self, node):
        self._index_edges()
        result = []
        first = self._edge_offset[node]
        for k in range(self.edge_count(node)):
            base = (first + k) * self.edge_width
            result.append(
                (
                    self.edge_type_names[self.edges[base + self.e_type]],
                    self.edges[base + self.e_name],
                    self.edges[base + self.e_to] // self.node_width,
                )
            )
        return result

    def _index_retainers(self):
        if self._retainers is not None:
            return
        self._index_edges()
        retainers: dict[int, list] = {}
        edges, width = self.edges, self.edge_width
        e_type, e_name, e_to, node_width = self.e_type, self.e_name, self.e_to, self.node_width
        for node in range(self.node_count):
            first = self._edge_offset[node]
            for k in range(self.edge_count(node)):
                base = (first + k) * width
                target = edges[base + e_to] // node_width
                retainers.setdefault(target, []).append(
                    (node, self.edge_type_names[edges[base + e_type]], edges[base + e_name])
                )
        self._retainers = retainers

    def _edge_label(self, edge_type, name_or_index):
        return self.strings[name_or_index] if edge_type in NAMED_EDGES else name_or_index

    def retainers(self, node):
        self._index_retainers()
        return [(frm, edge_type, self._edge_label(edge_type, raw)) for frm, edge_type, raw in self._retainers.get(node, [])]

    def resolve_string(self, node, depth=0):
        kind = self.type(node)
        if kind == "concatenated string" and depth < 64:
            first = second = ""
            for edge_type, raw, to in self.edges_of(node):
                if edge_type == "internal":
                    label = self._edge_label(edge_type, raw)
                    if label == "first":
                        first = self.resolve_string(to, depth + 1)
                    elif label == "second":
                        second = self.resolve_string(to, depth + 1)
            return first + second
        if kind == "sliced string" and depth < 64:
            for edge_type, raw, to in self.edges_of(node):
                if edge_type == "internal" and self._edge_label(edge_type, raw) == "parent":
                    return self.resolve_string(to, depth + 1)
        return self.name(node)

    def find_value(self, value):
        wanted = str(value)
        hits = []
        for node in range(self.node_count):
            kind = self.type(node)
            if kind in ("string", "number"):
                if self.name(node) == wanted:
                    hits.append(node)
            elif kind in ("concatenated string", "sliced string"):
                if self.resolve_string(node) == wanted:
                    hits.append(node)
        return hits

    def retaining_path(self, node, max_depth=8):
        path, seen, current = [], set(), node
        for _ in range(max_depth):
            if current in seen:
                break
            seen.add(current)
            strong = [r for r in self.retainers(current) if r[1] in STRONG_EDGES]
            if not strong:
                break
            frm, edge_type, label = strong[0]
            path.append({"holder": self.name(frm), "type": self.type(frm), "via": label})
            current = frm
        return path

    def find_objects(self, value=None, constructor=None, key=None, limit=50):
        results, seen_ids = [], set()

        def emit(obj_node, prop, val):
            oid = self.node_id(obj_node)
            if oid in seen_ids:
                return
            seen_ids.add(oid)
            results.append(
                {"id": oid, "constructor": self.name(obj_node), "type": self.type(obj_node), "property": prop, "value": val, "path": self.retaining_path(obj_node)}
            )

        if value is not None:
            for string_node in self.find_value(value):
                for frm, edge_type, label in self.retainers(string_node):
                    if edge_type == "property" and self.type(frm) in ("object", "closure"):
                        emit(frm, label, value)
                        if len(results) >= limit:
                            return results

        if constructor is not None or key is not None:
            for node in range(self.node_count):
                if self.type(node) != "object":
                    continue
                if constructor is not None and self.name(node) != constructor:
                    continue
                if key is not None and not any(
                    et == "property" and self._edge_label(et, raw) == key for et, raw, _ in self.edges_of(node)
                ):
                    continue
                emit(node, key, None)
                if len(results) >= limit:
                    break
        return results


# ---------------------------------------------------------------------------
# LiveObjects — patch/read heap objects
# ---------------------------------------------------------------------------
class LiveObjects:
    def __init__(self, engine: GhostwireEngine):
        self.engine = engine

    def _resolve(self, snapshot: Snapshot, sid, node_id, value, constructor, key):
        meta = {"id": node_id}
        if node_id is None:
            matches = snapshot.find_objects(value=value, constructor=constructor, key=key, limit=1)
            if not matches:
                return None, None
            meta, node_id = matches[0], matches[0]["id"]
        bridged = self.engine.send("HeapProfiler.getObjectByHeapObjectId", {"objectId": str(node_id)}, session_id=sid).get("result", {})
        return bridged.get("objectId"), meta

    def patch(self, node_id=None, value=None, constructor=None, key=None, assign=None, apply=None, target_url=None):
        sid = self.engine.resolve_session(target_url)
        snapshot = take_snapshot(self.engine, sid)
        handle, meta = self._resolve(snapshot, sid, node_id, value, constructor, key)
        if not handle:
            return {"patched": False, "error": "no matching live object, or its heap id no longer resolves"}
        result: dict[str, Any] = {"patched": False, "object": meta}
        if assign:
            keys = list(assign.keys())
            result["before"] = self._read_keys(handle, sid, keys)
            self._call(handle, sid, "function(p){ Object.assign(this, p); }", [{"value": assign}])
            result["after"] = self._read_keys(handle, sid, keys)
            result["patched"] = True
        if apply:
            result["apply_result"] = self._call(handle, sid, apply, [])
            result["patched"] = True
        return result

    def read(self, node_id=None, value=None, constructor=None, key=None, target_url=None):
        sid = self.engine.resolve_session(target_url)
        snapshot = take_snapshot(self.engine, sid)
        handle, meta = self._resolve(snapshot, sid, node_id, value, constructor, key)
        if not handle:
            return {"error": "no matching live object"}
        scalars = "function(){ var o={}; for (var k in this){ try { var v=this[k]; if (v===null||['string','number','boolean'].indexOf(typeof v)>=0) o[k]=v; } catch(e){} } return o; }"
        return {"object": meta, "properties": self._call(handle, sid, scalars, [])}

    def _read_keys(self, handle, sid, keys):
        return self._call(handle, sid, "function(keys){ var o={}; for (var k of keys) o[k]=this[k]; return o; }", [{"value": keys}])

    def _call(self, handle, sid, declaration, arguments):
        r = self.engine.send(
            "Runtime.callFunctionOn",
            {"objectId": handle, "functionDeclaration": declaration, "arguments": arguments, "returnByValue": True, "awaitPromise": True},
            session_id=sid,
        )
        if r.get("exceptionDetails"):
            return {"__error__": (r["exceptionDetails"].get("exception") or {}).get("description")}
        return r.get("result", {}).get("value")


# ---------------------------------------------------------------------------
# Oracle — verify candidate against corpus + live
# ---------------------------------------------------------------------------
class Oracle:
    def __init__(self, engine: GhostwireEngine, tracer: Tracer):
        self.engine = engine
        self.tracer = tracer
        self.scratch = None

    def corpus(self, label):
        with self.tracer.lock:
            return list(self.tracer.pairs.get(label, []))

    def corpora(self):
        with self.tracer.lock:
            return {k: len(v) for k, v in self.tracer.pairs.items()}

    def query_live(self, fn_expr, inputs, target_url=None):
        sid = self.engine.resolve_session(target_url)
        out = []
        for inp in inputs:
            args = inp if isinstance(inp, list) else [inp]
            try:
                r = self.engine.send(
                    "Runtime.evaluate",
                    {"expression": f"{GW}({fn_expr}).apply(null, {json.dumps(args)})", "returnByValue": True, "awaitPromise": True, "silent": True},
                    session_id=sid,
                )
            except Exception as e:
                out.append({"__error__": f"cdp: {e}"})
                continue
            if r.get("exceptionDetails"):
                out.append({"__error__": _exc(r["exceptionDetails"])})
            else:
                out.append(r.get("result", {}).get("value"))
        return out

    def run_candidate(self, candidate, inputs):
        if self.scratch is None:
            self.scratch = self.engine.open_isolated_page()
            # give target a moment to be ready if CDP
            time.sleep(0.15)
        for attempt in range(2):
            sid = self.scratch
            # if we have no session, just reuse main
            eval_sid = sid if sid not in (None, "window_open") else None
            out = []
            ok = True
            for inp in inputs:
                args = inp if isinstance(inp, list) else [inp]
                try:
                    r = self.engine.send(
                        "Runtime.evaluate",
                        {"expression": f"({candidate}).apply(null, {json.dumps(args)})", "returnByValue": True, "awaitPromise": True},
                        session_id=eval_sid,
                    )
                except Exception as e:
                    out.append({"__error__": f"cdp: {e}"})
                    continue
                if r.get("exceptionDetails"):
                    ex = r["exceptionDetails"]
                    # isolated target may not exist — retry once on main
                    if eval_sid is not None and attempt == 0:
                        ok = False
                        break
                    out.append({"__error__": _exc(ex)})
                else:
                    out.append(r.get("result", {}).get("value"))
            if ok:
                return out
        return out

    def verify(self, fn_expr, candidate, label=None, fresh_inputs=None, target_url=None, sample=200, max_mismatches=5):
        tests: list[tuple] = []
        skipped = 0
        if label:
            for p in self.corpus(label)[:sample]:
                if p.get("output") is None and p.get("unserialized"):
                    skipped += 1
                    continue
                tests.append((p["input"], p.get("output"), "corpus"))

        live_errors = 0
        if fresh_inputs:
            for inp, expected in zip(fresh_inputs, self.query_live(fn_expr, fresh_inputs, target_url)):
                if isinstance(expected, dict) and "__error__" in expected:
                    live_errors += 1
                else:
                    tests.append((inp, expected, "live"))

        got = self.run_candidate(candidate, [t[0] for t in tests])
        matched, mismatches = 0, []
        for (inp, expected, source), result in zip(tests, got):
            if _equal(expected, result):
                matched += 1
            elif len(mismatches) < max_mismatches:
                mismatches.append({"input": inp, "expected": expected, "got": result, "source": source})

        corpus_n = sum(1 for t in tests if t[2] == "corpus")
        notes = f"{corpus_n} corpus + {len(tests) - corpus_n} live inputs; candidate run isolated"
        if skipped:
            notes += f"; {skipped} corpus pairs unverifiable (non-serializable output)"
        if live_errors:
            notes += f"; {live_errors} fresh inputs errored on the live function"
        if not tests:
            notes += "; NO ground truth — capture a corpus or pass fresh_inputs"
        return {"verified": bool(tests) and matched == len(tests), "tested": len(tests), "matched": matched, "mismatches": mismatches, "coverage_notes": notes}

    def close(self):
        if self.scratch is not None:
            self.engine.close_target(self.scratch)
            self.scratch = None


def _exc(details):
    exc = details.get("exception") or {}
    return exc.get("description") or details.get("text") or "exception"


def _equal(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b and type(a) == type(b)


# ---------------------------------------------------------------------------
# OriginTracer
# ---------------------------------------------------------------------------
class OriginTracer:
    def __init__(self, engine: GhostwireEngine):
        self.engine = engine

    def trace(self, value, enter, trigger=None, target_url=None, blackbox=None, max_steps=300, max_returns=40):
        sid = self.engine.resolve_session(target_url)
        box: dict[str, Any] = {}
        ready = threading.Event()

        def on_paused(params, s=None):
            box["frames"], box["sid"] = params.get("callFrames", []), s
            ready.set()

        def wait_pause(timeout=20):
            if not ready.wait(timeout):
                return None
            ready.clear()
            return box.get("frames")

        saved = list(self.engine.handlers.get("Debugger.paused", []))
        self.engine.handlers["Debugger.paused"] = [on_paused]
        patterns = DEFAULT_BLACKBOX if blackbox is None else blackbox
        self._safe("Debugger.setBlackboxPatterns", {"patterns": patterns}, sid)

        breakpoint_id = None
        try:
            fn = self.engine.send("Runtime.evaluate", {"expression": enter, "silent": True}, session_id=sid).get("result", {})
            if fn.get("type") != "function" or not fn.get("objectId"):
                return {"found": False, "error": f"{enter!r} is not a function in {target_url or 'page'}"}
            breakpoint_id = self.engine.send("Debugger.setBreakpointOnFunctionCall", {"objectId": fn["objectId"]}, session_id=sid).get("breakpointId")
            if trigger:
                try:
                    self.engine.send("Runtime.evaluate", {"expression": trigger, "silent": True}, session_id=sid)
                except Exception:
                    pass
            frames = wait_pause()
            if not frames:
                return {"found": False, "error": f"{enter!r} was not called (provide a trigger, or invoke it)"}
            preexisting = bool(take_snapshot(self.engine, sid).find_value(value))
            trail, origin, previous, returns = [], None, frames, 0
            for _ in range(max_steps):
                self.engine.send("Debugger.stepInto", session_id=sid)
                frames = wait_pause()
                if not frames:
                    break
                if len(frames) < len(previous):
                    returned = previous[0]
                    present = bool(take_snapshot(self.engine, sid).find_value(value))
                    loc = returned.get("functionLocation", {})
                    step = {
                        "function": returned.get("functionName") or "<anonymous>",
                        "url": returned.get("url", ""),
                        "line": (loc.get("lineNumber") or 0) + 1,
                        "column": (loc.get("columnNumber") or 0) + 1,
                        "value_present_after": present,
                    }
                    trail.append(step)
                    returns += 1
                    if present and origin is None and not preexisting:
                        origin = step
                        break
                    if returns >= max_returns:
                        break
                previous = frames
            notes = []
            if preexisting:
                notes.append("value already in the heap before enter ran — its origin is upstream of the trace start; break on an earlier function")
            elif origin is None:
                notes.append("value never appeared at a user-land return within the step budget — raise max_steps, widen blackbox, or start deeper")
            return {
                "found": origin is not None,
                "value": value,
                "origin": origin,
                "preexisting": preexisting,
                "returns_observed": returns,
                "trail": trail,
                "confidence": "high" if origin else "none",
                "notes": "; ".join(notes),
            }
        finally:
            if breakpoint_id:
                self._safe("Debugger.removeBreakpoint", {"breakpointId": breakpoint_id}, sid)
            self._safe("Debugger.setBlackboxPatterns", {"patterns": []}, sid)
            self.engine.handlers["Debugger.paused"] = saved
            self._safe("Debugger.resume", None, sid)

    def _safe(self, method, params, sid):
        try:
            self.engine.send(method, params, session_id=sid)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DataflowTracer
# ---------------------------------------------------------------------------
class DataflowTracer:
    def __init__(self, engine: GhostwireEngine):
        self.engine = engine

    def follow(self, producer, trigger=None, target_url=None, blackbox=None, timeout=20):
        sid = self.engine.resolve_session(target_url)
        box: dict[str, Any] = {}
        ready = threading.Event()

        def on_paused(params, s=None):
            box["frames"], box["sid"] = params.get("callFrames", []), s
            ready.set()

        def wait_pause():
            if not ready.wait(timeout):
                return None
            ready.clear()
            return box.get("frames")

        saved = list(self.engine.handlers.get("Debugger.paused", []))
        self.engine.handlers["Debugger.paused"] = [on_paused]
        self._safe("Debugger.setBlackboxPatterns", {"patterns": DEFAULT_BLACKBOX if blackbox is None else blackbox}, sid)

        entry_bp, return_bps = None, []
        try:
            fn = self.engine.send("Runtime.evaluate", {"expression": producer, "silent": True}, session_id=sid).get("result", {})
            if fn.get("type") != "function" or not fn.get("objectId"):
                return {"error": f"{producer!r} is not a function in {target_url or 'page'}"}
            entry_bp = self.engine.send("Debugger.setBreakpointOnFunctionCall", {"objectId": fn["objectId"]}, session_id=sid).get("breakpointId")
            if trigger:
                try:
                    self.engine.send("Runtime.evaluate", {"expression": trigger, "silent": True}, session_id=sid)
                except Exception:
                    pass
            frames = wait_pause()
            if not frames:
                return {"error": f"{producer!r} was not called (provide a trigger, or invoke it)"}
            location = frames[0].get("functionLocation") or {}
            try:
                possible = self.engine.send("Debugger.getPossibleBreakpoints", {"start": location, "restrictToFunction": True}, session_id=sid).get("locations", [])
            except Exception:
                possible = []
            for ret in (l for l in possible if l.get("type") == "return"):
                try:
                    bp = self.engine.send("Debugger.setBreakpoint", {"location": ret}, session_id=sid).get("breakpointId")
                    if bp:
                        return_bps.append(bp)
                except Exception:
                    pass
            self.engine.send("Debugger.resume", session_id=sid)
            frames = wait_pause()
            if not frames:
                return {"error": f"{producer!r} did not reach a return"}
            rv = frames[0].get("returnValue", {}) or {}
            returned = {"type": rv.get("type"), "subtype": rv.get("subtype"), "value": rv.get("value")}
            is_async = rv.get("subtype") == "promise"
            self.engine.send("Debugger.stepOut", session_id=sid)
            frames = wait_pause()
            consumer = None
            if frames:
                top = frames[0]
                consumer = {
                    "function": top.get("functionName") or "<anonymous>",
                    "url": top.get("url", ""),
                    "line": (top.get("location", {}).get("lineNumber") or 0) + 1,
                    "variable": None if is_async else self._find_variable(top, rv, sid),
                }
            notes = []
            if is_async:
                notes.append("producer returns a Promise; the real consumer is the await/then continuation — this lands at the awaiter, not the resolved use")
            elif consumer and consumer["variable"] is None:
                notes.append("return value is not bound to a named variable in the consumer (passed inline or destructured); reporting the consumer frame only")
            return {"producer": producer, "returned": returned, "async": is_async, "consumer": consumer, "confidence": "high" if (consumer and consumer["variable"]) else "low", "notes": "; ".join(notes)}
        finally:
            for bp in filter(None, [entry_bp, *return_bps]):
                self._safe("Debugger.removeBreakpoint", {"breakpointId": bp}, sid)
            self._safe("Debugger.setBlackboxPatterns", {"patterns": []}, sid)
            self.engine.handlers["Debugger.paused"] = saved
            self._safe("Debugger.resume", None, sid)

    def _find_variable(self, frame, return_value, sid):
        if "value" not in return_value:
            return None
        target, target_type = return_value["value"], return_value.get("type")
        for scope in frame.get("scopeChain", []):
            if scope.get("type") not in ("local", "block", "closure"):
                continue
            object_id = (scope.get("object") or {}).get("objectId")
            if not object_id:
                continue
            try:
                props = self.engine.send("Runtime.getProperties", {"objectId": object_id, "ownProperties": True}, session_id=sid).get("result", [])
            except Exception:
                continue
            for prop in props:
                value = prop.get("value") or {}
                if "value" in value and value["value"] == target and value.get("type") == target_type:
                    return prop["name"]
        return None

    def _safe(self, method, params, sid):
        try:
            self.engine.send(method, params, session_id=sid)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CryptoLogger
# ---------------------------------------------------------------------------
class CryptoLogger:
    def __init__(self, engine: GhostwireEngine):
        self.engine = engine

    def capture(self, duration=4.0, target_url=None):
        sid = self.engine.resolve_session(target_url)
        entries: list[dict] = []
        lock = threading.Lock()
        breakpoint_method: dict[str, str] = {}

        def on_paused(params, s=None):
            try:
                self._record(params, s, breakpoint_method, entries, lock)
            finally:
                try:
                    self.engine.send("Debugger.resume", session_id=s)
                except Exception:
                    pass

        saved = list(self.engine.handlers.get("Debugger.paused", []))
        self.engine.handlers["Debugger.paused"] = [on_paused]
        breakpoints: list[str] = []
        try:
            for method in SUBTLE_METHODS:
                fn = self.engine.send("Runtime.evaluate", {"expression": f"crypto.subtle.{method}", "silent": True}, session_id=sid).get("result", {})
                if fn.get("type") == "function" and fn.get("objectId"):
                    bp = self.engine.send("Debugger.setBreakpointOnFunctionCall", {"objectId": fn["objectId"]}, session_id=sid).get("breakpointId")
                    if bp:
                        breakpoint_method[bp] = f"crypto.subtle.{method}"
                        breakpoints.append(bp)
            time.sleep(duration)
        finally:
            for bp in breakpoints:
                try:
                    self.engine.send("Debugger.removeBreakpoint", {"breakpointId": bp}, session_id=sid)
                except Exception:
                    pass
            self.engine.handlers["Debugger.paused"] = saved
        return entries

    def _record(self, params, sid, breakpoint_method, entries, lock):
        frames = params.get("callFrames", [])
        if not frames:
            return
        hit = params.get("hitBreakpoints") or []
        method = next((breakpoint_method[b] for b in hit if b in breakpoint_method), "crypto.subtle.*")
        caller = frames[0]
        inputs: list[dict] = []
        for scope in caller.get("scopeChain", []):
            if scope.get("type") not in ("local", "closure", "block"):
                continue
            object_id = (scope.get("object") or {}).get("objectId")
            if not object_id:
                continue
            try:
                props = self.engine.send("Runtime.getProperties", {"objectId": object_id, "ownProperties": True}, session_id=sid).get("result", [])
            except Exception:
                continue
            for prop in props:
                value = prop.get("value") or {}
                if value.get("type") == "string" and value.get("value"):
                    inputs.append({"name": prop["name"], "kind": "string", "value": str(value["value"])[:128]})
                elif value.get("subtype") in ("arraybuffer", "typedarray", "dataview") and value.get("objectId"):
                    try:
                        decoded = self.engine.send(
                            "Runtime.callFunctionOn", {"objectId": value["objectId"], "functionDeclaration": _DECODE, "returnByValue": True}, session_id=sid
                        ).get("result", {}).get("value")
                        if decoded:
                            inputs.append({"name": prop["name"], "kind": value["subtype"], **decoded})
                    except Exception:
                        pass
        with lock:
            entries.append({"method": method, "caller": caller.get("functionName") or "<anonymous>", "inputs": inputs})


# ---------------------------------------------------------------------------
# StringDumper + VMTracer
# ---------------------------------------------------------------------------
class StringDumper:
    def __init__(self, engine: GhostwireEngine):
        self.engine = engine

    def dump(self, decoder, start=0, count=512, extra_args=None, target_url=None, stop_after=48):
        sid = self.engine.resolve_session(target_url)
        suffix = "" if not extra_args else "," + ",".join(json.dumps(a) for a in extra_args)
        decoded: dict[int, str] = {}
        misses, errors = 0, 0
        for index in range(start, start + count):
            r = self.engine.send(
                "Runtime.evaluate", {"expression": f"/*gw*/({decoder})({index}{suffix})", "returnByValue": True, "silent": True}, session_id=sid
            )
            if r.get("exceptionDetails"):
                errors += 1
                misses += 1
            elif r.get("result", {}).get("type") == "string":
                decoded[index] = r["result"]["value"]
                misses = 0
            else:
                misses += 1
            if misses >= stop_after and decoded:
                break
        notes = f"{len(decoded)} strings over indices {start}..{start + count - 1}"
        if errors:
            notes += f"; {errors} indices threw (likely out of range)"
        return {"decoded": decoded, "count": len(decoded), "notes": notes}


class VMTracer:
    def __init__(self, engine: GhostwireEngine):
        self.engine = engine

    def watch(self, url_substr, line, column=0, watch=None, iterations=300, trigger=None, target_url=None, timeout=20):
        sid = self.engine.resolve_session(target_url)
        watch = watch or []
        box: dict[str, Any] = {}
        ready = threading.Event()

        def on_paused(params, s=None):
            box["frames"], box["sid"] = params.get("callFrames", []), s
            ready.set()

        def wait_pause():
            if not ready.wait(timeout):
                return None
            ready.clear()
            return box.get("frames")

        saved = list(self.engine.handlers.get("Debugger.paused", []))
        self.engine.handlers["Debugger.paused"] = [on_paused]
        breakpoint_id = None
        trace: list[dict] = []
        try:
            placed = self.engine.send("Debugger.setBreakpointByUrl", {"urlRegex": url_substr, "lineNumber": line - 1, "columnNumber": column}, session_id=sid)
            breakpoint_id = placed.get("breakpointId")
            if not placed.get("locations"):
                return {"error": f"no breakpoint resolved at {url_substr!r}:{line}", "hits": 0, "trace": []}
            if trigger:
                try:
                    self.engine.send("Runtime.evaluate", {"expression": trigger, "silent": True}, session_id=sid)
                except Exception:
                    pass
            for _ in range(iterations):
                frames = wait_pause()
                if not frames:
                    break
                frame = frames[0]
                row: dict[str, Any] = {}
                for expr in watch:
                    try:
                        ev = self.engine.send(
                            "Debugger.evaluateOnCallFrame", {"callFrameId": frame["callFrameId"], "expression": expr, "returnByValue": True}, session_id=sid
                        )
                        row[expr] = ev.get("result", {}).get("value")
                    except Exception:
                        row[expr] = "<error>"
                trace.append(row)
                self.engine.send("Debugger.resume", session_id=sid)
            return {"hits": len(trace), "trace": trace}
        finally:
            if breakpoint_id:
                try:
                    self.engine.send("Debugger.removeBreakpoint", {"breakpointId": breakpoint_id}, session_id=sid)
                except Exception:
                    pass
            self.engine.handlers["Debugger.paused"] = saved
            try:
                self.engine.send("Debugger.resume", session_id=sid)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# ScriptWatcher (Debugger.scriptParsed)
# ---------------------------------------------------------------------------
class ScriptWatcher:
    def __init__(self, engine: GhostwireEngine, fetch_source=True):
        self.engine = engine
        self.fetch_source = fetch_source
        self.scripts: list[dict] = []
        self.lock = threading.Lock()

    def attach(self, engine: GhostwireEngine):
        self.engine = engine
        engine.on("Debugger.scriptParsed", self._on_parsed)
        return self

    def _on_parsed(self, params, session_id=None):
        url = params.get("url") or ""
        script_id = params.get("scriptId") or ""
        # filter our own GW evals
        if "/*gw*/" in (params.get("embedderName") or ""):
            return
        stack = params.get("stackTrace")
        dynamic = not url and bool(stack)
        rec: dict[str, Any] = {
            "scriptId": script_id,
            "url": url,
            "dynamic": dynamic,
            "length": None,
            "source": None,
        }
        if self.fetch_source and script_id:
            try:
                src = self.engine.send("Debugger.getScriptSource", {"scriptId": script_id}, session_id=session_id).get("scriptSource", "")
                if "/*gw*/" in src[:200]:
                    return
                rec["source"] = src
                rec["length"] = len(src)
            except Exception:
                pass
        with self.lock:
            self.scripts.append(rec)

    def search(self, needle: str):
        if not needle:
            return list(self.scripts)
        low = needle.lower()
        with self.lock:
            return [s for s in self.scripts if low in (s.get("url") or "").lower() or low in (s.get("source") or "").lower()]

    def dynamic_only(self):
        with self.lock:
            return [s for s in self.scripts if s.get("dynamic")]
