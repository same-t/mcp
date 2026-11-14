"""Injected JS snippets for browser automation."""
from __future__ import annotations

_ELEMENTS_JS = r"""
(function(){
  // ── Stable ID registry ──────────────────────────────────────────────────
  if (!window.__mcpElCounter) window.__mcpElCounter = 1;

  var ATTR = 'data-mcp-el-id';

  function assignId(el) {
    var existing = el.getAttribute(ATTR);
    if (existing) return existing;
    var id = 'mcp' + (window.__mcpElCounter++);
    try { el.setAttribute(ATTR, id); } catch(e) {}
    return id;
  }

  // ── WAI-ARIA accessible name ─────────────────────────────────────────────
  function accessibleName(el) {
    // 1. aria-labelledby (highest priority)
    var lblBy = el.getAttribute('aria-labelledby');
    if (lblBy) {
      var parts = lblBy.trim().split(/\s+/).map(function(id) {
        var ref = document.getElementById(id);
        return ref ? (ref.innerText || ref.textContent || '').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ').slice(0, 80);
    }
    // 2. aria-label
    var al = (el.getAttribute('aria-label') || '').trim();
    if (al) return al.slice(0, 80);
    // 3. <label for="id">
    if (el.id) {
      var lbl = document.querySelector('label[for="' + el.id + '"]');
      if (lbl) return (lbl.innerText || lbl.textContent || '').trim().slice(0, 80);
    }
    // 4. closest <label> (exclude the element's own text to avoid duplication)
    var cl = el.closest('label');
    if (cl) {
      var lblTxt = (cl.innerText || cl.textContent || '').replace((el.innerText || el.value || ''), '').trim();
      if (lblTxt) return lblTxt.slice(0, 80);
    }
    // 5. innerText / value
    var txt = (el.innerText || el.textContent || '').trim();
    if (txt) return txt.slice(0, 80);
    // 6. title / alt (images inside buttons etc.)
    return (el.getAttribute('title') || el.getAttribute('alt') || '').slice(0, 80);
  }

  // ── Visibility helpers ───────────────────────────────────────────────────
  var vw = window.innerWidth, vh = window.innerHeight;

  function isVisible(el) {
    var r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    var st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    return true;
  }

  function inViewport(r) {
    return r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
  }

  function isOccluded(el, r) {
    // Only test when element is in the viewport
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    var top = document.elementFromPoint(cx, cy);
    if (!top) return false;
    // el itself or one of its descendants is on top → not occluded
    return top !== el && !el.contains(top);
  }

  // ── Shadow DOM traversal ─────────────────────────────────────────────────
  var TAGS = 'a[href],button,input,select,textarea,[role="button"],[role="link"],' +
             '[role="checkbox"],[role="radio"],[role="tab"],[role="menuitem"],[onclick],label[for],' +
             '[contenteditable="true"],[contenteditable=""],[contenteditable],[role="textbox"],[role="combobox"],' +
             '[role="searchbox"],div.ProseMirror,[data-lexical-editor],[data-slate-editor]';

  function collectElements(root, out) {
    root.querySelectorAll(TAGS).forEach(function(el) { out.push(el); });
    root.querySelectorAll('*').forEach(function(el) {
      if (el.shadowRoot) collectElements(el.shadowRoot, out);
    });
  }

  // ── Main loop ────────────────────────────────────────────────────────────
  var allEls = [];
  collectElements(document, allEls);

  var out = [];
  allEls.forEach(function(el) {
    var sid  = assignId(el);
    var vis  = isVisible(el);
    var r    = el.getBoundingClientRect();
    var inVp = vis && inViewport(r);
    var occ  = inVp && isOccluded(el, r);
    var name = accessibleName(el);

    out.push({
      i:           sid,
      tag:         el.tagName.toLowerCase(),
      type:        el.type || '',
      id:          el.id || '',
      name:        el.name || '',
      ph:          el.placeholder || '',
      label:       name,
      aria_label:  el.getAttribute('aria-label') || '',
      role:        el.getAttribute('role') || el.tagName.toLowerCase(),
      txt:         (el.innerText || el.value || '').trim().slice(0, 80),
      href:        el.href ? el.href.slice(0, 120) : null,
      disabled:    el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      checked:     (el.type === 'checkbox' || el.type === 'radio') ? el.checked : null,
      vis:         vis,
      in_viewport: inVp,
      occluded:    occ,
      needs_scroll: vis && !inVp,
      rect:        {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}
    });
  });

  return out;
})()
"""

