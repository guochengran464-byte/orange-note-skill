#!/usr/bin/env python3
"""Orange Note browser-backed agent operations, v4.0.1.
Provides file inspection/transfer, bounded execution and persistent jobs through
the built-in Jupyter workspace connector. Browser credentials stay in the page.
The existing sn.py filename and runtime configuration remain compatible.
"""
from __future__ import annotations
import argparse, base64, binascii, hashlib, json, os, posixpath, re, shlex, shutil, subprocess, sys, time, uuid
from urllib.parse import parse_qs, urlencode, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
RUNTIME_JS_PATH = os.path.join(HERE, "jupyter_runtime.js")
CONTROLLER_VERSION = "4.0.1"


DEFAULT_CFG = {
    "session": "stereonote",
    "work_root": "/data/work",
    "bsk_bin": "",
}
CONFIG_EXAMPLE_PATH = os.path.join(SKILL_DIR, "config.example.json")
LEGACY_CONFIG_PATH = os.path.join(SKILL_DIR, "config.json")


def config_path():
    """Return a per-user config path, never a path inside the Git checkout."""
    override = os.environ.get("SN_CONFIG_DIR")
    if override:
        base = os.path.expanduser(override)
    elif os.name == "nt" and os.environ.get("APPDATA"):
        base = os.path.join(os.environ["APPDATA"], "stereonote")
    else:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "stereonote")
    return os.path.join(base, "config.json")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}


def _load_cfg():
    cfg = dict(DEFAULT_CFG)
    cfg.update(_read_json(CONFIG_EXAMPLE_PATH))
    # Backward-compatible read only. v3.2 never writes this legacy repo-local file.
    cfg.update(_read_json(LEGACY_CONFIG_PATH))
    cfg.update(_read_json(config_path()))
    return cfg


def _workspace_ids(url):
    """Extract only projectId/workspaceId from a DCS StereoNote URL."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("invalid_workspace_url")
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "dcs.cloud" or host.endswith(".dcs.cloud")):
        raise ValueError("workspace_url_must_be_https_dcs_cloud")
    # StereoNote parameters live after '#', e.g. /notebookEmbed?projectId=...&workspaceId=...
    frag_query = parts.fragment.split("?", 1)[1] if "?" in parts.fragment else ""
    values = parse_qs(frag_query, keep_blank_values=False)
    project = (values.get("projectId") or [""])[0].strip()
    workspace = (values.get("workspaceId") or [""])[0].strip()
    if not project or not workspace:
        raise ValueError("workspace_url_missing_project_or_workspace_id")
    # Keep identifiers printable and bounded; never persist arbitrary query/token material.
    for value in (project, workspace):
        if len(value) > 512 or any(ord(ch) < 32 for ch in value):
            raise ValueError("invalid_workspace_identifier")
    return project, workspace


def _build_workspace_url(project_id, workspace_id):
    if not project_id or not workspace_id:
        return ""
    return ("https://www.dcs.cloud/stereonote/#/notebookEmbed?" +
            urlencode({"projectId": str(project_id), "workspaceId": str(workspace_id)}))


def canonical_workspace_url(url):
    return _build_workspace_url(*_workspace_ids(url))


CFG = _load_cfg()
BSK_BIN = (os.environ.get("BSK_BIN") or CFG.get("bsk_bin") or
           shutil.which("bsk") or
           os.path.join(os.path.expanduser("~"), ".local", "bin",
                        "bsk.exe" if os.name == "nt" else "bsk"))
_raw_env_url = os.environ.get("SN_URL", "").strip()
if _raw_env_url:
    try:
        WORKSPACE_URL = canonical_workspace_url(_raw_env_url)
        WORKSPACE_URL_ERROR = None
    except ValueError as exc:
        WORKSPACE_URL = ""
        WORKSPACE_URL_ERROR = str(exc)
else:
    WORKSPACE_URL = _build_workspace_url(CFG.get("project_id", ""), CFG.get("workspace_id", ""))
    # Legacy read support: sanitize an old workspace_url in memory, but never rewrite it here.
    WORKSPACE_URL_ERROR = None
    if not WORKSPACE_URL and CFG.get("workspace_url"):
        try:
            WORKSPACE_URL = canonical_workspace_url(CFG.get("workspace_url"))
        except ValueError as exc:
            WORKSPACE_URL_ERROR = str(exc)


def set_runtime_url(url):
    """Validate a workspace URL and use its canonical form for this process only."""
    global WORKSPACE_URL, WORKSPACE_URL_ERROR
    WORKSPACE_URL = canonical_workspace_url(url)
    WORKSPACE_URL_ERROR = None
    return WORKSPACE_URL


def save_url(url):
    """Persist only DCS project/workspace identifiers in a per-user config file."""
    global WORKSPACE_URL, WORKSPACE_URL_ERROR, CFG
    project_id, workspace_id = _workspace_ids(url)
    WORKSPACE_URL = _build_workspace_url(project_id, workspace_id)
    WORKSPACE_URL_ERROR = None
    cfg = _read_json(config_path())
    for key in ("session", "work_root", "bsk_bin"):
        cfg.setdefault(key, CFG.get(key, DEFAULT_CFG[key]))
    cfg["project_id"] = project_id
    cfg["workspace_id"] = workspace_id
    cfg.pop("workspace_url", None)
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    CFG = _load_cfg()

WORK_ROOT = CFG.get("work_root", "/data/work").rstrip("/")
INSPECT_ROOT = "/data/work"
JOB_ROOT = "/data/work/agent_jobs"
MAX_LOG_TAIL = 200
MAX_LOG_BYTES = 64 * 1024
MAX_STATUS_BYTES = 4096
MAX_INSPECT_BYTES = 1024 * 1024
MAX_INSPECT_RESULT_BYTES = 48 * 1024
MAX_INSPECT_ITEMS = 200
MAX_INSPECT_VALUE_CHARS = 256
MAX_NOTEBOOK_CELLS = 200
MAX_SHELL_BYTES = 64 * 1024
MAX_INTERNAL_SHELL_BYTES = 192 * 1024
MAX_KERNEL_STREAM_CHARS = 640 * 1024
MAX_ARTIFACT_HASH_BYTES = 64 * 1024 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_CODE_BYTES = 1024 * 1024
MAX_COMMAND_BYTES = 64 * 1024
MAX_SYNC_SECONDS = 50
DEFAULT_TEXT_PREVIEW_BYTES = 16 * 1024
DEFAULT_NOTEBOOK_SUMMARY_BYTES = 1024 * 1024
_JOB_ID_RE = re.compile(
    r"^(?:[a-z][a-z0-9_-]{0,31}_)?[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class Bridge(RuntimeError):
    pass


def validate_job_id(value):
    """Return a safe, UUID-backed job id or raise ValueError."""
    if not isinstance(value, str) or not _JOB_ID_RE.fullmatch(value):
        raise ValueError("invalid_job_id")
    return value


def validate_tail(value):
    """Return a bounded log-tail line count or raise ValueError."""
    if isinstance(value, bool):
        raise ValueError("invalid_tail")
    try:
        tail = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_tail") from exc
    if not 1 <= tail <= MAX_LOG_TAIL or str(value).strip() != str(tail):
        raise ValueError("invalid_tail")
    return tail


def validate_inspect_bytes(value):
    """Return a bounded server-side inspection byte limit or raise ValueError."""
    if isinstance(value, bool):
        raise ValueError("invalid_inspect_bytes")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_inspect_bytes") from exc
    if not 1 <= limit <= MAX_INSPECT_BYTES or str(value).strip() != str(limit):
        raise ValueError("invalid_inspect_bytes")
    return limit


def validate_text_payload(value, max_bytes, error):
    """Validate a UTF-8 text payload without silently truncating executable content."""
    if not isinstance(value, str):
        raise ValueError(error)
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(error)
    return value


def validate_sync_timeout(value):
    """Keep synchronous browser operations below the bridge's practical ceiling."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SYNC_SECONDS:
        raise ValueError(f"sync_timeout_must_be_1_to_{MAX_SYNC_SECONDS}_seconds_use_submit_for_long_jobs")
    return value


def read_local_text(path, max_bytes, error):
    """Read a local UTF-8 text file with a hard byte ceiling."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes + 1)
    except OSError as exc:
        raise ValueError(f"local_file_read_failed: {exc}") from exc
    if len(raw) > max_bytes:
        raise ValueError(error)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("local_file_not_utf8") from exc


def make_job_id(label=None, idempotency_key=None):
    """Create a safe UUID-backed job id, optionally namespaced by a label.

    A supplied idempotency key yields a deterministic id, so a retried submit
    resolves to the same job instead of starting a second one.
    """
    prefix = "job"
    if label is not None:
        if not isinstance(label, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", label):
            raise ValueError("invalid_job_label: use [a-z][a-z0-9_-]{0,31}, for example analysis_1")
        prefix = label
    if idempotency_key is not None:
        if (not isinstance(idempotency_key, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key)):
            raise ValueError("invalid_idempotency_key")
        identity = json.dumps([*_workspace_ids(WORKSPACE_URL), idempotency_key])
        return f"job_{uuid.uuid5(uuid.NAMESPACE_URL, 'stereonote:' + identity)}"
    return f"{prefix}_{uuid.uuid4()}"


# ─────────────────────────── local job receipts ───────────────────────────
def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _receipts_dir():
    return os.path.join(os.path.dirname(config_path()), "jobs")


def _read_receipt(job_id):
    try:
        record = _read_json(os.path.join(_receipts_dir(), validate_job_id(job_id) + ".json"))
    except ValueError:
        return None
    return record or None


def _write_receipt(job_id, state, exclusive=False, **fields):
    """Persist the receipt before and after dispatch so a lost ack never loses the id."""
    path = os.path.join(_receipts_dir(), validate_job_id(job_id) + ".json")
    record = {} if exclusive else _read_json(path)
    record.update(fields)
    record["job_id"] = job_id
    record["state"] = state
    record["updated_at"] = _utc_now()
    record.setdefault("created_at", record["updated_at"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + "." + uuid.uuid4().hex + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    try:
        if exclusive:
            os.link(tmp, path)
        else:
            os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return record


def list_receipts(limit=20):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("invalid_limit")
    root = _receipts_dir()
    try:
        names = [name for name in os.listdir(root) if name.endswith(".json")]
    except OSError:
        return []
    records = [record for record in
               (_read_json(os.path.join(root, name)) for name in names) if record]
    records.sort(key=lambda record: record.get("updated_at") or "", reverse=True)
    return records[:limit]


def build_job_launcher(job_id, command, cwd=None):
    """Build a detached shell launcher with durable job-state records."""
    job_id = validate_job_id(job_id)
    if not isinstance(command, str) or not command:
        raise ValueError("invalid_command")
    validate_text_payload(command, MAX_COMMAND_BYTES, "command_too_large")
    jdir = f"{JOB_ROOT}/{job_id}"
    execution_cwd = to_server(cwd or WORK_ROOT)
    qdir = shlex.quote(jdir)
    qcwd = shlex.quote(execution_cwd)
    qjob = shlex.quote(json.dumps(job_id))
    manifest = json.dumps({"schema_version": 2, "job_id": job_id, "command": command, "cwd": execution_cwd},
                          ensure_ascii=False, separators=(",", ":"))
    qmanifest = shlex.quote(manifest)
    command_delimiter = f"__SN_COMMAND_{uuid.uuid4().hex}__"
    while command_delimiter in command.splitlines():
        command_delimiter = f"__SN_COMMAND_{uuid.uuid4().hex}__"
    return f"""set -e
