/*
 * jupyter_runtime.js
 * Runs inside the selected Jupyter workspace page.
 * Talks to the Jupyter Server via same-origin fetch + kernel websocket, using
 * the login session already present in the iframe.
 *
 * SAFETY: this code reads the _xsrf cookie and the ?token from location only to
 * authenticate requests. It NEVER returns/echoes cookie, token, or Authorization
 * values. Callers must keep it that way.
 *
 * Defines window.__codexJupyter(op, payload) -> Promise<result>.
 */
(function () {
  function discoverBase() {
    var m = location.pathname.match(/^(.*\/notebook\/st\/[^/]+\/)/);
    var basePath = m ? m[1] : "/";
    return { basePath: basePath, baseUrl: location.origin + basePath };
  }
  function tokenFromLocation() {
    try { return new URLSearchParams(location.search).get("token") || ""; }
    catch (e) { return ""; }
  }
  function cookieMap() {
    var out = {};
    (document.cookie || "").split(";").filter(Boolean).forEach(function (x) {
      var i = x.indexOf("=");
      out[x.slice(0, i).trim()] = x.slice(i + 1);
    });
    return out;
  }

  var B = discoverBase();
  var runtimeKey = "4.0.1:" + B.basePath;
  if (window.__snRuntimeKey === runtimeKey && window.__codexJupyter) return;
  var apiUrl = function (p) { return B.baseUrl + String(p).replace(/^\//, ""); };
  var wsBase = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + B.basePath;
  var wsUrl = function (p) { return wsBase + String(p).replace(/^\//, ""); };
  var XSRF = cookieMap()._xsrf || "";
  var TOKEN = tokenFromLocation();

  function encPath(p) {
    return String(p).split("/").filter(function (s) { return s.length; })
      .map(encodeURIComponent).join("/");
  }

  async function jf(path, opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {});
    if (opts.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    if (XSRF && opts.method && opts.method !== "GET") headers["X-XSRFToken"] = XSRF;
    var res = await fetch(apiUrl(path), Object.assign(
      { credentials: "same-origin", cache: "no-store" }, opts, { headers: headers }));
    var ct = res.headers.get("content-type") || "";
    var body = ct.indexOf("application/json") >= 0 ? await res.json() : await res.text();
    return { status: res.status, ok: res.ok, body: body };
  }

  function rid(n) {
    var s = "", c = "abcdef0123456789";
    for (var i = 0; i < (n || 16); i++) s += c[(Math.random() * 16) | 0];
    return s;
  }
  function header(type, sess) {
    return { msg_id: rid(24), username: "codex", session: sess, msg_type: type,
             version: "5.3", date: new Date().toISOString() };
  }

  function clipText(value, limit) {
    value = String(value == null ? "" : value);
    return value.length <= limit ? value : value.slice(0, limit) + "…[truncated]";
  }
  function boundedError(ename, evalue, traceback) {
    return { ename: clipText(ename, 256), evalue: clipText(evalue, 4096),
             traceback: clipText(Array.isArray(traceback) ? traceback.slice(-20).join("\n") : traceback, 32768) };
  }

  // Tab-scoped records survive reloads without sharing a kernel with other controllers.
  var KERNEL_KEY = "__snKernelId:" + B.basePath;
  var OWNED_KEY = "__snOwnedKernels:" + B.basePath;
  var runQueue = Promise.resolve();

  function storageGet(key) { return sessionStorage.getItem(key); }
  function storageSet(key, value) { sessionStorage.setItem(key, value); }
  function storageRemove(key) { sessionStorage.removeItem(key); }
  function ownedKernels() {
    try { var v = JSON.parse(storageGet(OWNED_KEY) || "[]"); return Array.isArray(v) ? v : []; }
    catch (e) { return []; }
  }
  function rememberKernel(id) {
    var ids = ownedKernels();
    if (ids.indexOf(id) < 0) ids.push(id);
    storageSet(OWNED_KEY, JSON.stringify(ids.slice(-10)));
  }
  async function forgetKernel(id) {
    try { await jf("api/kernels/" + encodeURIComponent(id), { method: "DELETE" }); } catch (e) {}
    storageSet(OWNED_KEY, JSON.stringify(ownedKernels().filter(function (x) { return x !== id; })));
  }
  async function reapOwned(keep) {
    var ids = ownedKernels();
    for (var i = 0; i < ids.length; i++) {
      if (ids[i] !== keep) await forgetKernel(ids[i]);
    }
    if (keep) rememberKernel(keep);
  }
  async function kernelId() {
    var id = storageGet(KERNEL_KEY);
    if (id) {
      var live = await jf("api/kernels/" + encodeURIComponent(id));
      if (live.ok) return { id: id, created: false };
      storageRemove(KERNEL_KEY);
    }
    var k = await jf("api/kernels", { method: "POST", body: JSON.stringify({ name: "python3" }) });
    if (!k.ok) return { id: null, status: k.status };
    var created = k.body.id;
    storageSet(KERNEL_KEY, created);
    rememberKernel(created);
    await reapOwned(created);
    return { id: created, created: true };
  }
  function queued(task) {
    var run = runQueue.then(task, task);
    runQueue = run.then(function () {}, function () {});
    return run;
  }

  async function runPython(code, timeoutMs, streamLimitChars) {
    return queued(function () { return executeInKernel(code, timeoutMs, streamLimitChars); });
  }

  async function executeInKernel(code, timeoutMs, streamLimitChars) {
    timeoutMs = timeoutMs || 120000;
    var MAX_STREAM_CHARS = Math.max(16384, Math.min(Number(streamLimitChars) || 65536, 655360));
    var MAX_RESULT_CHARS = 16384;
    function appendBounded(current, value, limit) {
      value = String(value == null ? "" : value);
      var combined = current + value;
      if (combined.length <= limit) return { text: combined, truncated: false };
      return { text: combined.slice(combined.length - limit), truncated: true };
    }
    var kernel = await kernelId();
    if (!kernel.id) return { ok: false, stage: "create_kernel", status: kernel.status,
                                  error: "create_kernel_failed" };
    var kid = kernel.id;
    var sess = rid(24);
    var wsu = wsUrl("api/kernels/" + encodeURIComponent(kid) + "/channels") + "?session_id=" + sess;
    if (TOKEN) wsu += "&token=" + encodeURIComponent(TOKEN);
    var out = { ok: true, stdout: "", stderr: "", results: [], error: null, timedOut: false, kernel: kernel.created ? "created" : "reused", kernel_lost: false, stdout_truncated: false, stderr_truncated: false, results_truncated: false };
    try {
      await new Promise(function (resolve, reject) {
        var done = false, parentId = null;
        var ws = new WebSocket(wsu);
        var timer = setTimeout(function () {
          if (!done) { done = true; out.ok = false; out.timedOut = true;
            out.error = boundedError("TimeoutError", "kernel execution timed out", "");
            try { ws.close(); } catch (e) {} resolve(); }
        }, timeoutMs);
        ws.onopen = function () {
          var h = header("execute_request", sess);
          parentId = h.msg_id;
          ws.send(JSON.stringify({
            header: h, parent_header: {}, metadata: {}, channel: "shell",
            content: { code: code, silent: false, store_history: false,
                       user_expressions: {}, allow_stdin: false, stop_on_error: true }
          }));
        };
        ws.onerror = function () { if (!done) { done = true; out.ok = false;
          clearTimeout(timer); reject(new Error("ws_error")); } };
        ws.onclose = function () { if (!done) { done = true; out.ok = false; out.kernel_lost = true;
          out.error = boundedError("WebSocketClosed", "kernel channel closed before idle", "");
          clearTimeout(timer); resolve(); } };
        ws.onmessage = function (ev) {
          var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
          var pid = m.parent_header && m.parent_header.msg_id;
          if (parentId && pid && pid !== parentId) return;
          var t = m.header && m.header.msg_type, c = m.content || {};
          if (t === "stream") {
            var target = c.name === "stderr" ? "stderr" : "stdout";
            var appended = appendBounded(out[target], c.text, MAX_STREAM_CHARS);
            out[target] = appended.text;
            if (appended.truncated) out[target + "_truncated"] = true;
          }
          else if (t === "execute_result" || t === "display_data") {
            var d = c.data && c.data["text/plain"];
            if (d != null && out.results.length < 20) {
              d = String(d);
              if (d.length > MAX_RESULT_CHARS) { d = d.slice(0, MAX_RESULT_CHARS); out.results_truncated = true; }
              out.results.push(d);
            } else if (d != null) out.results_truncated = true;
          } else if (t === "error") {
            out.ok = false;
            out.error = boundedError(c.ename, c.evalue, c.traceback || []);
          } else if (t === "status" && c.execution_state === "idle") {
            if (!done) { done = true; clearTimeout(timer); resolve(); }
          }
        };
      });
    } finally {
      // A healthy kernel stays alive for the next call. Only a lost or timed-out one is
      // dropped: its leftover execution would otherwise block every later request.
      if (out.kernel_lost || out.timedOut) {
        await forgetKernel(kid);
        storageRemove(KERNEL_KEY);
      }
    }
    return out;
  }

  async function probe(lite) {
    var r = {};
    var st = await jf("api/status"); r.status_code = st.status;
    r.base_path = B.basePath;
    if (lite) return r;
    var root = await jf("api/contents?content=1");
    r.root_status = root.status;
    r.root_entries = (root.body && root.body.content || []).slice(0, 200).map(function (e) { return clipText(e.name, 256); });
    r.root_entries_truncated = !!(root.body && root.body.content && root.body.content.length > 200);
    var work = await jf("api/contents/work?content=0");
    r.work_status = work.status; r.work_writable = (work.body && work.body.writable) || false;
    var ses = await jf("api/sessions"); r.sessions = Array.isArray(ses.body) ? ses.body.length : null;
    var ks = await jf("api/kernels"); r.kernels = Array.isArray(ks.body) ? ks.body.length : null;
    var ts = await jf("api/terminals"); r.terminals = Array.isArray(ts.body) ? ts.body.length : null;
    return r;
  }

  async function listDir(path) {
    var r = await jf("api/contents/" + encPath(path) + "?content=1");
    if (!r.ok) return { ok: false, error: "list_failed", status: r.status };
    var all = (r.body && r.body.content || []);
    var entries = all.slice(0, 200).map(function (e) {
      return { name: e.name, type: e.type, size: e.size }; });
    return { ok: true, status: r.status, entries: entries, truncated: all.length > entries.length };
  }

  async function readFile(path, maxBytes) {
    var meta = await jf("api/contents/" + encPath(path) + "?content=0");
    if (!meta.ok) return { ok: false, error: "read_failed", status: meta.status };
    var size = meta.body && typeof meta.body.size === "number" ? meta.body.size : null;
    if (maxBytes && size !== null && size > maxBytes) {
      return { ok: false, error: "too_large", status: meta.status, size: size, limit: maxBytes };
    }
    var r = await jf("api/contents/" + encPath(path) + "?content=1&format=text");
    if (!r.ok) return { ok: false, error: "read_failed", status: r.status };
    var content = r.body && r.body.content;
    if (typeof content !== "string") content = null;
    return { ok: true, status: r.status, content: content, size: size };
  }

  async function statFile(path) {
    var r = await jf("api/contents/" + encPath(path) + "?content=0");
    if (!r.ok) return { ok: false, status: r.status,
                        error: r.status === 404 ? "not_found" : "stat_failed" };
    var body = r.body || {};
    return { ok: true, status: r.status, type: body.type, size: body.size,
             last_modified: body.last_modified };
  }

  async function fetchRange(path, start, end) {
    var url = B.basePath + "files/" + encPath(path);
    var r = await fetch(url, { credentials: "same-origin", cache: "no-store",
                               headers: { Range: "bytes=" + start + "-" + end } });
    if (r.status !== 206 && r.status !== 200) {
      return { ok: false, status: r.status, error: "range_failed" };
    }
    var buf = new Uint8Array(await r.arrayBuffer());
    var bin = "";
    for (var i = 0; i < buf.length; i += 8192) {
      bin += String.fromCharCode.apply(null, buf.subarray(i, i + 8192));
    }
    return { ok: true, status: r.status, len: buf.length, b64: btoa(bin) };
  }

  async function cleanupKernels() {
    var ids = ownedKernels();
    var live = storageGet(KERNEL_KEY);
    if (live && ids.indexOf(live) < 0) ids.push(live);
    var deleted = [];
    for (var i = 0; i < ids.length; i++) {
      var r = await jf("api/kernels/" + encodeURIComponent(ids[i]), { method: "DELETE" });
      deleted.push({ id: ids[i], status: r.status });
    }
    storageRemove(KERNEL_KEY);
    storageSet(OWNED_KEY, "[]");
    return { ok: true, deleted: deleted };
  }

  window.__codexJupyter = async function (op, payload) {
    payload = payload || {};
    try {
      if (op === "probe") {
        var lite = payload.lite === true;
        var pr = await probe(lite);
        var probeOk = pr.status_code === 200 &&
          (lite || (pr.root_status < 400 && pr.work_status < 400));
        return { ok: probeOk, data: pr, error: probeOk ? null : "jupyter_probe_failed" };
      }
      if (op === "list") { var ld = await listDir(payload.path || ""); return { ok: !!ld.ok, data: ld, error: ld.error || null }; }
      if (op === "read") { var rd = await readFile(payload.path, payload.max_bytes); return { ok: !!rd.ok, data: rd, error: rd.error || null }; }
      if (op === "cleanup") { var cl = await cleanupKernels(); return { ok: true, data: cl, error: null }; }
      if (op === "stat") { var st = await statFile(payload.path); return { ok: !!st.ok, data: st, error: st.error || null }; }
      if (op === "fetch_range") {
        var fr = await fetchRange(payload.path, payload.start, payload.end);
        return { ok: !!fr.ok, data: fr, error: fr.error || null };
      }
      if (op === "run_python") {
        var data = await runPython(payload.code || "", payload.timeout_ms, payload.stream_limit_chars);
        return { ok: !!data.ok, data: data, error: data.error || (data.timedOut ? "kernel_timeout" : null) };
      }
      return { ok: false, error: "unknown_op:" + op };
    } catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
  };
  window.__snRuntimeKey = runtimeKey;
})();