_TRACE_JS = r"""
(function(){
  if (window.__sametTrace && window.__sametTrace.__patched) return;
  const T = window.__sametTrace = window.__sametTrace || {
    enabled: false,
    calls: [],
    listeners: [],
    __patched: false,
    __orig: {}
  };
  if (T.__patched) return;

  function _stack(skip){
    try { return (new Error().stack || "").split("\n").slice(skip||2, (skip||2)+9).join(" | "); } catch(e){ return ""; }
  }
  function _push(rec){
    if (!T.enabled) return;
    rec.t = Date.now();
    rec.ts = new Date().toISOString().slice(11,23);
    if (T.calls.length < 800) T.calls.push(rec);
  }
  // ---- addEventListener wrapping (covers React delegated root as well) ----
  try {
    const proto = EventTarget.prototype;
    const origAdd = proto.addEventListener;
    const origRemove = proto.removeEventListener;
    T.__orig.addEventListener = origAdd;
    T.__orig.removeEventListener = origRemove;
    const wrapMap = new WeakMap();
    proto.addEventListener = function(type, listener, opts){
      if (typeof listener !== 'function') return origAdd.call(this, type, listener, opts);
      // record registration even when not enabled
      try { T.listeners.push({event:type, tag:(this.tagName||this.constructor.name||'window'), id:this.id||'', stack:_stack(3)}); if(T.listeners.length>400) T.listeners.shift(); } catch(e){}
      let wrapped = wrapMap.get(listener);
      if (!wrapped) {
        wrapped = function(/*event*/){
          const st = _stack(2);
          const tgt = (this && this.tagName) ? (this.tagName + (this.id ? '#'+this.id : '')) : (type);
          _push({kind:'listener', event:type, target:tgt, stack:st});
          return listener.apply(this, arguments);
        };
        wrapMap.set(listener, wrapped);
        // keep reverse map for remove
        if (!wrapped.__orig) wrapped.__orig = listener;
      }
      return origAdd.call(this, type, wrapped, opts);
    };
    proto.removeEventListener = function(type, listener, opts){
      const wrapped = listener ? wrapMap.get(listener) : null;
      return origRemove.call(this, type, wrapped || listener, opts);
    };
  } catch(e){}

  // ---- fetch ----
  try {
    const origFetch = window.fetch;
    if (origFetch) {
      T.__orig.fetch = origFetch;
      window.fetch = function(input, init){
        const url = typeof input === 'string' ? input : (input && input.url) || String(input);
        const method = (init && init.method) || 'GET';
        _push({kind:'fetch', method:method, url:String(url).slice(0,300), stack:_stack(2)});
        return origFetch.apply(this, arguments).then(function(r){
          _push({kind:'fetch:response', method:method, url:String(url).slice(0,300), status:r.status, stack:_stack(2)});
          return r;
        }, function(err){
          _push({kind:'fetch:error', method:method, url:String(url).slice(0,300), error:String(err).slice(0,200), stack:_stack(2)});
          throw err;
        });
      };
    }
  } catch(e){}

  // ---- XHR ----
  try {
    const OrigXHR = window.XMLHttpRequest;
    const origOpen = OrigXHR.prototype.open;
    const origSend = OrigXHR.prototype.send;
    OrigXHR.prototype.open = function(method, url){ this.__sametMethod=method; this.__sametUrl=url; return origOpen.apply(this, arguments); };
    OrigXHR.prototype.send = function(body){
      const xhr=this; const method=xhr.__sametMethod||'GET'; const url=xhr.__sametUrl||'';
      _push({kind:'xhr', method:method, url:String(url).slice(0,300), stack:_stack(2)});
      function onLoad(){ _push({kind:'xhr:response', method:method, url:String(url).slice(0,300), status:xhr.status, stack:_stack(2)}); }
      function onErr(){ _push({kind:'xhr:error', method:method, url:String(url).slice(0,300), stack:_stack(2)}); }
      xhr.addEventListener('load', onLoad, {once:true});
      xhr.addEventListener('error', onErr, {once:true});
      return origSend.apply(this, arguments);
    };
  } catch(e){}

  // ---- setTimeout / rAF (shows async continuations) ----
  try {
    const origST = window.setTimeout;
    T.__orig.setTimeout = origST;
    window.setTimeout = function(fn, ms){
      if (typeof fn === 'function') {
        const wrapped = function(){ _push({kind:'timeout', delay:ms, stack:_stack(2)}); return fn.apply(this, arguments); };
        return origST.call(this, wrapped, ms);
      }
      return origST.apply(this, arguments);
    };
    const origRAF = window.requestAnimationFrame;
    if (origRAF) {
      window.requestAnimationFrame = function(fn){
        const wrapped = function(ts){ _push({kind:'rAF', stack:_stack(2)}); return fn.call(this, ts); };
        return origRAF.call(this, wrapped);
      };
    }
  } catch(e){}

  // ---- console.error passthrough (so pageErrors also surface here) ----
  try {
    const origErr = console.error;
    console.error = function(){ try{ _push({kind:'console:error', text:Array.from(arguments).join(' ').slice(0,300), stack:_stack(2)});}catch(e){} return origErr.apply(this, arguments); };
  } catch(e){}

  window.__sametTraceStart = function(clear){
    if (clear!==false) T.calls=[];
    T.enabled=true; return {ok:true, enabled:true, calls:T.calls.length};
  };
  window.__sametTraceStop = function(){ T.enabled=false; return {ok:true, enabled:false, calls:T.calls.length}; };
  window.__sametTraceClear = function(){ T.calls=[]; T.listeners=[]; return {ok:true}; };
  window.__sametTraceGet = function(){ return {enabled:T.enabled, calls:T.calls.slice(-600), listeners:T.listeners.slice(-200)}; };

  T.__patched = true;
})()
"""