umask 077
if [ -L {shlex.quote(JOB_ROOT)} ]; then echo controller_job_root_is_symlink >&2; exit 1; fi
if [ ! -e {shlex.quote(JOB_ROOT)} ]; then mkdir {shlex.quote(JOB_ROOT)}; fi
if [ ! -d {shlex.quote(JOB_ROOT)} ] || [ -L {shlex.quote(JOB_ROOT)} ]; then echo invalid_controller_job_root >&2; exit 1; fi
chmod 700 {shlex.quote(JOB_ROOT)}
mkdir {qdir}
chmod 700 {qdir}
mkdir {qdir}/artifacts
chmod 700 {qdir}/artifacts
: > {qdir}/stdout.log
: > {qdir}/stderr.log
chmod 600 {qdir}/stdout.log {qdir}/stderr.log
printf '%s\\n' {qmanifest} > {qdir}/manifest.json.tmp
mv {qdir}/manifest.json.tmp {qdir}/manifest.json
chmod 600 {qdir}/manifest.json
printf '{{\"state\":\"queued\",\"job_id\":%s}}\\n' {qjob} > {qdir}/status.json.tmp
mv {qdir}/status.json.tmp {qdir}/status.json
chmod 600 {qdir}/status.json
cat > {qdir}/cmd.sh <<'{command_delimiter}'
{command}
{command_delimiter}
chmod 600 {qdir}/cmd.sh
cat > {qdir}/runner.sh <<'__SN_RUNNER__'
#!/usr/bin/env bash
set +e
umask 077
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\\n' "$$" > {qdir}/pid.tmp.$$ && mv {qdir}/pid.tmp.$$ {qdir}/pid
chmod 600 {qdir}/pid
printf '%s\\n' "$started_at" > {qdir}/started_at.tmp.$$ && mv {qdir}/started_at.tmp.$$ {qdir}/started_at
chmod 600 {qdir}/started_at
printf '{{\"state\":\"running\",\"job_id\":%s,\"pid\":%s,\"started_at\":\"%s\"}}\\n' {qjob} "$$" "$started_at" > {qdir}/status.json.tmp.$$
mv {qdir}/status.json.tmp.$$ {qdir}/status.json
chmod 600 {qdir}/status.json
printf '%s\\n' "$started_at" > {qdir}/runner.ready.tmp.$$ && mv {qdir}/runner.ready.tmp.$$ {qdir}/runner.ready
chmod 600 {qdir}/runner.ready
finalize() {{
  rc=$?
  trap - EXIT TERM INT
  ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '%s\\n' "$ended_at" > {qdir}/ended_at.tmp.$$ && mv {qdir}/ended_at.tmp.$$ {qdir}/ended_at
  chmod 600 {qdir}/ended_at
  printf '%s\\n' "$rc" > {qdir}/exit_code.tmp.$$ && mv {qdir}/exit_code.tmp.$$ {qdir}/exit_code
  chmod 600 {qdir}/exit_code
  python3 - {qdir}/artifacts > {qdir}/artifacts.json.tmp.$$ <<'__SN_ARTIFACTS_PY__'
import hashlib, json, os, stat, sys
MAX_HASH_BYTES = 64 * 1024 * 1024
root = sys.argv[1]
items = []
manifest_error = None
if os.path.isdir(root) and not os.path.islink(root):
    os.chmod(root, 0o700)
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(base, d)))
        for dirname in dirs:
            try: os.chmod(os.path.join(base, dirname), 0o700)
            except OSError: pass
        for name in sorted(names):
            path = os.path.join(base, name)
            try:
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode): continue
                os.chmod(path, 0o600)
                info = os.lstat(path)
                record = {{'path': os.path.relpath(path, root), 'size': info.st_size,
                           'mtime_ns': info.st_mtime_ns}}
                if info.st_size <= MAX_HASH_BYTES:
                    digest = hashlib.sha256()
                    with open(path, 'rb') as fh:
                        for block in iter(lambda: fh.read(1048576), b''): digest.update(block)
                    record['sha256'] = digest.hexdigest()
                    record['checksum'] = 'sha256'
                else:
                    record['sha256'] = None
                    record['checksum'] = 'skipped_large_file'
                items.append(record)
            except OSError: pass
else:
    manifest_error = 'unsafe_artifact_root'
payload = {{'files': items}}
if manifest_error: payload['error'] = manifest_error
print(json.dumps(payload, sort_keys=True))
__SN_ARTIFACTS_PY__
  mv {qdir}/artifacts.json.tmp.$$ {qdir}/artifacts.json 2>/dev/null || true
  chmod 600 {qdir}/artifacts.json 2>/dev/null || true
  printf '{{\"state\":\"finished\",\"job_id\":%s,\"pid\":%s,\"started_at\":\"%s\",\"ended_at\":\"%s\",\"exit_code\":%s}}\\n' {qjob} "$$" "$started_at" "$ended_at" "$rc" > {qdir}/status.json.tmp.$$
  mv {qdir}/status.json.tmp.$$ {qdir}/status.json
  chmod 600 {qdir}/status.json
  exit "$rc"
}}
trap finalize EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
export SN_JOB_DIR={qdir}
export SN_ARTIFACT_DIR={qdir}/artifacts
cd {qcwd} || exit 72
bash {qdir}/cmd.sh > {qdir}/stdout.log 2> {qdir}/stderr.log
__SN_RUNNER__
chmod 700 {qdir}/runner.sh
setsid bash {qdir}/runner.sh < /dev/null > {qdir}/launcher.log 2>&1 &
runner_pid=$!
printf '%s\\n' "$runner_pid" > {qdir}/pid.tmp && mv {qdir}/pid.tmp {qdir}/pid
chmod 600 {qdir}/pid {qdir}/launcher.log
ready_attempt=0
while [ "$ready_attempt" -lt 50 ]; do
  if [ -f {qdir}/runner.ready ] && [ ! -L {qdir}/runner.ready ]; then break; fi
  if ! kill -0 "$runner_pid" 2>/dev/null; then break; fi
  ready_attempt=$((ready_attempt + 1))
  sleep 0.1
done
if [ ! -f {qdir}/runner.ready ] || [ -L {qdir}/runner.ready ]; then
  kill "$runner_pid" 2>/dev/null || true
  ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '{{\"state\":\"setup_error\",\"job_id\":%s,\"error\":\"runner_start_timeout\",\"ended_at\":\"%s\"}}\\n' {qjob} "$ended_at" > {qdir}/status.json.tmp.$$
  mv {qdir}/status.json.tmp.$$ {qdir}/status.json
  chmod 600 {qdir}/status.json
  echo runner_start_timeout >&2
  exit 1
fi
echo __SN_SUBMITTED__={job_id}
"""


# ─────────────────────────── browser transport (Tencent BrowserSkill) ───────────────────────────
INNER_ORIGIN = "https://inner.dcs.cloud"
IFRAME_SRC_JS = "(document.querySelector('iframe[src*=\"inner.dcs.cloud/notebook/st/\"]')||{}).src||''"
TASK_BASE_RE = re.compile(r"^https://inner\.dcs\.cloud(/notebook/st/[A-Za-z0-9_.-]+/)")
MAX_EXPR_CHARS = 24000   # Windows argv ceiling is 32767; leave room for flags/paths
MAX_CHUNK_CHARS = 16000


def _browser_state_path():
    target = canonical_workspace_url(WORKSPACE_URL) if WORKSPACE_URL else ""
    suffix = "-" + hashlib.sha256(target.encode("utf-8")).hexdigest()[:16] if target else ""
    return os.path.join(os.path.dirname(config_path()), "browser" + suffix + ".json")


def _browser_state():
    state = _read_json(_browser_state_path())
    if not state and WORKSPACE_URL:
        legacy = _read_json(os.path.join(os.path.dirname(config_path()), "browser.json"))
        if (legacy.get("project_id"), legacy.get("workspace_id")) == _workspace_ids(WORKSPACE_URL):
            state = legacy
    return state if isinstance(state, dict) else {}


def _save_browser_state(**updates):
    state = _browser_state()
    state.update(updates)
    path = _browser_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + "." + uuid.uuid4().hex + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def bsk(*args, timeout=120, session=None, tab_id=None):
    """Run one `bsk` CLI call and return its JSON payload."""
    if not os.path.exists(BSK_BIN):
        raise Bridge(f"bsk_missing: {BSK_BIN} — 需先安装 BrowserSkill CLI")
    cmd = [BSK_BIN, *args, "--json"]
    if session:
        cmd += ["--session", session]
    if tab_id is not None:
        cmd += ["--tab-id", str(tab_id)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise Bridge(f"bsk_timeout({args[0]}): 浏览器调用 {timeout}s 未返回") from exc
    except OSError as exc:
        raise Bridge(f"bsk_unavailable: {exc}") from exc
    raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not raw:
        raise Bridge(f"bsk_empty_output({args[0]}): exit {proc.returncode}")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise Bridge(f"bsk_bad_output({args[0]}): {raw[:200]}") from exc
    if isinstance(payload, dict) and isinstance(payload.get("code"), str) and payload.get("exit_code"):
        message = str(payload.get("message") or "").strip()
        raise Bridge(f"bsk_{payload['code']}({args[0]}): {message}".rstrip(": "))
    if proc.returncode != 0:
        raise Bridge(f"bsk_failed({args[0]}): exit {proc.returncode}")
    return payload


def _session_ids():
    try:
        listed = bsk("session", "list", timeout=30)
    except Bridge:
        return set()
    if not isinstance(listed, list):
        return set()
    return {item.get("session_id") for item in listed if isinstance(item, dict)}


def ensure_session(force=False):
    """Reuse the recorded bsk session, else start one (bsk reaps idle sessions)."""
    session_id = _browser_state().get("session_id")
    if session_id and not force and session_id in _session_ids():
        return session_id
    started = bsk("session", "start", timeout=120)
    session_id = started.get("session_id") if isinstance(started, dict) else None
    if not session_id:
        raise Bridge("bsk_session_start_failed: 未返回 session_id")
    _save_browser_state(session_id=session_id, tab_id=None, task_base=None)
    return session_id


def _agent_tabs(session_id):
    listed = bsk("tab", "list", session=session_id, timeout=30)
    tabs = listed.get("tabs") if isinstance(listed, dict) else None
    return [t for t in (tabs or []) if isinstance(t, dict) and t.get("scope") == "agent"]


def _eval_js(tab_id, expression, timeout=60, session_id=None):
    """Evaluate one expression in the tab's main frame; returns the JSON value."""
    session_id = session_id or ensure_session()
    budget = min(max(1, timeout), 60)
    for attempt in range(3):
        try:
            reply = bsk("evaluate", expression, "--timeout", f"{budget}s",
                        timeout=budget + 20, session=session_id, tab_id=tab_id)
            break
        except Bridge as exc:
            # CDP reports this when the tab navigated mid-call: the expression never ran,
            # so retrying cannot duplicate a side effect. Not a general timeout retry.
            if "inspected target navigated or closed" not in str(exc).lower() or attempt == 2:
                raise
            time.sleep(1)
    if reply.get("ok") is not True:
        error = reply.get("error") or {}
        text = error.get("text") if isinstance(error, dict) else None
        raise Bridge("iframe_js_exception: " + str(text or "evaluate_failed"))
    return reply.get("value")


def _runtime():
    with open(RUNTIME_JS_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _call_js(op, payload, tab_id, timeout, session_id=None):
    """Run one runtime op; large payloads are transferred in chunks to fit argv limits."""
    call = ("(async () => { return await window.__codexJupyter(%s, %s); })()"
            % (json.dumps(op), json.dumps(payload)))
    expression = _runtime() + "\n;" + call
    if len(expression) <= MAX_EXPR_CHARS:
        return _eval_js(tab_id, expression, timeout, session_id)
    _eval_js(tab_id, _runtime(), timeout, session_id)
    blob = json.dumps(payload)
    buffer = "window.__snPayload_" + uuid.uuid4().hex
    _eval_js(tab_id, buffer + " = ''", timeout, session_id)
    for offset in range(0, len(blob), MAX_CHUNK_CHARS):
        _eval_js(tab_id, buffer + " += " + json.dumps(blob[offset:offset + MAX_CHUNK_CHARS]),
                 timeout, session_id)
    expression = ("(async () => { const p = JSON.parse(%s); delete %s;"
                  " return await window.__codexJupyter(%s, p); })()"
                  % (buffer, buffer, json.dumps(op)))
    return _eval_js(tab_id, expression, timeout, session_id)


def _task_base(iframe_src):
    match = TASK_BASE_RE.match(str(iframe_src or ""))
    return match.group(1) if match else None


def _probe_tab(tab_id, lite=True):
    """Read-only readiness probe. `lite` skips root/work/session enumeration."""
    result = _call_js("probe", {"lite": bool(lite)}, tab_id, 30) or {}
    data = result.get("data") or {}
    statuses = [data.get(key) for key in ("status_code", "root_status", "work_status")]
    if any(status in (401, 403) for status in statuses):
        raise Bridge("jupyter_auth_required: Jupyter 登录或访问权限失效，请在 Edge 中检查登录状态")
    return bool(result.get("ok")) and data.get("status_code") == 200


def ensure_tab(force=False, full_probe=False, settle=60):
    """Return the Agent Window tab running the authorized Jupyter workspace."""
    if not WORKSPACE_URL:
        if WORKSPACE_URL_ERROR:
            raise Bridge(f"invalid_workspace_url: {WORKSPACE_URL_ERROR}")
        raise Bridge("workspace_not_configured: 首次连接请提供目标工作区 URL 并使用 connect --url")
    target_url = canonical_workspace_url(WORKSPACE_URL)
    project_id, workspace_id = _workspace_ids(target_url)
    session_id = ensure_session()
    state = _browser_state()
    task_base = state.get("task_base")
    tabs = _agent_tabs(session_id)

    if (not force and task_base and state.get("project_id") == project_id
            and state.get("workspace_id") == workspace_id):
        match = next((t for t in tabs
                      if str(t.get("url", "")).startswith(INNER_ORIGIN + task_base)), None)
        if match and match.get("tab_id") is not None:
            tab_id = match["tab_id"]
            try:
                if _probe_tab(tab_id, lite=not full_probe):
                    _save_browser_state(tab_id=tab_id)
                    return tab_id
            except Bridge as exc:
                if str(exc).startswith("jupyter_auth_required:"):
                    raise
                # Tab exists but is not answering; fall through and reload the workspace.

    tab_id = tabs[0].get("tab_id") if tabs else None
    nav = bsk("navigate", target_url, session=session_id, timeout=120, tab_id=tab_id)
    tab_id = nav.get("tab_id") if nav.get("tab_id") is not None else tab_id
    if tab_id is None:
        raise Bridge("tab_unavailable: bsk 未返回标签页")
    final_url = str(nav.get("final_url") or nav.get("url") or "")
    if re.search(r"(?:#|/)login(?:[/?#]|$)", final_url, re.I):
        raise Bridge("dcs_login_required: DCS 已进入登录页，请在原 Edge profile 中登录")

    deadline = time.monotonic() + settle
    iframe_src = ""
    while time.monotonic() < deadline:
        try:
            iframe_src = str(_eval_js(tab_id, IFRAME_SRC_JS, 30) or "")
        except Bridge:
            iframe_src = ""
        if iframe_src:
            break
        time.sleep(2)
    task_base = _task_base(iframe_src)
    if not task_base:
        raise Bridge("workspace_startup_timeout: 工作区 iframe 在等待时间内未就绪；"
                     "请检查 DCS 工作区是否已启动")

    bsk("navigate", str(iframe_src).split("?", 1)[0], session=session_id, timeout=120, tab_id=tab_id)
    probe_deadline = time.monotonic() + 45
    while True:
        try:
            if _probe_tab(tab_id, lite=not full_probe):
                break
        except Bridge as exc:
            if str(exc).startswith("jupyter_auth_required:"):
                raise
        if time.monotonic() >= probe_deadline:
            raise Bridge("jupyter_unavailable: 工作区 Jupyter 未就绪（api/status 非 200）")
        time.sleep(2)
    _save_browser_state(session_id=session_id, tab_id=tab_id, task_base=task_base,
                        project_id=project_id, workspace_id=workspace_id)
    return tab_id


def evaluate_op(op, payload, timeout=60):
    tab_id = ensure_tab()
    value = _call_js(op, payload, tab_id, timeout)
    if (op == "read" and isinstance(value, dict) and value.get("error") == "too_large"
            and (value.get("data") or {}).get("error") == "too_large"):
        return value
    if not isinstance(value, dict) or not value.get("ok", False):
        detail = (value or {}).get("error") if isinstance(value, dict) else "operation_failed"
        raise Bridge(f"jupyter_operation_failed({op}): "
                     f"{json.dumps(detail, ensure_ascii=False)[:300]}")
    return value


# ─────────────────────────── shell helper (kernel + subprocess) ───────────────────────────
_SHELL_TMPL = """import base64, json, os, subprocess, tempfile
MAX_BYTES = {max_bytes}
def _tail_bytes(fh, limit):
    size = fh.tell()
    take = min(size, limit)
    fh.seek(max(0, size - take))
    data = fh.read(take)
    return data.decode('utf-8', errors='replace'), size > limit
with tempfile.TemporaryFile() as _out, tempfile.TemporaryFile() as _err:
    try:
        _p = subprocess.Popen({cmd!r}, shell=True, stdout=_out, stderr=_err,
                              executable='/bin/bash', start_new_session=True)
        try:
            _rc = _p.wait(timeout={timeout})
            _timed = False
        except subprocess.TimeoutExpired:
            _timed = True
            try: os.killpg(_p.pid, 15)
            except Exception: _p.terminate()
            try: _rc = _p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(_p.pid, 9)
                except Exception: _p.kill()
                _rc = _p.wait()
        _stdout, _stdout_truncated = _tail_bytes(_out, MAX_BYTES)
        _stderr, _stderr_truncated = _tail_bytes(_err, MAX_BYTES)
        _record = {{'returncode': None if _timed else _rc, 'stdout': _stdout, 'stderr': _stderr,
                    'stdout_truncated': _stdout_truncated, 'stderr_truncated': _stderr_truncated,
                    'timedOut': _timed}}
    except Exception as _exc:
        _record = {{'returncode': None, 'stdout': '', 'stderr': str(_exc),
                    'stdout_truncated': False, 'stderr_truncated': False, 'timedOut': False}}
_encoded = base64.b64encode(json.dumps(_record).encode('utf-8')).decode('ascii')
print({marker!r} + _encoded)
"""


def _between(t, a, b):
    i, j = t.find(a), t.find(b)
    return t[i + len(a):j] if (i >= 0 and j >= i) else ""


def run_shell(cmd, cwd=None, timeout=MAX_SYNC_SECONDS, max_bytes=MAX_SHELL_BYTES):
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_INTERNAL_SHELL_BYTES:
        raise ValueError("invalid_shell_output_limit")
    validate_text_payload(cmd, MAX_COMMAND_BYTES, "command_too_large")
    timeout = validate_sync_timeout(timeout)
    cwd = cwd or WORK_ROOT
    full = "cd " + shlex.quote(cwd) + " && " + cmd if cwd else cmd
    marker = f"__SN_SHELL_{uuid.uuid4().hex}__:"
    code = _SHELL_TMPL.format(cmd=full, timeout=timeout, marker=marker, max_bytes=max_bytes)
    res = evaluate_op("run_python", {"code": code, "timeout_ms": (timeout + 5) * 1000,
                                     "stream_limit_chars": MAX_KERNEL_STREAM_CHARS},
                      timeout=timeout + 10)
    raw = (res.get("data", {}) or {}).get("stdout", "") or ""
    line = next((item for item in raw.splitlines() if item.startswith(marker)), None)
    if line is None:
        return {"ok": False, "returncode": None, "stdout": "",
                "stderr": "shell_protocol_error: missing result frame", "timedOut": False,
                "stdout_truncated": False, "stderr_truncated": False}
    try:
        decoded = base64.b64decode(line[len(marker):], validate=True)
        record = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        return {"ok": False, "returncode": None, "stdout": "",
                "stderr": "shell_protocol_error: invalid result frame", "timedOut": False,
                "stdout_truncated": False, "stderr_truncated": False}
    rc = record.get("returncode")
    timed_out = record.get("timedOut") is True
    if not timed_out and (isinstance(rc, bool) or not isinstance(rc, int)):
        return {"ok": False, "returncode": None, "stdout": "",
                "stderr": "shell_protocol_error: invalid returncode", "timedOut": False,
                "stdout_truncated": False, "stderr_truncated": False}
    return {"ok": (not timed_out and rc == 0), "returncode": rc,
            "stdout": str(record.get("stdout") or ""), "stderr": str(record.get("stderr") or ""),
            "stdout_truncated": record.get("stdout_truncated") is True,
            "stderr_truncated": record.get("stderr_truncated") is True,
            "timedOut": timed_out}



def run_python(code, timeout=MAX_SYNC_SECONDS):
    validate_text_payload(code, MAX_CODE_BYTES, "python_code_too_large")
    timeout = validate_sync_timeout(timeout)
    return evaluate_op("run_python", {"code": code, "timeout_ms": timeout * 1000}, timeout=timeout + 10)


def write_text(path, content, overwrite=False):
    """Write UTF-8 text under /data/work with atomic no-clobber and readback."""
    validate_text_payload(content, MAX_WRITE_BYTES, "write_content_too_large")
    return _write_bytes(path, content.encode("utf-8"), overwrite, WORK_ROOT)


def _write_bytes(path, raw, overwrite, root):
    """Share exclusive creation and readback between text writes and binary uploads."""
    server_path = to_server(path)
    if not server_path.startswith(root + "/"):
        raise ValueError("write_path_must_be_under_work_root")
    encoded = base64.b64encode(raw).decode("ascii")
    marker = f"__SN_WRITE_{uuid.uuid4().hex}__:"
    code = f'''import base64, json, os, stat
_path = {server_path!r}
_root = {root!r}
_data = base64.b64decode({encoded!r}, validate=True)
_overwrite = {bool(overwrite)!r}
_out = None
try:
    _parent = os.path.dirname(_path)
    os.makedirs(_parent, mode=0o700, exist_ok=True)
    if os.path.commonpath((os.path.realpath(_root), os.path.realpath(_parent))) != os.path.realpath(_root):
        raise ValueError('write_parent_outside_work_root')
    _existed = os.path.lexists(_path)
    if _overwrite and _existed:
        _before = os.lstat(_path)
        if not stat.S_ISREG(_before.st_mode):
            raise ValueError('target_not_regular_file')
    _flags = os.O_WRONLY | os.O_CREAT
    _flags |= os.O_TRUNC if _overwrite else os.O_EXCL
    if hasattr(os, 'O_NOFOLLOW'): _flags |= os.O_NOFOLLOW
    if hasattr(os, 'O_CLOEXEC'): _flags |= os.O_CLOEXEC
    _fd = os.open(_path, _flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(_fd).st_mode):
            raise ValueError('target_not_regular_file')
        os.fchmod(_fd, 0o600)
        _offset = 0
        while _offset < len(_data):
            _offset += os.write(_fd, _data[_offset:])
        os.fsync(_fd)
    finally:
        os.close(_fd)
    _read_flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'): _read_flags |= os.O_NOFOLLOW
    _fd = os.open(_path, _read_flags)
    try:
        if not stat.S_ISREG(os.fstat(_fd).st_mode):
            raise ValueError('target_not_regular_file')
        _readback = b''
        while len(_readback) <= len(_data):
            _chunk = os.read(_fd, min(1048576, len(_data) + 1 - len(_readback)))
            if not _chunk: break
            _readback += _chunk
    finally:
        os.close(_fd)
    if _readback != _data:
        raise ValueError('write_readback_mismatch')
    _out = {{'ok': True, 'path': _path, 'bytes': len(_data),
             'overwritten': bool(_overwrite and _existed)}}
except FileExistsError:
    _out = {{'ok': False, 'error': 'file_exists_use_overwrite', 'path': _path}}
except Exception as _exc:
    _out = {{'ok': False, 'error': str(_exc)[:256], 'path': _path}}
print({marker!r} + base64.b64encode(json.dumps(_out, sort_keys=True).encode('utf-8')).decode('ascii'))
'''
    result = evaluate_op("run_python", {"code": code, "timeout_ms": MAX_SYNC_SECONDS * 1000},
                         timeout=60)
    stdout = str((result.get("data") or {}).get("stdout") or "")
    line = next((item for item in stdout.splitlines() if item.startswith(marker)), None)
    if line is None:
        raise Bridge("jupyter_operation_failed(write): write_protocol_error")
    try:
        payload = json.loads(base64.b64decode(line[len(marker):], validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise Bridge("jupyter_operation_failed(write): invalid_write_result") from exc
    if payload.get("ok") is not True:
        raise Bridge(f"jupyter_operation_failed(write): {json.dumps(payload.get('error'), ensure_ascii=False)}")
    return {"ok": True, "data": payload, "error": None}


# ─────────────────────────── binary transfer ───────────────────────────
TRANSFER_CHUNK = 262144          # measured: 256 KiB keeps each browser round trip ~0.1 s
MAX_UPLOAD_BYTES = 4 * 1024 * 1024


def _transfer_path(remote):
    server_path = to_server(remote)
    contents = contents_path(remote)
    if contents is None:
        raise ValueError("transfer_path_must_be_under_the_jupyter_contents_root")
    return server_path, contents


def _remote_stat(contents, session_id=None, tab_id=None):
    value = _call_js("stat", {"path": contents}, tab_id or ensure_tab(), 30, session_id) or {}
    data = value.get("data") or {}
    if value.get("ok") is not True:
        raise Bridge(f"remote_stat_failed: {data.get('error') or 'not_found'}")
    if data.get("type") != "file":
        raise Bridge("remote_path_is_not_a_file")
    size = data.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise Bridge("remote_size_unknown")
    return {"size": size, "last_modified": data.get("last_modified")}


def _remote_sha256(server_path, size):
    """Server-side checksum for files small enough to hash without rereading data twice."""
    if size > MAX_ARTIFACT_HASH_BYTES:
        return None
    res = run_shell("sha256sum -- " + shlex.quote(server_path), cwd=WORK_ROOT,
                    timeout=MAX_SYNC_SECONDS)
    if res.get("ok") is not True:
        return None
    parts = (res.get("stdout") or "").split()
    return parts[0] if parts else None


def get_file(remote, out, overwrite=False, verify=True):
    """Download one Contents-root file in bounded ranges, resumably and without a kernel."""
    server_path, contents = _transfer_path(remote)
    tab = ensure_tab()
    session_id = ensure_session()
    meta = _remote_stat(contents, session_id, tab)
    out = os.path.abspath(out)
    part = out + ".part"
    sidecar = out + ".part.json"
    if os.path.exists(out) and not overwrite:
        raise ValueError("local_file_exists_use_overwrite")
    project_id, workspace_id = _workspace_ids(WORKSPACE_URL)
    record = {"project_id": project_id, "workspace_id": workspace_id,
              "remote": server_path, "size": meta["size"],
              "last_modified": meta["last_modified"]}
    resumed_from = 0
    resumable = os.path.exists(part) and _read_json(sidecar) == record
    if not resumable:
        for stale in (part, sidecar):
            try:
                os.remove(stale)
            except OSError:
                pass
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(sidecar, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        with open(part, "xb"):
            pass
    else:
        resumed_from = os.path.getsize(part)
        if resumed_from > meta["size"]:
            raise Bridge("partial_file_larger_than_source")

    digest = hashlib.sha256()
    if resumed_from:
        with open(part, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
    position = resumed_from
    while position < meta["size"]:
        end = min(position + TRANSFER_CHUNK, meta["size"]) - 1
        chunk = _call_js("fetch_range", {"path": contents, "start": position, "end": end},
                         tab, 60, session_id) or {}
        data = chunk.get("data") or {}
        if chunk.get("ok") is not True:
            raise Bridge(f"transfer_chunk_failed: {data.get('status')} {data.get('error')}")
        try:
            payload = base64.b64decode(data.get("b64") or "", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Bridge("transfer_chunk_decode_failed") from exc
        if len(payload) != end - position + 1:
            raise Bridge(f"transfer_chunk_size_mismatch: expected {end - position + 1}, "
                         f"got {len(payload)}")
        with open(part, "ab") as fh:
            fh.write(payload)
        digest.update(payload)
        position += len(payload)

    if _remote_stat(contents, session_id, tab) != meta:
        for stale in (part, sidecar):
            try:
                os.remove(stale)
            except OSError:
                pass
        raise Bridge("source_changed_during_transfer")
    if os.path.getsize(part) != meta["size"]:
        raise Bridge("transfer_size_mismatch")
    local_sha = digest.hexdigest()
    remote_sha = _remote_sha256(server_path, meta["size"]) if verify else None
    if remote_sha and remote_sha != local_sha:
        raise Bridge("transfer_checksum_mismatch")
    if overwrite:
        os.replace(part, out)
    else:
        try:
            os.link(part, out)
        except FileExistsError as exc:
            raise ValueError("local_file_exists_use_overwrite") from exc
        os.remove(part)
    try:
        os.remove(sidecar)
    except OSError:
        pass
    return {"ok": True, "path": out, "bytes": meta["size"], "sha256": local_sha,
            "resumed_from": resumed_from,
            "verified": "sha256" if remote_sha else "size_only"}


def put_file(local, remote, overwrite=False):
    """Upload one local file under the Contents root; no-clobber unless overwrite is explicit."""
    server_path, contents = _transfer_path(remote)
    try:
        with open(local, "rb") as fh:
            raw = fh.read(MAX_UPLOAD_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"local_file_read_failed: {exc}") from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"upload_too_large: limit {MAX_UPLOAD_BYTES} bytes")
    _write_bytes(server_path, raw, overwrite, "/data")
    after = _remote_stat(contents)
    if after["size"] != len(raw):
        raise Bridge("upload_size_mismatch")
    return {"ok": True, "path": server_path, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


# ─────────────────────────── path helpers ───────────────────────────
def fix_msys(p):
    """Undo git-bash's rewrite of Unix ``/data/...`` paths."""
    if not p:
        return p
    low = p.replace("\\", "/")
    j = low.find("/data/")
    if j > 0 and "Git" in low[:j]:
        return low[j:]
    return p


def to_contents(path):
    """Map an absolute ``/data/...`` path to the Jupyter Contents root."""
    p = fix_msys(path).replace("\\", "/").lstrip("/")
    if p == "data":
        return ""
    if p.startswith("data/"):
        return p[len("data/"):]
    return p


def contents_path(path):
    """Return the Jupyter Contents path, or None for an external Linux mount."""
    p = fix_msys(path).replace("\\", "/")
    if p == "/data" or p.startswith("/data/"):
        return to_contents(p)
    if p.startswith("/"):
        return None
    return to_contents(p)


def to_server(path):
    """Normalize a Linux server path without imposing a read allowlist."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("invalid_inspect_path")
    p = fix_msys(path).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        raise ValueError("inspect_path_not_linux_path")
    if p == "work" or p.startswith("work/"):
        p = INSPECT_ROOT + p[4:]
    elif not p.startswith("/"):
        p = INSPECT_ROOT + "/" + p
    return posixpath.normpath(p)


# ─────────────────────────── async jobs ───────────────────────────

def _mark_setup_error(job_id, reason="runner_not_ready"):
    """Persist a bounded setup failure without following controller symlinks."""
    job_id = validate_job_id(job_id)
    jdir = shlex.quote(f"{JOB_ROOT}/{job_id}")
    qjob = shlex.quote(json.dumps(job_id))
    qreason = shlex.quote(json.dumps(str(reason)[:MAX_INSPECT_VALUE_CHARS]))
    command_text = f"""if [ -d {shlex.quote(JOB_ROOT)} ] && [ ! -L {shlex.quote(JOB_ROOT)} ] && [ -d {jdir} ] && [ ! -L {jdir} ]; then
ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '{{\"state\":\"setup_error\",\"job_id\":%s,\"error\":%s,\"ended_at\":\"%s\"}}\\n' {qjob} {qreason} "$ended_at" > {jdir}/status.json.tmp.$$
mv {jdir}/status.json.tmp.$$ {jdir}/status.json
chmod 600 {jdir}/status.json
else
exit 1
fi"""
    return run_shell(command_text, cwd=WORK_ROOT, timeout=30)


def _wait_for_runner_ready(job_id, timeout):
    """Require a runner-written running/finished status within a bounded interval."""
    if timeout < 0:
        raise ValueError("invalid_readiness_timeout")
    deadline = time.monotonic() + timeout
    last = None
    while True:
        last = poll(job_id, tail=1)
        state = (last.get("status") or {}).get("state")
        if state in ("running", "finished"):
            return last
        if state == "setup_error":
            raise Bridge("runner_not_ready: setup_error")
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    _mark_setup_error(job_id)
    raise Bridge(f"runner_not_ready: {json.dumps(last, ensure_ascii=False)[:300]}")


def submit(cmd, name=None, cwd=None, timeout=MAX_SYNC_SECONDS, readiness_timeout=5.0,
           idempotency_key=None):
    """Submit a raw shell command; its payload is intentionally not OS-sandboxed.

    Controller-created state is restricted to ``/data/work/agent_jobs``. The
    payload starts in ``cwd`` (default ``/data/work``), but ``cmd`` can still write
    wherever its server-side permissions allow.
    """
    jid = make_job_id(name, idempotency_key)
    job_dir = f"{JOB_ROOT}/{jid}"
    execution_cwd = to_server(cwd or WORK_ROOT)
    project_id, workspace_id = _workspace_ids(WORKSPACE_URL)
    command_sha = hashlib.sha256(cmd.encode("utf-8")).hexdigest()
    identity = {"project_id": project_id, "workspace_id": workspace_id,
                "cwd": execution_cwd, "command_sha256": command_sha}
    launcher = build_job_launcher(jid, cmd, cwd=cwd)
    try:
        _write_receipt(jid, "pending", exclusive=True, **identity)
    except FileExistsError:
        previous = _read_receipt(jid) or {}
        if any(previous.get(key) != value for key, value in identity.items()):
            raise ValueError("idempotency_conflict: workspace, cwd or command differs from receipt")
        return {"job_id": jid, "dir": job_dir, "cwd": execution_cwd, "reused": True,
                "state": previous.get("state"),
                "note": "idempotency key 已用过；未再次提交，请用 poll 查看该 job 的状态"}
    try:
        res = run_shell(launcher, cwd=WORK_ROOT, timeout=timeout)
    except Bridge as exc:
        _write_receipt(jid, "unknown", error=str(exc)[:200])
        raise Bridge(f"submit_unknown: job_id={jid} — 提交结果未知，不要重复提交；"
                     f"先运行 poll {jid} 确认") from exc
    if res.get("ok") is not True or res.get("returncode") != 0:
        detail = res.get("stderr") or res.get("stdout") or "launcher failed"
        _write_receipt(jid, "setup_failed", error=str(detail)[:200])
        raise Bridge(f"job_setup_failed: job_id={jid} — {detail}")
    acknowledgement = f"__SN_SUBMITTED__={jid}"
    if acknowledgement not in (res.get("stdout") or "").splitlines():
        _write_receipt(jid, "unknown", error="launcher_ack_missing")
        raise Bridge(f"submit_unknown: job_id={jid} — launcher 未回执，不要重复提交；"
                     f"先运行 poll {jid} 确认")
    try:
        _wait_for_runner_ready(jid, readiness_timeout)
    except Bridge as exc:
        _write_receipt(jid, "unknown", error=str(exc)[:200])
        raise Bridge(f"submit_pending: job_id={jid} — {exc}；先运行 poll {jid} 确认") from exc
    _write_receipt(jid, "submitted", dir=job_dir, launched_at=_utc_now())
    return {"job_id": jid, "dir": job_dir, "cwd": execution_cwd, "reused": False,
            "launch_stdout": res.get("stdout"), "launch_stderr": res.get("stderr")}


def _poll_section(raw, frame, section):
    """Decode one base64 poll record; encoded data cannot forge frame boundaries."""
    prefix = f"{frame} {section} "
    line = next((item for item in (raw or "").splitlines() if item.startswith(prefix)), None)
    if line is None:
        return ("protocol_error", None)
    remainder = line[len(prefix):]
    state, separator, encoded = remainder.partition(" ")
    if not separator:
        return ("protocol_error", None)
    if state != "ok":
        return (state, None)
    try:
        value = base64.b64decode(encoded, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ("protocol_error", None)
    return ("ok", value)


def poll(job_id, tail=40):
    """Return durable state and bounded log tails as structured controller data."""
    job_id = validate_job_id(job_id)
    tail = validate_tail(tail)
    jdir = shlex.quote(f"{JOB_ROOT}/{job_id}")
    frame = f"__SN_POLL_{uuid.uuid4().hex}__"
    cmd = f"""frame={shlex.quote(frame)}
jdir={jdir}
emit() {{ printf '%s %s %s %s\\n' "$frame" "$1" "$2" "$3"; }}
emit_live() {{ printf '%s %s %s %s\\n' "$frame" live ok "$(printf '%s' "$1" | base64 | tr -d '\\n')"; }}
if [ -L {shlex.quote(JOB_ROOT)} ] || [ ! -d {shlex.quote(JOB_ROOT)} ]; then
  emit status unsafe_root ''; emit stdout unsafe_root ''; emit stderr unsafe_root ''; emit dir unsafe_root ''; emit_live unknown
elif [ -L "$jdir" ] || [ ! -d "$jdir" ]; then
  emit status missing ''; emit stdout missing ''; emit stderr missing ''; emit dir missing ''; emit_live unknown
else
  emit dir ok ''
  if [ -L "$jdir/pid" ] || [ ! -f "$jdir/pid" ]; then emit_live unknown
  else
    pid=$(head -c 32 "$jdir/pid" | tr -dc '0-9')
    if [ -z "$pid" ]; then emit_live unknown
    elif ! kill -0 "$pid" 2>/dev/null; then emit_live "dead:$pid"
    else
      cmdline=$(tr '\\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
      case "$cmdline" in *"$jdir"*) emit_live "owned:$pid" ;; *) emit_live "foreign:$pid" ;; esac
    fi
  fi
  if [ -f {jdir}/status.json ] && [ ! -L {jdir}/status.json ]; then
    status_bytes=$(head -c {MAX_STATUS_BYTES + 1} {jdir}/status.json | wc -c)
    if [ "$status_bytes" -gt {MAX_STATUS_BYTES} ]; then emit status too_large ''
    else emit status ok "$(head -c {MAX_STATUS_BYTES} {jdir}/status.json | base64 | tr -d '\\n')"; fi
  elif [ -e {jdir}/status.json ] || [ -L {jdir}/status.json ]; then emit status read_error ''
  else emit status missing ''; fi
  if [ -f {jdir}/stdout.log ] && [ ! -L {jdir}/stdout.log ]; then
    emit stdout ok "$(tail -n {tail} {jdir}/stdout.log | tail -c {MAX_LOG_BYTES} | base64 | tr -d '\\n')"
  elif [ -e {jdir}/stdout.log ] || [ -L {jdir}/stdout.log ]; then emit stdout read_error ''
  else emit stdout missing ''; fi
  if [ -f {jdir}/stderr.log ] && [ ! -L {jdir}/stderr.log ]; then
    emit stderr ok "$(tail -n {tail} {jdir}/stderr.log | tail -c {MAX_LOG_BYTES} | base64 | tr -d '\\n')"
  elif [ -e {jdir}/stderr.log ] || [ -L {jdir}/stderr.log ]; then emit stderr read_error ''
  else emit stderr missing ''; fi
fi"""
    res = run_shell(cmd, cwd=WORK_ROOT, timeout=MAX_SYNC_SECONDS, max_bytes=MAX_INTERNAL_SHELL_BYTES)
    raw = res.get("stdout") or ""
    records = {key: _poll_section(raw, frame, key)
               for key in ("status", "stdout", "stderr", "dir", "live")}
    missing = {key: records[key][0] == "missing" for key in records}
    job_present = records["dir"][0] == "ok"
    live_state, live_value = records["live"]
    process, _, pid_text = (live_value or "").partition(":")
    pid = int(pid_text) if pid_text.isdigit() else None
    if live_state != "ok":
        process = "unknown"
    errors = {}
    status = None
    status_state, status_raw = records["status"]
    if status_state == "ok":
        try:
            status = json.loads(status_raw)
        except (TypeError, ValueError):
            errors["status"] = "status_parse_error"
    elif status_state != "missing":
        errors["status"] = f"status_{status_state}"
    tails = {}
    for key in ("stdout", "stderr"):
        state, value = records[key]
        tails[key] = value if state == "ok" else ""
        if state not in ("ok", "missing"):
            errors[key] = f"{key}_{state}"
    if res.get("ok") is not True or res.get("returncode") != 0:
        errors["poll"] = res.get("stderr") or "poll_shell_failed"
    receipt = _read_receipt(job_id)
    local_state = (receipt or {}).get("state")
    if status is not None:
        outcome = status.get("state") or "unknown"
    elif job_present:
        outcome = "starting"
    elif local_state in ("pending", "setup_failed"):
        outcome = "not_dispatched"
    else:
        outcome = "unknown"
    if outcome == "running":
        # A status file alone never proves the job is alive.
        outcome = {"owned": "running", "dead": "stale_running",
                   "foreign": "foreign_pid"}.get(process, "running_unverified")
    return {"job_id": job_id, "tail": tail, "status": status,
            "stdout_tail": tails["stdout"], "stderr_tail": tails["stderr"],
            "missing": missing, "read_errors": errors,
            "job_dir_present": job_present, "local_state": local_state, "outcome": outcome,
            "process": process, "pid": pid}


_REMOTE_JOBS = r'''
import json, os
root = %(root)r
limit = %(limit)d


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


out = []
try:
    entries = [entry for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
except OSError as exc:
    print("JOBS_JSON=" + json.dumps({"error": str(exc)[:200]}))
else:
    entries.sort(key=lambda entry: entry.stat(follow_symlinks=False).st_mtime, reverse=True)
    for entry in entries[:limit]:
        job_dir = entry.path
        record = {"job_id": entry.name, "dir": job_dir}
        status = read_json(os.path.join(job_dir, "status.json")) or {}
        record["state"] = status.get("state") or "no_status"
        for key in ("started_at", "ended_at", "exit_code"):
            if key in status:
                record[key] = status.get(key)
        manifest = read_json(os.path.join(job_dir, "manifest.json")) or {}
        if manifest.get("cwd"):
            record["cwd"] = manifest["cwd"]
        pid = None
        try:
            with open(os.path.join(job_dir, "pid"), encoding="utf-8") as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            pid = None
        record["pid"] = pid
        if pid is None:
            record["pid_alive"] = None
            record["owned"] = None
        else:
            record["pid_alive"] = alive(pid)
            try:
                with open("/proc/%%d/cmdline" %% pid, "rb") as fh:
                    cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
            except OSError:
                cmdline = ""
            record["owned"] = job_dir in cmdline
        if record["state"] == "running" and record["pid_alive"] is False:
            record["state"] = "stale_running"
        elif record["state"] == "running" and record["owned"] is False:
            record["state"] = "foreign_pid"
        stage = read_json(os.path.join(job_dir, "stage.json"))
        if stage is not None:
            record["stage"] = stage
        artifacts = {"count": 0, "bytes": 0}
        try:
            for item in os.scandir(os.path.join(job_dir, "artifacts")):
                if item.is_file(follow_symlinks=False):
                    artifacts["count"] += 1
                    artifacts["bytes"] += item.stat(follow_symlinks=False).st_size
                if artifacts["count"] >= 200:
                    break
        except OSError:
            pass
        record["artifacts"] = artifacts
        out.append(record)
    print("JOBS_JSON=" + json.dumps({"jobs": out}))
'''


def list_jobs(limit=20, remote=False):
    """Job registry: server job directories merged with local receipts and acceptance."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("invalid_limit")
    receipts = {item.get("job_id"): item for item in list_receipts(200) if item.get("job_id")}
    jobs = []
    if remote:
        response = run_python(_REMOTE_JOBS % {"root": JOB_ROOT, "limit": limit},
                              timeout=MAX_SYNC_SECONDS)
        listing = _pull_json((response.get("data") or {}).get("stdout"), marker="JOBS_JSON=") or {}
        for item in listing.get("jobs", []) or []:
            merged = dict(item)
            local = receipts.pop(item.get("job_id"), None)
            if local:
                merged["local_state"] = local.get("state")
                merged["accepted_at"] = local.get("accepted_at")
                merged["acceptance_note"] = local.get("acceptance_note")
                merged.setdefault("cwd", local.get("cwd"))
            jobs.append(merged)
        if listing.get("error"):
            jobs.append({"job_id": None, "state": "list_error", "error": listing["error"]})
    for job_id, local in receipts.items():
        jobs.append({"job_id": job_id, "state": "local_only", "cwd": local.get("cwd"),
                     "local_state": local.get("state"),
                     "accepted_at": local.get("accepted_at"),
                     "acceptance_note": local.get("acceptance_note")})
    jobs.sort(key=lambda item: item.get("started_at") or item.get("accepted_at") or "",
              reverse=True)
    return {"ok": True, "count": len(jobs), "jobs": jobs[:limit]}


_CANCEL_JOB = r'''
jdir=%(jdir)s
root=%(root)s
if [ -L "$root" ] || [ ! -d "$root" ]; then echo missing_root; exit 10; fi
if [ -L "$jdir" ] || [ ! -d "$jdir" ]; then echo missing_job; exit 11; fi
if [ -L "$jdir/pid" ] || [ ! -f "$jdir/pid" ]; then echo missing_pid; exit 12; fi
pid=$(head -c 32 "$jdir/pid" | tr -dc '0-9')
[ -n "$pid" ] || { echo invalid_pid; exit 13; }
kill -0 "$pid" 2>/dev/null || { echo not_running; exit 14; }
cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
case "$cmdline" in
  *"$jdir"*) ;;
  *) echo pid_not_owned; exit 15 ;;
esac
kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || { echo kill_failed; exit 16; }
echo "terminated:$pid"
'''


def cancel_job(job_id):
    """Cancel only a job whose PID still belongs to that job's controller directory."""
    job_id = validate_job_id(job_id)
    cmd = _CANCEL_JOB % {"jdir": shlex.quote(f"{JOB_ROOT}/{job_id}"),
                         "root": shlex.quote(JOB_ROOT)}
    res = run_shell(cmd, cwd=WORK_ROOT, timeout=MAX_SYNC_SECONDS)
    out = str(res.get("stdout") or "").strip()
    if res.get("returncode") == 0 and out.startswith("terminated:"):
        return {"ok": True, "job_id": job_id, "signal": "SIGTERM",
                "pid": int(out.split(":", 1)[1])}
    return {"ok": False, "job_id": job_id, "error": out or "cancel_failed"}


def accept_job(job_id, note=None):
    """Record that a human accepted the result of this job (separate from process state)."""
    job_id = validate_job_id(job_id)
    receipt = _read_receipt(job_id) or {}
    record = _write_receipt(job_id, receipt.get("state") or "remote",
                            accepted_at=_utc_now(),
                            acceptance_note=str(note or "")[:500])
    return {"ok": True, "job_id": job_id, "accepted_at": record["accepted_at"],
            "acceptance_note": record.get("acceptance_note", "")}


_ARTIFACT_INVENTORY = r'''
import hashlib, json, os, stat
MAX_RESULT_BYTES = __MAX_RESULT_BYTES__
MAX_HASH_BYTES = __MAX_HASH_BYTES__
root = %(root)r
records = []
job_dir = os.path.dirname(root)
controller_root = os.path.dirname(job_dir)
safe_root = (os.path.isdir(controller_root) and not os.path.islink(controller_root) and
             os.path.isdir(job_dir) and not os.path.islink(job_dir) and
             os.path.isdir(root) and not os.path.islink(root) and
             os.path.commonpath([os.path.realpath(controller_root), os.path.realpath(root)]) ==
             os.path.realpath(controller_root))
if safe_root:
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for name in names:
            path = os.path.join(base, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            try:
                flags = os.O_RDONLY
                if hasattr(os, 'O_NOFOLLOW'):
                    flags |= os.O_NOFOLLOW
                fd = os.open(path, flags)
                try:
                    opened = os.fstat(fd)
                    if (not stat.S_ISREG(opened.st_mode) or
                            (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                        continue
                    digest = None
                    if info.st_size <= MAX_HASH_BYTES:
                        digest = hashlib.sha256()
                        with os.fdopen(fd, 'rb') as fh:
                            fd = None
                            for block in iter(lambda: fh.read(1024 * 1024), b''):
                                digest.update(block)
                    else:
                        os.close(fd)
                        fd = None
                finally:
                    if fd is not None:
                        os.close(fd)
            except OSError:
                continue
            records.append({'path': os.path.relpath(path, root), 'size': info.st_size,
                            'mtime_ns': info.st_mtime_ns,
                            'sha256': digest.hexdigest() if digest is not None else None,
                            'checksum': 'sha256' if digest is not None else 'skipped_large_file'})
records.sort(key=lambda record: record['path'])
summary = {'files': records} if safe_root else {'error': 'unsafe_artifact_root', 'files': []}
payload = json.dumps(summary, sort_keys=True, separators=(',', ':'))
if len(payload.encode('utf-8')) > MAX_RESULT_BYTES:
    payload = json.dumps({'error': 'bounded_summary_too_large',
                          'limit_bytes': MAX_RESULT_BYTES}, separators=(',', ':'))
print('ARTIFACTS_JSON=' + payload)
'''.replace("__MAX_RESULT_BYTES__", str(MAX_INSPECT_RESULT_BYTES)).replace("__MAX_HASH_BYTES__", str(MAX_ARTIFACT_HASH_BYTES))


def artifacts(job_id, timeout=MAX_SYNC_SECONDS):
    """Inventory only regular files under a job's dedicated artifacts directory."""
    job_id = validate_job_id(job_id)
    root = f"{JOB_ROOT}/{job_id}/artifacts"
    response = run_python(_ARTIFACT_INVENTORY % {"root": root}, timeout=timeout)
    data = response.get("data", {}) or {}
    inventory = _pull_json(data.get("stdout"), marker="ARTIFACTS_JSON=")
    if inventory is None:
        return {"job_id": job_id, "error": "artifact_inventory_failed",
                "stderr": data.get("stderr"), "raw": data.get("stdout")}
    if inventory.get("error"):
        return {"job_id": job_id,
                **{key: value for key, value in inventory.items() if key != "files"}}
    return {"job_id": job_id, "files": inventory.get("files", [])}


# ─────────────────────────── bioinformatics inspect ───────────────────────────
_INSPECT_PRELUDE = r'''
import itertools, json, os
MAX_ITEMS = 100
MAX_VALUE = 256
MAX_CELLS = 500
MAX_RESULT_BYTES = __MAX_RESULT_BYTES__
def safe_path(value):
    real = os.path.realpath(value)
    if not os.path.isfile(real):
        raise ValueError("inspect_path_not_regular_file")
    return real
def clip(value):
    text = str(value)
    return text[:MAX_VALUE]
def clipped(values, limit=MAX_ITEMS):
    return [clip(value) for value in itertools.islice(values, limit)]
def emit_summary(value):
    payload = json.dumps(value, ensure_ascii=False, default=clip, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_RESULT_BYTES:
        payload = json.dumps({"error":"bounded_summary_too_large",
                              "limit_bytes":MAX_RESULT_BYTES}, separators=(",", ":"))
    print("INSPECT_JSON=" + payload)
'''.replace("__MAX_RESULT_BYTES__", str(MAX_INSPECT_RESULT_BYTES))

H5AD = _INSPECT_PRELUDE + r'''
import anndata as ad, json
p = safe_path(%(p)r)
a = ad.read_h5ad(p, backed="r")
def cols(df):
    return {clip(c): clip(df[c].dtype) for c in itertools.islice(df.columns, MAX_ITEMS)}
out = {"format":"h5ad","path":p,"shape":[int(a.shape[0]),int(a.shape[1])],
 "obs_columns":cols(a.obs),"var_columns":cols(a.var),
 "obsm":clipped(a.obsm.keys()),"varm":clipped(a.varm.keys()),
 "layers":clipped(a.layers.keys()),"uns":clipped(a.uns.keys()),
 "obs_head":clipped(a.obs_names, 5),"var_head":clipped(a.var_names, 5)}
cats={}; cells=0
for c in itertools.islice(a.obs.columns, MAX_ITEMS):
    try:
        s=a.obs[c]
        if str(s.dtype)=="category" or s.dtype==object:
            values=itertools.islice(s.astype("category").cat.categories, 40)
            room=max(0, MAX_CELLS-cells)
            if room:
                cats[clip(c)]=clipped(values, min(40, room)); cells += len(cats[clip(c)])
    except Exception: pass
out["obs_categories"]=cats
emit_summary(out)
'''

TABLE = _INSPECT_PRELUDE + r'''
import pandas as pd, json
p=safe_path(%(p)r); sep=%(sep)r
df=pd.read_csv(p, sep=sep, nrows=1000)
try:
    with open(p, "rb") as fh:
        total=sum(block.count(b"\n") for block in iter(lambda: fh.read(1048576), b""))
except Exception: total=None
selected=list(itertools.islice(df.columns, MAX_ITEMS))
rows=[]
for record in df.loc[:, selected].head(5).to_dict(orient="records"):
    rows.append({clip(k): clip(v) for k, v in itertools.islice(record.items(), MAX_ITEMS)})
out={"format":"table","path":p,"sampled_rows":int(len(df)),"total_lines":total,
 "n_columns":int(df.shape[1]),"columns":{clip(c):clip(df[c].dtype) for c in selected},
 "head":rows[:MAX_CELLS]}
emit_summary(out)
'''

PARQUET = _INSPECT_PRELUDE + r'''
import pyarrow.parquet as pq, json
p=safe_path(%(p)r); f=pq.ParquetFile(p); s=f.schema_arrow
out={"format":"parquet","path":p,"num_rows":int(f.metadata.num_rows),
 "num_row_groups":int(f.num_row_groups),
 "columns":{clip(s.field(i).name):clip(s.field(i).type) for i in range(min(len(s),MAX_ITEMS))}}
emit_summary(out)
'''

RDS_R = r'''
MAX_ITEMS <- 100L; MAX_VALUE <- 256L; MAX_RESULT_BYTES <- __MAX_RESULT_BYTES__L
clip <- function(x) substr(as.character(x), 1L, MAX_VALUE)
bounded <- function(x) clip(utils::head(x, MAX_ITEMS))
args <- commandArgs(trailingOnly=TRUE)
f <- normalizePath(args[1], mustWork=TRUE)
if (!file.exists(f) || dir.exists(f)) stop("inspect_path_not_regular_file")
x <- readRDS(f); info <- list(class=bounded(class(x)))
if (inherits(x,"Seurat")) {
  info$type <- "Seurat"; info$n_features <- nrow(x); info$n_cells <- ncol(x)
  info$assays <- bounded(names(x@assays)); info$default_assay <- tryCatch(clip(Seurat::DefaultAssay(x)), error=function(e) NA)
  info$meta_columns <- bounded(colnames(x@meta.data)); info$reductions <- bounded(names(x@reductions))
} else if (inherits(x,"SingleCellExperiment") || inherits(x,"SummarizedExperiment")) {
  info$type <- "SCE/SE"; info$dim <- dim(x)
  info$assayNames <- bounded(tryCatch(SummarizedExperiment::assayNames(x), error=function(e) NULL))
  info$colData <- bounded(tryCatch(colnames(SummarizedExperiment::colData(x)), error=function(e) NULL))
  info$reducedDims <- bounded(tryCatch(SingleCellExperiment::reducedDimNames(x), error=function(e) NULL))
} else if (is.data.frame(x)) {
  info$type <- "data.frame"; info$dim <- dim(x); info$columns <- bounded(colnames(x))
} else if (is.list(x)) {
  info$type <- "list"; info$length <- length(x); info$names <- bounded(names(x))
} else {
  info$type <- "generic"; info$length <- length(x)
}
if (requireNamespace("jsonlite", quietly=TRUE)) {
  payload <- jsonlite::toJSON(info, auto_unbox=TRUE, null="null", force=TRUE)
  if (length(charToRaw(payload)) > MAX_RESULT_BYTES) {
    payload <- jsonlite::toJSON(list(error="bounded_summary_too_large",
      limit_bytes=MAX_RESULT_BYTES), auto_unbox=TRUE)
  }
  cat("INSPECT_JSON=", payload, "\n", sep="")
} else {
  cat("INSPECT_JSON={\"error\":\"jsonlite_required\"}\n")
}
'''.replace("__MAX_RESULT_BYTES__", str(MAX_INSPECT_RESULT_BYTES))


def _pull_json(stdout, marker="INSPECT_JSON="):
    for line in (stdout or "").splitlines():
        if line.startswith(marker):
            payload = line[len(marker):]
            if len(payload.encode("utf-8")) > MAX_INSPECT_RESULT_BYTES:
                return {"error": "bounded_summary_too_large",
                        "limit_bytes": MAX_INSPECT_RESULT_BYTES}
            try:
                return json.loads(payload)
            except (TypeError, ValueError):
                return None
    return None


_SERVER_LISTING = r'''
import itertools, json, os, stat
MAX_ENTRIES = 200
MAX_RESULT_BYTES = __MAX_RESULT_BYTES__
p = os.path.realpath(%(p)r)
try:
    if not os.path.isdir(p): raise ValueError("list_path_not_directory")
    scanned = list(itertools.islice(os.scandir(p), MAX_ENTRIES + 1))
    truncated = len(scanned) > MAX_ENTRIES
    entries = []
    for entry in sorted(scanned[:MAX_ENTRIES], key=lambda item: item.name):
        try:
            info = entry.stat(follow_symlinks=False)
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
            entries.append({"name": entry.name, "type": kind, "size": int(info.st_size)})
        except OSError:
            continue
    payload = {"format": "directory", "path": p, "entries": entries, "truncated": truncated}
except Exception as exc:
    payload = {"format": "directory", "path": p, "error": str(exc)[:256]}
encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
    encoded = json.dumps({"error":"bounded_summary_too_large", "limit_bytes":MAX_RESULT_BYTES}, separators=(",", ":"))
print("LIST_JSON=" + encoded)
'''.replace("__MAX_RESULT_BYTES__", str(MAX_INSPECT_RESULT_BYTES))


def _contents_listing(contents, server_path):
    """List through the Contents API; None means the caller should use the kernel."""
    value = evaluate_op("list", {"path": contents}, timeout=MAX_SYNC_SECONDS)
    data = value.get("data") or {}
    entries = data.get("entries")
    if data.get("ok") is not True or not isinstance(entries, list):
        return None
    return {"format": "directory", "path": server_path, "entries": entries,
            "truncated": bool(data.get("truncated"))}


def list_path(path, timeout=MAX_SYNC_SECONDS, include_hidden=False):
    """List a directory: Contents API when mappable, bounded kernel scan otherwise.

    The Contents view omits dotfiles the way the JupyterLab browser does; `include_hidden`
    forces the kernel scan that shows them.
    """
    server_path = to_server(path)
    contents = None if include_hidden else contents_path(path)
    if contents is not None:
        listing = _contents_listing(contents, server_path)
        if listing is not None:
            return listing
    response = run_python(_SERVER_LISTING % {"p": server_path}, timeout=timeout)
    data = response.get("data", {}) or {}
    listing = _pull_json(data.get("stdout"), marker="LIST_JSON=")
    if listing is None:
        return {"error": "directory_listing_failed", "path": server_path,
                "stderr": str(data.get("stderr") or "")[:MAX_INSPECT_VALUE_CHARS]}
    return listing


def cat_path(path):
    """Always use the bounded server-side text preview; never download full files."""
    return inspect_text(path, max_bytes=DEFAULT_TEXT_PREVIEW_BYTES)


def inspect(path, timeout=300):
    raw = fix_msys(path)
    ext = os.path.splitext(raw.split("?")[0])[1].lower()
    srv = to_server(raw)

    if ext == ".h5ad":
        r = run_python(H5AD % {"p": srv}, timeout=min(timeout, MAX_SYNC_SECONDS))
        data = r.get("data", {}) or {}
        out = _pull_json(data.get("stdout"))
        return out or {"error": "inspect_failed",
                       "stderr": str(data.get("stderr") or "")[:MAX_INSPECT_VALUE_CHARS],
                       "raw": str(data.get("stdout") or "")[:MAX_INSPECT_VALUE_CHARS]}

    if ext in (".csv", ".tsv", ".txt"):
        sep = "\t" if ext == ".tsv" else ","
        r = run_python(TABLE % {"p": srv, "sep": sep}, timeout=min(timeout, MAX_SYNC_SECONDS))
        data = r.get("data", {}) or {}
        out = _pull_json(data.get("stdout"))
        return out or {"error": "inspect_failed",
                       "stderr": str(data.get("stderr") or "")[:MAX_INSPECT_VALUE_CHARS],
                       "raw": str(data.get("stdout") or "")[:MAX_INSPECT_VALUE_CHARS]}

    if ext == ".parquet":
        r = run_python(PARQUET % {"p": srv}, timeout=min(timeout, MAX_SYNC_SECONDS))
        data = r.get("data", {}) or {}
        out = _pull_json(data.get("stdout"))
        return out or {"error": "inspect_failed",
                       "stderr": str(data.get("stderr") or "")[:MAX_INSPECT_VALUE_CHARS],
                       "raw": str(data.get("stdout") or "")[:MAX_INSPECT_VALUE_CHARS]}

    if ext == ".ipynb":
        return inspect_ipynb(raw)

    if ext in (".rds",):
        return inspect_rds(srv, timeout=timeout)

    # scripts / text
    if ext in (".py", ".r", ".sh", ".md", ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".log", ""):
        return inspect_text(raw)

    # default: peek as text
    return inspect_text(raw)


_TEXT_PREVIEW = _INSPECT_PRELUDE + r'''
import json, os, stat
p=safe_path(%(p)r); limit=%(limit)d
try:
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'): flags |= os.O_NOFOLLOW
    fd = os.open(p, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode): raise ValueError('not_regular_file')
        with os.fdopen(fd, 'rb') as fh:
            fd = None
            raw = fh.read(limit + 1)
    finally:
        if fd is not None: os.close(fd)
    truncated = len(raw) > limit
    raw = raw[:limit]
    out = {'format':'text', 'path':p, 'bytes_read':len(raw), 'truncated':truncated,
           'head':raw.decode('utf-8', errors='replace')}
except Exception as exc:
    out = {'format':'text', 'path':p, 'error':'bounded_read_failed', 'detail':clip(exc)}
emit_summary(out)
'''


_NOTEBOOK_SUMMARY = _INSPECT_PRELUDE + r'''
import json, os, stat
p=safe_path(%(p)r); limit=%(limit)d
MAX_CELLS = 200
MAX_VALUE = 160
try:
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'): flags |= os.O_NOFOLLOW
    fd = os.open(p, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode): raise ValueError('not_regular_file')
        with os.fdopen(fd, 'rb') as fh:
            fd = None
            raw = fh.read(limit + 1)
    finally:
        if fd is not None: os.close(fd)
    if len(raw) > limit: raise ValueError('notebook_exceeds_byte_limit')
    nb = json.loads(raw.decode('utf-8'))
    cells = []
    for i, cell in enumerate(itertools.islice(nb.get('cells', []), MAX_CELLS)):
        cell.pop('outputs', None)
        source = cell.get('source', [])
        if isinstance(source, list): source = ''.join(source)
        source = str(source)
        cells.append({'i':i, 'type':cell.get('cell_type'),
                      'lines':source.count('\\n') + (1 if source else 0),
                      'preview':source[:MAX_VALUE]})
    ks = (nb.get('metadata') or {}).get('kernelspec') or {}
    out = {'format':'ipynb', 'path':p, 'nbformat':nb.get('nbformat'),
           'kernel':ks.get('name'), 'n_cells':len(nb.get('cells', [])), 'cells':cells}
except Exception as exc:
    out = {'format':'ipynb', 'path':p, 'error':'notebook_summary_failed', 'detail':clip(exc)}
emit_summary(out)
'''


def _bounded_inspection_result(response, path, error):
    data = response.get("data", {}) or {}
    out = _pull_json(data.get("stdout"))
    return out or {"error": error, "path": path,
                   "stderr": str(data.get("stderr") or "")[:MAX_INSPECT_VALUE_CHARS],
                   "raw": str(data.get("stdout") or "")[:MAX_INSPECT_VALUE_CHARS]}


def _contents_text(contents, server_path, limit):
    """Read a small file through the Contents API; None means use the kernel reader."""
    value = evaluate_op("read", {"path": contents, "max_bytes": limit},
                        timeout=MAX_SYNC_SECONDS)
    data = value.get("data") or {}
    text = data.get("content")
    if data.get("ok") is not True or not isinstance(text, str):
        return None
    return {"format": "text", "path": server_path,
            "bytes_read": len(text.encode("utf-8")), "truncated": False, "head": text}


def inspect_text(path, max_bytes=DEFAULT_TEXT_PREVIEW_BYTES):
    """Return a byte-bounded text preview; Contents API first, kernel for the rest."""
    limit = validate_inspect_bytes(max_bytes)
    srv = to_server(path)
    contents = contents_path(path)
    if contents is not None:
        preview = _contents_text(contents, srv, limit)
        if preview is not None:
            return preview
    response = run_python(_TEXT_PREVIEW % {"p": srv, "limit": limit}, timeout=MAX_SYNC_SECONDS)
    return _bounded_inspection_result(response, srv, "text_preview_failed")


def inspect_ipynb(path, max_bytes=DEFAULT_NOTEBOOK_SUMMARY_BYTES):
    """Return a bounded no-output notebook summary without using the Contents API."""
    limit = validate_inspect_bytes(max_bytes)
    srv = to_server(path)
    response = run_python(_NOTEBOOK_SUMMARY % {"p": srv, "limit": limit}, timeout=MAX_SYNC_SECONDS)
    return _bounded_inspection_result(response, srv, "notebook_summary_failed")


def inspect_rds(srv_path, timeout=300):
    # Embed the description program in the durable launcher, then use its returned ID.
    cmd = f"Rscript -e {shlex.quote(RDS_R)} {shlex.quote(srv_path)}"
    job = submit(cmd, name="rdsinspect")
    jid = job["job_id"]
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        time.sleep(4)
        p = poll(jid, tail=200)
        last = p.get("stdout_tail", "")
        out = _pull_json(last)
        if out:
            return out
        if (p.get("status") or {}).get("state") == "finished":
            return {"error": "rds_inspect_no_json", "log_tail": last[-1500:]}
    return {"error": "rds_inspect_timeout", "job_id": jid, "log_tail": (last or "")[-1500:]}


# ─────────────────────────── misc ───────────────────────────
def close_tabs():
    """Stop the Agent Window session and delete the kernels this controller created."""
    state = _browser_state()
    session_id = state.get("session_id")
    if not session_id:
        return {"ok": True, "closed": 0}
    kernels = None
    tab_id = state.get("tab_id")
    if tab_id is not None and session_id in _session_ids():
        try:
            kernels = (_call_js("cleanup", {}, tab_id, 30) or {}).get("data")
        except Bridge:
            kernels = None
    try:
        bsk("session", "stop", session_id, timeout=60)
    except Bridge as exc:
        if not str(exc).startswith("bsk_not_found"):
            return {"ok": False, "error": str(exc)}
    _save_browser_state(session_id=None, tab_id=None, task_base=None)
    return {"ok": True, "closed": 1, "kernels": kernels}


def _bsk_version():
    if not os.path.exists(BSK_BIN):
        return ""
    try:
        proc = subprocess.run([BSK_BIN, "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
        return (proc.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def doctor(deep=False):
    """Local preflight; `deep` walks the full browser -> Jupyter chain layer by layer."""
    version = _bsk_version()
    sessions, reachable = [], True
    try:
        listed = bsk("session", "list", timeout=30)
        sessions = [s.get("session_id") for s in listed if isinstance(s, dict)] \
            if isinstance(listed, list) else []
    except Bridge:
        reachable = False
    state = _browser_state()
    info = {
        "ok": bool(version and reachable),
        "controller_version": CONTROLLER_VERSION,
        "platform": sys.platform,
        "windows_native_supported": os.name == "nt",
        "python": sys.version.split()[0],
        "bsk_bin": BSK_BIN,
        "bsk_bin_exists": os.path.exists(BSK_BIN),
        "bsk_version": version,
        "bsk_daemon_reachable": reachable,
        "bsk_sessions": sessions,
        "config_path": config_path(),
        "saved_workspace_ids": bool(CFG.get("project_id") and CFG.get("workspace_id")),
        "legacy_repo_config_present": os.path.exists(LEGACY_CONFIG_PATH),
        "browser_state": {key: state.get(key) for key in ("session_id", "tab_id", "task_base")},
    }
    if not deep:
        return info

    layers = []

    def add(name, status, detail=None, action=None):
        layers.append({"layer": name, "status": status, "detail": detail or {},
                       "action": action})

    add("cli", "ok" if version else "failed", {"bin": BSK_BIN, "version": version},
        None if version else "安装 BrowserSkill CLI，或设置 BSK_BIN")
    add("daemon", "ok" if reachable else "failed", {"sessions": sessions},
        None if reachable else "运行 bsk daemon start（或 bsk doctor 查看原因）")

    browsers = None
    if not reachable:
        add("extension", "skipped")
    else:
        try:
            browsers = bsk("browsers", timeout=30)
        except Bridge as exc:
            browsers = None
            add("extension", "failed", {"error": str(exc)[:200]},
                "检查 BrowserSkill 扩展与本地连接（bsk doctor）")
    if browsers is not None:
        online = browsers if isinstance(browsers, list) else []
        detail = [{"browser": b.get("browser_name"), "version": b.get("browser_version"),
                   "extension": b.get("extension_version"),
                   "unresponsive": b.get("unresponsive")} for b in online if isinstance(b, dict)]
        if online:
            add("extension", "ok", {"browsers": detail})
        else:
            add("extension", "failed", {"browsers": []},
                "在浏览器中启用 BrowserSkill 扩展并打开本地连接")

    if not WORKSPACE_URL:
        add("workspace", "failed", {"error": WORKSPACE_URL_ERROR or "workspace_not_configured"},
            "运行 connect --url \"<当前工作区 URL>\"")
    else:
        add("workspace", "ok", {"project_id": CFG.get("project_id"),
                                "workspace_id": CFG.get("workspace_id")})

    session_id = state.get("session_id")
    task_base = state.get("task_base")
    if not reachable:
        for name in ("session", "tab", "jupyter"):
            add(name, "skipped")
    elif not session_id or session_id not in sessions:
        add("session", "stale", {"session_id": session_id},
            "bsk session 空闲回收；下一次命令会自动重开，或运行 connect")
        add("tab", "skipped")
        add("jupyter", "skipped")
    else:
        add("session", "ok", {"session_id": session_id})
        tab_id = None
        try:
            tabs = _agent_tabs(session_id)
        except Bridge as exc:
            tabs = []
            add("tab", "failed", {"error": str(exc)[:200]}, "运行 connect 重开工作区")
        if not any(layer["layer"] == "tab" for layer in layers):
            match = next((t for t in tabs if task_base and
                          str(t.get("url", "")).startswith(INNER_ORIGIN + task_base)), None)
            tab_id = match.get("tab_id") if match else None
            if tab_id is None:
                add("tab", "stale", {"task_base": task_base, "agent_tabs": len(tabs)},
                    "工作区标签页不存在；运行 connect 或任意命令自动重开")
            else:
                add("tab", "ok", {"tab_id": tab_id, "task_base": task_base})
        if tab_id is None:
            add("jupyter", "skipped")
        else:
            try:
                ready = _probe_tab(tab_id, lite=True)
                add("jupyter", "ok" if ready else "failed", {"status_code": 200},
                    None if ready else "工作区 Jupyter 未就绪；检查启动或队列状态")
            except Bridge as exc:
                message = str(exc)
                if message.startswith("jupyter_auth_required:"):
                    add("jupyter", "failed", {"error": message[:200]},
                        "在同一个浏览器 profile 中重新登录 DCS/Jupyter")
                else:
                    add("jupyter", "failed", {"error": message[:200]},
                        "运行 connect 重新打开工作区，仍失败则 bsk doctor")

    info["layers"] = layers
    info["ok"] = not any(layer["status"] == "failed" for layer in layers)
    info["next_action"] = None
    for status in ("failed", "stale"):
        action = next((layer["action"] for layer in layers
                       if layer["status"] == status and layer["action"]), None)
        if action:
            info["next_action"] = action
            break
    return info


# ─────────────────────────── CLI ───────────────────────────
def _print(o):
    """Write UTF-8 JSON regardless of the Windows console code page."""
    text = json.dumps(o, ensure_ascii=False, indent=2) + "\n"
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    stream.write(text.encode("utf-8"))
    stream.flush()


def _result_failed(result):
    """Return True only for structured command results that clearly represent failure."""
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False or result.get("error"):
        return True
    if result.get("read_errors"):
        return True
    missing = result.get("missing")
    if isinstance(missing, dict) and any(bool(value) for value in missing.values()):
        return True
    status = result.get("status")
    if isinstance(status, dict):
        if status.get("state") == "setup_error":
            return True
        if status.get("state") == "finished" and status.get("exit_code") not in (None, 0):
            return True
    return False


def _emit(result, failure_code=1):
    _print(result)
    return failure_code if _result_failed(result) else 0


def main(argv):
    ap = argparse.ArgumentParser(description=f"Orange Note agent operations for Jupyter workspaces (v{CONTROLLER_VERSION})")
    sub = ap.add_subparsers(dest="subcmd", required=True)

    p = sub.add_parser("connect", help="connect only to the user-selected workspace URL and confirm its iframe")
    p.add_argument("--url", help="current workspace URL copied after the user manually opens Personal Analysis; "
                                 "a supplied URL is saved only after the live Jupyter probe succeeds")
    sub.add_parser("status", help="list active bsk sessions")
    sub.add_parser("probe", help="read-only Jupyter env probe")
    p = sub.add_parser("doctor", help="local prerequisite/config preflight (no secrets)")
    p.add_argument("--deep", action="store_true",
                   help="walk the full CLI -> daemon -> extension -> tab -> Jupyter chain")

    p = sub.add_parser("ls", help="list a Jupyter Contents path such as work/project")
    p.add_argument("path", nargs="?", default="work", help="use work/... for files under /data/work")
    p.add_argument("--all", action="store_true",
                   help="include dotfiles (uses a bounded kernel scan; the Contents view hides them)")
    p = sub.add_parser("cat", help="read a bounded preview; use work/... for /data/work files")
    p.add_argument("path")
    p = sub.add_parser("write", help="atomically create UTF-8 text under work/...; no-clobber by default")
    p.add_argument("path", help="target under work/... (server /data/work/...)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--content"); g.add_argument("--content-file")
    p.add_argument("--overwrite", action="store_true", help="explicitly allow replacing an existing file")

    p = sub.add_parser("get", help="download a Contents-root file to a local path (resumable)")
    p.add_argument("remote", help="work/... path under the Jupyter Contents root")
    p.add_argument("--out", required=True, help="local destination path")
    p.add_argument("--overwrite", action="store_true", help="replace an existing local file")
    p.add_argument("--no-verify", action="store_true", help="skip the remote sha256 comparison")
    p = sub.add_parser("put", help="upload a local file under the Contents root (<= 4 MiB)")
    p.add_argument("local", help="local source path")
    p.add_argument("--remote", required=True, help="work/... destination under the Contents root")
    p.add_argument("--overwrite", action="store_true", help="replace an existing remote file")

    p = sub.add_parser("run-python")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--code"); g.add_argument("--code-file")
    p.add_argument("--timeout", type=int, default=MAX_SYNC_SECONDS,
                   help=f"synchronous timeout in seconds (1-{MAX_SYNC_SECONDS}); use submit for longer work")

    p = sub.add_parser("run-shell", help="run a bounded shell command; default cwd is /data/work")
    p.add_argument("--cmd", required=True)
    p.add_argument("--cwd", default=WORK_ROOT, help="server cwd (default: /data/work)")
    p.add_argument("--timeout", type=int, default=MAX_SYNC_SECONDS,
                   help=f"synchronous timeout in seconds (1-{MAX_SYNC_SECONDS}); use submit for longer work")

    p = sub.add_parser("inspect"); p.add_argument("path"); p.add_argument("--timeout", type=int, default=300)

    p = sub.add_parser("submit")
    p.add_argument("--cmd", required=True,
                   help="raw server shell command (not OS-sandboxed; controller state uses /data/work/agent_jobs)")
    p.add_argument("--name", help="optional label matching [a-z][a-z0-9_-]{0,31}, for example analysis_1")
    p.add_argument("--idempotency-key",
                   help="reuse this key to get the same job id; a retried submit does not start a second job")
    p.add_argument("--cwd", default=WORK_ROOT,
                   help="server working directory for the payload (default: /data/work)")
    p = sub.add_parser("jobs", help="job registry: server job dirs merged with local receipts")
    p.add_argument("--remote", action="store_true", help="also scan /data/work/agent_jobs")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("cancel", help="SIGTERM a job whose PID still belongs to its job directory")
    p.add_argument("job_id")
    p = sub.add_parser("accept", help="record that a job's result was accepted")
    p.add_argument("job_id")
    p.add_argument("--note")
    p = sub.add_parser("poll"); p.add_argument("job_id"); p.add_argument("--tail", type=int, default=40)
    p = sub.add_parser("artifacts", help="inventory regular files under a job's artifacts directory")
    p.add_argument("job_id")

    sub.add_parser("close", help="stop the controller bsk session (closes its Agent Window tab)")

    a = ap.parse_args(argv)
    try:
        if a.subcmd == "status":
            try:
                return _emit({"ok": True, "bsk_sessions": bsk("session", "list", timeout=30)})
            except Bridge as exc:
                return _emit({"ok": False, "error": str(exc)})
        elif a.subcmd == "connect":
            supplied_url = canonical_workspace_url(a.url) if getattr(a, "url", None) else None
            if supplied_url:
                set_runtime_url(supplied_url)
            tab_id = ensure_tab(force=bool(supplied_url), full_probe=True)
            if supplied_url:
                save_url(supplied_url)
            state = _browser_state()
            _print({"ok": True, "session_id": state.get("session_id"), "tab_id": tab_id,
                    "task_base": state.get("task_base"), "url_saved": bool(supplied_url)})
        elif a.subcmd == "probe":
            _print(evaluate_op("probe", {}))
        elif a.subcmd == "doctor":
            return _emit(doctor(deep=a.deep))
        elif a.subcmd == "ls":
            return _emit(list_path(a.path, include_hidden=a.all))
        elif a.subcmd == "cat":
            return _emit(cat_path(a.path))
        elif a.subcmd == "write":
            content = a.content
            if a.content_file:
                content = read_local_text(a.content_file, MAX_WRITE_BYTES, "write_content_too_large")
            validate_text_payload(content, MAX_WRITE_BYTES, "write_content_too_large")
            _print(write_text(a.path, content, overwrite=bool(a.overwrite)))
        elif a.subcmd == "get":
            return _emit(get_file(a.remote, a.out, overwrite=a.overwrite,
                                  verify=not a.no_verify))
        elif a.subcmd == "put":
            return _emit(put_file(a.local, a.remote, overwrite=a.overwrite))
        elif a.subcmd == "run-python":
            code = a.code if a.code is not None else read_local_text(
                a.code_file, MAX_CODE_BYTES, "python_code_too_large")
            _print(run_python(code, timeout=a.timeout))
        elif a.subcmd == "run-shell":
            return _emit(run_shell(a.cmd, cwd=a.cwd, timeout=a.timeout))
        elif a.subcmd == "inspect":
            return _emit(inspect(a.path, timeout=a.timeout))
        elif a.subcmd == "submit":
            _print(submit(a.cmd, name=a.name, cwd=a.cwd,
                          idempotency_key=a.idempotency_key))
        elif a.subcmd == "jobs":
            return _emit(list_jobs(limit=a.limit, remote=a.remote))
        elif a.subcmd == "cancel":
            return _emit(cancel_job(a.job_id))
        elif a.subcmd == "accept":
            return _emit(accept_job(a.job_id, note=a.note))
        elif a.subcmd == "poll":
            return _emit(poll(a.job_id, tail=a.tail))
        elif a.subcmd == "artifacts":
            return _emit(artifacts(a.job_id))
        elif a.subcmd == "close":
            return _emit(close_tabs())
        else:
            ap.error("unknown command")
    except Bridge as exc:
        _print({"ok": False, "error": str(exc)}); return 1
    except ValueError as exc:
        _print({"ok": False, "error": str(exc)}); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
