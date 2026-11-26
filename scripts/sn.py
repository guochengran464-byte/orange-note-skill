#!/usr/bin/env python3
"""Orange Note browser-backed agent operations, v4.2.0.
Provides file inspection/transfer, bounded execution and persistent jobs through
the built-in Jupyter workspace connector. Browser credentials stay in the page.
The existing sn.py filename and runtime configuration remain compatible.
"""
from __future__ import annotations
import argparse, base64, binascii, csv, errno, fnmatch, hashlib, io, json, os, posixpath, re, shlex, shutil, subprocess, sys, tarfile, time, uuid
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
RUNTIME_JS_PATH = os.path.join(HERE, "jupyter_runtime.js")
CONTROLLER_VERSION = "5.0.0"
LOGICAL_SESSION_ID = None
_ACTIVE_BROWSER_SESSION = None


DEFAULT_CFG = {
    "session": "stereonote",
    "work_root": "/data/work",
    "bsk_bin": "",
    "bsk_browser_id": "",
}
CONFIG_EXAMPLE_PATH = os.path.join(SKILL_DIR, "config.example.json")
LEGACY_CONFIG_PATH = os.path.join(SKILL_DIR, "config.json")


def _base_config_path():
    """Return a per-user config path, never a path inside the Git checkout."""
    override = os.environ.get("SN_CONFIG_DIR")
    if override:
        base = os.path.expanduser(override)
    elif os.name == "nt" and os.environ.get("APPDATA"):
        base = os.path.join(os.environ["APPDATA"], "stereonote")
    else:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "stereonote")
    return os.path.join(base, "config.json")


def config_path():
    if LOGICAL_SESSION_ID:
        return os.path.join(_logical_dir(LOGICAL_SESSION_ID), "config.json")
    return _base_config_path()


def _logical_dir(session_id):
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError("invalid_session_id")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid_session_id") from exc
    root = os.path.realpath(os.path.join(os.path.dirname(_base_config_path()), "sessions"))
    path = os.path.realpath(os.path.join(root, session_id))
    if os.path.commonpath((root, path)) != root:
        raise ValueError("invalid_session_directory")
    return path


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
    if not LOGICAL_SESSION_ID:
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


def _configure_runtime():
    global CFG, BSK_BIN, WORKSPACE_URL, WORKSPACE_URL_ERROR, WORK_ROOT
    CFG = _load_cfg()
    BSK_BIN = (os.environ.get("BSK_BIN") or CFG.get("bsk_bin") or shutil.which("bsk") or
               os.path.join(os.path.expanduser("~"), ".local", "bin",
                            "bsk.exe" if os.name == "nt" else "bsk"))
    WORKSPACE_URL = _build_workspace_url(CFG.get("project_id", ""), CFG.get("workspace_id", ""))
    WORKSPACE_URL_ERROR = None
    if not WORKSPACE_URL and CFG.get("workspace_url"):
        try:
            WORKSPACE_URL = canonical_workspace_url(CFG.get("workspace_url"))
        except ValueError as exc:
            WORKSPACE_URL_ERROR = str(exc)
    raw_url = os.environ.get("SN_URL", "").strip()
    if raw_url:
        try:
            env_url = canonical_workspace_url(raw_url)
            if LOGICAL_SESSION_ID and env_url != WORKSPACE_URL:
                raise ValueError("session_workspace_mismatch: SN_URL differs from the bound workspace")
            WORKSPACE_URL = env_url
        except ValueError as exc:
            if LOGICAL_SESSION_ID:
                raise
            WORKSPACE_URL, WORKSPACE_URL_ERROR = "", str(exc)
    WORK_ROOT = CFG.get("work_root", "/data/work").rstrip("/")


_configure_runtime()


def set_runtime_url(url):
    """Validate a workspace URL and use its canonical form for this process only."""
    global WORKSPACE_URL, WORKSPACE_URL_ERROR
    target = canonical_workspace_url(url)
    if LOGICAL_SESSION_ID and target != WORKSPACE_URL:
        raise ValueError("session_workspace_mismatch: create another session for a different workspace")
    WORKSPACE_URL = target
    WORKSPACE_URL_ERROR = None
    return WORKSPACE_URL


def save_url(url):
    """Persist only DCS project/workspace identifiers in a per-user config file."""
    global WORKSPACE_URL, WORKSPACE_URL_ERROR, CFG
    set_runtime_url(url)
    project_id, workspace_id = _workspace_ids(url)
    WORKSPACE_URL = _build_workspace_url(project_id, workspace_id)
    WORKSPACE_URL_ERROR = None
    cfg = _read_json(config_path())
    for key in ("session", "work_root", "bsk_bin", "bsk_browser_id"):
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
BATCH_ROOT = "/data/work/agent_jobs/batches"
MAX_CHUNKED_UPLOAD_BYTES = 64 * 1024 * 1024
CHUNKED_UPLOAD_SLICE = 512 * 1024
OBSERVATION_RETRY_ATTEMPTS = 4
OBSERVATION_RETRY_BASE_SEC = 0.4
MAX_LOG_TAIL = 200
MAX_LOG_BYTES = 64 * 1024
MAX_STATUS_BYTES = 4096
MAX_INSPECT_BYTES = 1024 * 1024
MAX_INSPECT_RESULT_BYTES = 48 * 1024
MAX_INSPECT_VALUE_CHARS = 256
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


@contextmanager
def _controller_lock(directory, timeout=10):
    with open(os.path.join(directory, "controller.lock"), "a+b") as lock:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"\0")
            lock.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise Bridge("session_busy: another command is using this session") from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def selected_session(session_id):
    if session_id is None:
        yield
        return
    directory = _logical_dir(session_id)
    if not os.path.isfile(os.path.join(directory, "config.json")):
        raise ValueError("unknown_session: use session create first")
    with _controller_lock(directory):
        keys = ("LOGICAL_SESSION_ID", "_ACTIVE_BROWSER_SESSION", "CFG", "BSK_BIN",
                "WORKSPACE_URL", "WORKSPACE_URL_ERROR", "WORK_ROOT")
        previous = {key: globals()[key] for key in keys}
        try:
            globals().update(LOGICAL_SESSION_ID=session_id, _ACTIVE_BROWSER_SESSION=None)
            _configure_runtime()
            if not WORKSPACE_URL:
                raise ValueError("session_workspace_missing")
            yield
        finally:
            globals().update(previous)


def create_logical_session(url):
    project_id, workspace_id = _workspace_ids(url)
    session_id = str(uuid.uuid4())
    directory = _logical_dir(session_id)
    os.makedirs(directory, mode=0o700, exist_ok=False)
    cfg = {key: CFG.get(key, DEFAULT_CFG[key]) for key in ("work_root", "bsk_bin", "bsk_browser_id")}
    if os.environ.get("BSK_BROWSER"):
        cfg["bsk_browser_id"] = os.environ["BSK_BROWSER"]
    cfg.update(project_id=project_id, workspace_id=workspace_id, created_at=_utc_now())
    path = os.path.join(directory, "config.json")
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    return session_id


def list_logical_sessions():
    root = os.path.join(os.path.dirname(_base_config_path()), "sessions")
    rows = []
    if not os.path.isdir(root):
        return {"ok": True, "sessions": rows}
    for name in sorted(os.listdir(root)):
        try:
            directory = _logical_dir(name)
        except ValueError:
            continue
        cfg = _read_json(os.path.join(directory, "config.json"))
        url = _build_workspace_url(cfg.get("project_id"), cfg.get("workspace_id"))
        if not url:
            continue
        suffix = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        state = _read_json(os.path.join(directory, "browser-" + suffix + ".json"))
        rows.append({"logical_session_id": name, "workspace_url": url, "state_dir": directory,
                     "created_at": cfg.get("created_at"), "browser_session_id": state.get("session_id"),
                     "tab_id": state.get("tab_id"), "updated_at": state.get("updated_at")})
    return {"ok": True, "sessions": rows, "note": "saved state only; use --session ID probe for live status"}


def validate_job_id(value):
    """Return a safe, UUID-backed job id or raise ValueError."""
    if not isinstance(value, str) or not _JOB_ID_RE.fullmatch(value):
        raise ValueError("invalid_job_id")
    return value


def _validate_bounded_int(value, low, high, error):
    """Parse a decimal integer within [low, high], rejecting bools and loose forms."""
    if isinstance(value, bool):
        raise ValueError(error)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(error) from exc
    if not low <= number <= high or str(value).strip() != str(number):
        raise ValueError(error)
    return number


def validate_tail(value):
    """Return a bounded log-tail line count or raise ValueError."""
    return _validate_bounded_int(value, 1, MAX_LOG_TAIL, "invalid_tail")


def validate_inspect_bytes(value):
    """Return a bounded server-side inspection byte limit or raise ValueError."""
    return _validate_bounded_int(value, 1, MAX_INSPECT_BYTES, "invalid_inspect_bytes")


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


def build_job_launcher(job_id, command, cwd=None, threads=None, local_tmp=False,
                       batch_id=None, sample_id=None):
    """Build a detached shell launcher with durable job-state records."""
    job_id = validate_job_id(job_id)
    if not isinstance(command, str) or not command:
        raise ValueError("invalid_command")
    validate_text_payload(command, MAX_COMMAND_BYTES, "command_too_large")
    if threads is not None:
        threads = _validate_bounded_int(threads, 1, 256, "invalid_threads")
    jdir = f"{JOB_ROOT}/{job_id}"
    execution_cwd = to_server(cwd or WORK_ROOT)
    qdir = shlex.quote(jdir)
    qcwd = shlex.quote(execution_cwd)
    qjob = shlex.quote(json.dumps(job_id))
    manifest_obj = {"schema_version": 2, "job_id": job_id, "command": command, "cwd": execution_cwd}
    if batch_id:
        manifest_obj["batch_id"] = str(batch_id)
    if sample_id:
        manifest_obj["sample_id"] = str(sample_id)
    if threads is not None:
        manifest_obj["threads"] = threads
    if local_tmp:
        manifest_obj["local_tmp"] = True
    manifest = json.dumps(manifest_obj, ensure_ascii=False, separators=(",", ":"))
    qmanifest = shlex.quote(manifest)
    env_lines = []
    if threads is not None:
        env_lines.append(
            f"export OPENBLAS_NUM_THREADS={threads} OMP_NUM_THREADS={threads} "
            f"MKL_NUM_THREADS={threads} NUMEXPR_NUM_THREADS={threads}"
        )
    local_tmp_setup = ""
    local_tmp_cleanup = ""
    if local_tmp:
        tmp_dir = shlex.quote(f"/tmp/sn_job_{job_id}")
        local_tmp_setup = f"mkdir -p {tmp_dir} && chmod 700 {tmp_dir}\nexport TMPDIR={tmp_dir} SN_TMPDIR={tmp_dir}"
        local_tmp_cleanup = f"  rm -rf {tmp_dir} 2>/dev/null || true"
    env_block = "\n".join(x for x in (*env_lines, local_tmp_setup) if x)
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
printf '{{"state":"queued","job_id":%s}}\\n' {qjob} > {qdir}/status.json.tmp
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
printf '{{"state":"running","job_id":%s,"pid":%s,"started_at":"%s"}}\\n' {qjob} "$$" "$started_at" > {qdir}/status.json.tmp.$$
mv {qdir}/status.json.tmp.$$ {qdir}/status.json
chmod 600 {qdir}/status.json
printf '{{"job_id":%s,"pid":%s,"ts":"%s","epoch":%s}}\\n' {qjob} "$$" "$started_at" "$(date -u +%s)" > {qdir}/heartbeat.json.tmp.$$
mv {qdir}/heartbeat.json.tmp.$$ {qdir}/heartbeat.json
chmod 600 {qdir}/heartbeat.json
printf '%s\\n' "$started_at" > {qdir}/runner.ready.tmp.$$ && mv {qdir}/runner.ready.tmp.$$ {qdir}/runner.ready
chmod 600 {qdir}/runner.ready
sn_stage() {{
  local _s="${{1:-unknown}}" _d="${{2:-}}" _t="${{3:-}}" _n="${{4:-}}"
  python3 -c 'import json, os, sys, time
p, s, d, t, n = sys.argv[1:6]
obj = {{"stage": s, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}
if d.isdigit(): obj["done"] = int(d)
if t.isdigit(): obj["total"] = int(t)
if n: obj["note"] = n[:256]
tmp = p + ".tmp." + str(os.getpid())
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(obj, fh, ensure_ascii=False)
    fh.write("\\n")
os.chmod(tmp, 0o600)
os.replace(tmp, p)' {qdir}/stage.json "$_s" "$_d" "$_t" "$_n" 2>/dev/null || true
}}
export -f sn_stage
_SN_HB_PID=""
(
  _sp=""
  trap '[ -n "$_sp" ] && kill "$_sp" 2>/dev/null || true; exit 0' TERM INT
  while true; do
    sleep 60 &
    _sp=$!
    wait "$_sp" 2>/dev/null || break
    _now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    _ep=$(date -u +%s)
    printf '{{"job_id":%s,"pid":%s,"ts":"%s","epoch":%s}}\\n' {qjob} "$$" "$_now" "$_ep" > {qdir}/heartbeat.json.tmp.$$ 2>/dev/null && mv {qdir}/heartbeat.json.tmp.$$ {qdir}/heartbeat.json 2>/dev/null || true
  done
) </dev/null >/dev/null 2>&1 &
_SN_HB_PID=$!
finalize() {{
  rc=$?
  trap - EXIT TERM INT
  if [ -n "$_SN_HB_PID" ]; then kill "$_SN_HB_PID" 2>/dev/null || true; wait "$_SN_HB_PID" 2>/dev/null || true; fi
{local_tmp_cleanup}
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
{env_block}
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
  printf '{{"state\":\"setup_error\",\"job_id\":%s,\"error\":\"runner_start_timeout\",\"ended_at\":\"%s\"}}\\n' {qjob} "$ended_at" > {qdir}/status.json.tmp.$$
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
PAGE_STATE_JS = (
    "(() => { const f = document.querySelector('iframe[src*=\"inner.dcs.cloud/notebook/st/\"]');"
    " const t = ((document.body && document.body.innerText) || '').slice(0, 1200);"
    " return { href: (location.href || '').split('?')[0], title: document.title || '',"
    " iframe: f ? f.src : '', text: t }; })()"
)
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
    state["updated_at"] = _utc_now()
    path = _browser_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + "." + uuid.uuid4().hex + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _bsk_call(*args, timeout=120, session=None, tab_id=None):
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
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("error") or "").strip()[:200]
        suffix = f" ({detail})" if detail else ""
        raise Bridge(f"bsk_failed({args[0]}): exit {proc.returncode}{suffix}")
    return payload


def _is_transient_observation_error(exc):
    """Return True only when BrowserSkill rejected an action BEFORE dispatch due to DOM churn.

    Human handoff / non-ready modes (`cooperation_not_ready`) and post-dispatch
    errors (timeouts, navigation lost) MUST return False and never be retried.
    """
    msg = str(exc)
    return (
        "Document changed during observation" in msg
        or "strict observation changed before this action" in msg
        or "A page frame changed; synchronize before choosing this action" in msg
    )


def bsk(*args, timeout=120, session=None, tab_id=None):
    """Coordinate each browser action when using BrowserSkill 0.4 or newer."""
    if args[0] in ("navigate", "evaluate") and session:
        version = _bsk_version()
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version)
        if not match:
            raise Bridge("bsk_version_unknown: cannot determine browser coordination requirements")
        parsed_version = tuple(map(int, match.groups()))
        if parsed_version >= (0, 4, 0):
            if tab_id is None:
                raise Bridge("agent_tab_unverified: browser action requires a controller-owned tab")
            if args[0] == "navigate":
                listed = _bsk_call("tab", "list", timeout=30, session=session)
                tabs = listed.get("tabs", []) if isinstance(listed, dict) else []
                tab = next((t for t in tabs if t.get("tab_id") == tab_id and t.get("scope") == "agent"), None)
                if tab is None:
                    raise Bridge("agent_tab_unverified: navigation requires a controller-owned tab")
                if tab.get("url") == "about:blank":
                    return _bsk_call(*args, timeout=timeout, session=session, tab_id=tab_id)
            last_exc = None
            for attempt in range(OBSERVATION_RETRY_ATTEMPTS):
                try:
                    sync = _bsk_call("cooperate", "sync", "--settle", timeout=30, session=session, tab_id=tab_id)
                    state = sync.get("state") or {} if isinstance(sync, dict) else {}
                    context = sync.get("context_id") if isinstance(sync, dict) else None
                    if state.get("mode") != "ready" or not isinstance(context, str) or not context:
                        raise Bridge("cooperation_not_ready: " + str(state.get("mode") or "unknown"))
                    call_args = (*args, "--context", context)
                    # 0.4.1+ target policy guards scripts with the top-level observation
                    # receipt. The nested observation ID only identifies the ref map.
                    if parsed_version >= (0, 4, 1):
                        observation = sync.get("observation_id")
                        if not isinstance(observation, str) or not observation:
                            raise Bridge("cooperation_observation_missing: script was not dispatched")
                        call_args = (*call_args, "--observation", observation)
                    return _bsk_call(*call_args, timeout=timeout, session=session, tab_id=tab_id)
                except Bridge as exc:
                    if not _is_transient_observation_error(exc) or attempt + 1 >= OBSERVATION_RETRY_ATTEMPTS:
                        raise
                    last_exc = exc
                    time.sleep(OBSERVATION_RETRY_BASE_SEC * (2 ** attempt))
            if last_exc is not None:
                raise last_exc
    return _bsk_call(*args, timeout=timeout, session=session, tab_id=tab_id)


def _is_cooperation_error(exc):
    return str(exc).startswith("cooperation_") or "(cooperate)" in str(exc)


def _session_ids():
    listed = bsk("session", "list", timeout=30)
    if not isinstance(listed, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("session_id"), str)
            or not item["session_id"] for item in listed):
        raise Bridge("bsk_session_list_invalid: existing sessions could not be verified")
    return {item["session_id"] for item in listed}


def ensure_session(force=False):
    """Reuse the recorded session; create only after verified absence."""
    global _ACTIVE_BROWSER_SESSION
    if LOGICAL_SESSION_ID and _ACTIVE_BROWSER_SESSION and not force:
        return _ACTIVE_BROWSER_SESSION
    session_id = _browser_state().get("session_id")
    if session_id and not force and session_id in _session_ids():
        if LOGICAL_SESSION_ID:
            _ACTIVE_BROWSER_SESSION = session_id
        return session_id
    browser = os.environ.get("BSK_BROWSER") or CFG.get("bsk_browser_id")
    selector = ("--browser", browser) if browser else ()
    started = bsk("session", "start", *selector, timeout=120)
    session_id = started.get("session_id") if isinstance(started, dict) else None
    if not session_id:
        raise Bridge("bsk_session_start_failed: 未返回 session_id")
    _save_browser_state(session_id=session_id, tab_id=None, task_base=None)
    if LOGICAL_SESSION_ID:
        _ACTIVE_BROWSER_SESSION = session_id
    return session_id


def _agent_tabs(session_id):
    listed = bsk("tab", "list", session=session_id, timeout=30)
    tabs = listed.get("tabs") if isinstance(listed, dict) else None
    return [t for t in (tabs or []) if isinstance(t, dict) and t.get("scope") == "agent"]


def _eval_js(tab_id, expression, timeout=60, session_id=None):
    """Evaluate one expression in the tab's main frame; returns the JSON value."""
    session_id = session_id or ensure_session()
    budget = min(max(1, timeout), 60)
    # A lost browser response cannot prove that an expression had no side effects.
    reply = bsk("evaluate", expression, "--timeout", f"{budget}s",
                timeout=budget + 20, session=session_id, tab_id=tab_id)
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
    if LOGICAL_SESSION_ID and session_id is None:
        session_id = ensure_session()
    call = ("(async () => { return await window.__codexJupyter(%s, %s); })()"
            % (json.dumps(op), json.dumps(payload)))
    expression = _runtime() + "\n;" + call
    if len(expression) <= MAX_EXPR_CHARS:
        return _eval_js(tab_id, expression, timeout, session_id)
    _eval_js(tab_id, _runtime(), timeout, session_id)
    blob = json.dumps(payload)
    buffer = "window.__snPayload_" + uuid.uuid4().hex
    # Push chunks onto an array and join once: repeated `str += chunk` is O(n^2) and
    # dominates transfer time for multi-MiB payloads (measured 3.4x slower at 3.2 MiB).
    _eval_js(tab_id, buffer + " = []", timeout, session_id)
    for offset in range(0, len(blob), MAX_CHUNK_CHARS):
        _eval_js(tab_id, buffer + ".push(" + json.dumps(blob[offset:offset + MAX_CHUNK_CHARS]) + ")",
                 timeout, session_id)
    expression = ("(async () => { const p = JSON.parse(%s.join('')); %s.length = 0;"
                  " return await window.__codexJupyter(%s, p); })()"
                  % (buffer, buffer, json.dumps(op)))
    return _eval_js(tab_id, expression, timeout, session_id)


def _task_base(iframe_src):
    match = TASK_BASE_RE.match(str(iframe_src or ""))
    return match.group(1) if match else None


def _probe_tab(tab_id, lite=True, detail=None):
    """Read-only readiness probe. `lite` skips root/work/session enumeration."""
    result = _call_js("probe", {"lite": bool(lite)}, tab_id, 30) or {}
    data = result.get("data") or {}
    if detail is not None:
        detail.update(data)
    statuses = [data.get(key) for key in ("status_code", "root_status", "work_status")]
    if any(status in (401, 403) for status in statuses):
        raise Bridge("jupyter_auth_required: Jupyter 登录或访问权限失效，请在 Edge 中检查登录状态")
    return bool(result.get("ok")) and data.get("status_code") == 200


def _classify_page_state(tab_id, session_id=None):
    """Best-effort read-only diagnosis when the StereoNote iframe is absent."""
    try:
        info = _eval_js(tab_id, PAGE_STATE_JS, 15, session_id=session_id)
    except Bridge:
        return None
    if not isinstance(info, dict):
        return None
    href = str(info.get("href") or "")
    text = str(info.get("text") or "")
    low = (href + " " + text).lower()
    if re.search(r"(?:#|/)login(?:[/?#]|$)", href, re.I) or any(
        k in low for k in ("账号登录", "扫码登录", "登录dcs", "sign in", "log in", "login required")
    ):
        return "dcs_login_required: DCS 登录态已失效，请在浏览器中重新登录后重试"
    if any(k in low for k in ("未启动", "已停止", "已关闭", "启动容器", "容器已停止", "not started", "stopped")):
        return "workspace_stopped: StereoNote 容器当前未启动，请在 DCS 页面启动工作区后再连接"
    if any(k in low for k in ("启动中", "排队中", "初始化", "starting", "initializing", "pending", "creating")):
        return "workspace_starting_timeout: StereoNote 容器仍在启动/排队中，请稍后重试 connect"
    return None


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
        matches = [t for t in tabs if str(t.get("url", "")).startswith(INNER_ORIGIN + task_base)]
        match = next((t for t in matches if t.get("tab_id") == state.get("tab_id")),
                     matches[0] if matches else None)
        if match and match.get("tab_id") is not None:
            tab_id = match["tab_id"]
            try:
                ready = _probe_tab(tab_id, lite=not full_probe)
            except Bridge as exc:
                if str(exc).startswith("jupyter_auth_required:"):
                    raise
                raise Bridge(f"workspace_probe_failed_kept_open: tab_id={tab_id}; {exc}") from exc
            if not ready:
                raise Bridge(f"jupyter_unavailable_kept_open: tab_id={tab_id}; retry the probe when the workspace is ready")
            _save_browser_state(tab_id=tab_id)
            return tab_id

    tab_id = tabs[0].get("tab_id") if tabs else None
    try:
        nav = bsk("navigate", target_url, session=session_id, timeout=120, tab_id=tab_id)
    except Bridge as exc:
        # bsk navigate waits for full `load` (30s default) which can time out (exit 4)
        # on slow DCS SPA subresources even after the tab has already landed.
        if tab_id is not None and ("exit 4" in str(exc) or "bsk_timeout(navigate)" in str(exc)):
            nav = {"tab_id": tab_id}
        else:
            raise
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
        except Bridge as exc:
            if _is_cooperation_error(exc):
                raise
            iframe_src = ""
        if iframe_src:
            break
        time.sleep(2)
    task_base = _task_base(iframe_src)
    if not task_base:
        diagnosis = _classify_page_state(tab_id, session_id=session_id)
        if diagnosis:
            raise Bridge(diagnosis)
        raise Bridge("workspace_startup_timeout: 工作区 iframe 在等待时间内未就绪；"
                     "请检查 DCS 工作区是否已启动")

    # Park the controller tab on a lightweight same-origin HTML endpoint instead of
    # the heavy JupyterLab `/lab` SPA. This avoids multi-tab workspace collisions
    # (`auto-N`), eliminates continuous frontend polling/DOM churn, and prevents
    # browser freezes when multiple agent windows are open alongside the user's tab.
    lite_target = INNER_ORIGIN + task_base + "sn-agent-lite"
    full_target = str(iframe_src).split("?", 1)[0]
    use_full = os.environ.get("SN_FULL_UI", "").strip() in ("1", "true", "yes")
    park_url = full_target if use_full else lite_target
    try:
        bsk("navigate", park_url, session=session_id, timeout=120, tab_id=tab_id)
    except Bridge as exc:
        if not ("exit 4" in str(exc) or "bsk_timeout(navigate)" in str(exc)):
            raise
    probe_deadline = time.monotonic() + 45
    auth_minted = False
    while True:
        try:
            if _probe_tab(tab_id, lite=not full_probe):
                break
        except Bridge as exc:
            if str(exc).startswith("jupyter_auth_required:"):
                # On a cold browser profile, the iframe may not have minted the inner
                # session cookie yet. Hit the authenticated iframe URL once, then re-park
                # on the lightweight page so `/lab` never stays running in the background.
                if not use_full and not auth_minted and "?token=" in str(iframe_src):
                    auth_minted = True
                    try:
                        bsk("navigate", str(iframe_src), session=session_id, timeout=60, tab_id=tab_id)
                        bsk("navigate", lite_target, session=session_id, timeout=60, tab_id=tab_id)
                        continue
                    except Bridge:
                        pass
                raise
            if _is_cooperation_error(exc):
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
    code = f'''import base64, json, os, stat, tempfile
_path = {server_path!r}
_root = {root!r}
_data = base64.b64decode({encoded!r}, validate=True)
_overwrite = {bool(overwrite)!r}
_out = None
_temp = None
try:
    _parent = os.path.dirname(_path)
    if os.path.commonpath((os.path.realpath(_root), os.path.realpath(_parent))) != os.path.realpath(_root):
        raise ValueError('write_parent_outside_work_root')
    os.makedirs(_parent, mode=0o700, exist_ok=True)
    _existed = os.path.lexists(_path)
    if _existed and not _overwrite:
        raise FileExistsError(_path)
    if _overwrite and _existed:
        _before = os.lstat(_path)
        if not stat.S_ISREG(_before.st_mode):
            raise ValueError('target_not_regular_file')
    _fd, _temp = tempfile.mkstemp(prefix='.sn-write-', dir=_parent)
    try:
        if not stat.S_ISREG(os.fstat(_fd).st_mode):
            raise ValueError('target_not_regular_file')
        os.fchmod(_fd, 0o600)
        _offset = 0
        while _offset < len(_data):
            _written = os.write(_fd, _data[_offset:])
            if _written <= 0: raise OSError('write_made_no_progress')
            _offset += _written
        os.fsync(_fd)
        os.lseek(_fd, 0, os.SEEK_SET)
        _readback = b''
        while len(_readback) <= len(_data):
            _chunk = os.read(_fd, min(1048576, len(_data) + 1 - len(_readback)))
            if not _chunk: break
            _readback += _chunk
    finally:
        os.close(_fd)
    if _readback != _data:
        raise ValueError('write_readback_mismatch')
    _existed = os.path.lexists(_path)
    if _overwrite:
        if _existed and not stat.S_ISREG(os.lstat(_path).st_mode):
            raise ValueError('target_not_regular_file')
        os.replace(_temp, _path)
        _temp = None
    else:
        os.link(_temp, _path)
    _out = {{'ok': True, 'path': _path, 'bytes': len(_data),
             'overwritten': bool(_overwrite and _existed)}}
except FileExistsError:
    _out = {{'ok': False, 'error': 'file_exists_use_overwrite', 'path': _path}}
except Exception as _exc:
    _out = {{'ok': False, 'error': str(_exc)[:256], 'path': _path}}
finally:
    if _temp is not None:
        try: os.unlink(_temp)
        except OSError: pass
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


def _resolved_link_stat(contents, expected_size):
    """Verify a size discrepancy against the followed server-side symlink."""
    server_path = posixpath.normpath("/data/" + contents.lstrip("/"))
    if server_path != "/data" and not server_path.startswith("/data/"):
        raise Bridge("remote_link_outside_contents_root")
    code = ("import os, json\n"
            f"p = {server_path!r}\n"
            "a = os.lstat(p); b = os.stat(p)\n"
            "print(json.dumps({'is_link': os.path.islink(p), 'size': b.st_size, "
            "'fingerprint': [a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns,a.st_ctime_ns,"
            "b.st_dev,b.st_ino,b.st_size,b.st_mtime_ns,b.st_ctime_ns]}))")
    result = run_python(code)
    try:
        data = json.loads((result.get("data") or {}).get("stdout") or "")
    except (ValueError, TypeError) as exc:
        raise Bridge("remote_link_stat_invalid") from exc
    if data.get("is_link") is not True or data.get("size") != expected_size:
        raise Bridge("remote_link_size_changed_or_unverified")
    return {"size": expected_size, "last_modified": "resolved-link:" + json.dumps(data["fingerprint"])}


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
    range_size = data.get("range_size")
    if isinstance(range_size, int) and not isinstance(range_size, bool) and range_size != size:
        return _resolved_link_stat(contents, range_size)
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


def _put_chunked_bytes(raw, remote, overwrite=False):
    """Upload up to MAX_CHUNKED_UPLOAD_BYTES via Jupyter Contents chunked PUT with atomic publish."""
    server_path, contents = _transfer_path(remote)
    if not server_path.startswith(WORK_ROOT + "/"):
        raise ValueError("write_path_must_be_under_work_root")
    if len(raw) > MAX_CHUNKED_UPLOAD_BYTES:
        raise ValueError(f"upload_too_large: limit {MAX_CHUNKED_UPLOAD_BYTES} bytes")
    tab = ensure_tab()
    session_id = ensure_session()
    dest_exists = False
    try:
        _remote_stat(contents, session_id, tab)
        dest_exists = True
    except Bridge as exc:
        if "not_found" not in str(exc):
            raise
    if dest_exists and not overwrite:
        raise Bridge("file_exists_use_overwrite")
    parent = posixpath.dirname(contents)
    if parent:
        ed = _call_js("ensure_dir", {"path": parent}, tab, 30, session_id) or {}
        if not ed.get("ok"):
            raise Bridge(f"upload_mkdir_failed: {ed.get('error')}")
    # Avoid leading dot: Jupyter Contents API rejects hidden files when allow_hidden=False.
    tmp_name = f"__sn_up_{uuid.uuid4().hex}.tmp"
    tmp_contents = f"{parent}/{tmp_name}" if parent else tmp_name
    tmp_server = posixpath.join(posixpath.dirname(server_path), tmp_name)
    try:
        if len(raw) <= CHUNKED_UPLOAD_SLICE:
            b64 = base64.b64encode(raw).decode("ascii")
            r = _call_js("put_chunk", {"path": tmp_contents, "b64": b64}, tab, 60, session_id) or {}
            if not r.get("ok"):
                raise Bridge(f"upload_chunk_failed: {r.get('error')}")
        else:
            total_chunks = (len(raw) + CHUNKED_UPLOAD_SLICE - 1) // CHUNKED_UPLOAD_SLICE
            for idx in range(total_chunks):
                piece = raw[idx * CHUNKED_UPLOAD_SLICE:(idx + 1) * CHUNKED_UPLOAD_SLICE]
                chunk_num = -1 if idx == total_chunks - 1 else idx + 1
                b64 = base64.b64encode(piece).decode("ascii")
                r = _call_js("put_chunk", {"path": tmp_contents, "b64": b64, "chunk": chunk_num},
                             tab, 60, session_id) or {}
                if not r.get("ok"):
                    raise Bridge(f"upload_chunk_failed({chunk_num}): {r.get('error')}")
        staged = _remote_stat(tmp_contents, session_id, tab)
        if staged["size"] != len(raw):
            raise Bridge("upload_size_mismatch")
        if dest_exists and overwrite:
            # Prefer atomic server-side replace so an existing file is never lost on error
            code = (
                "import os\n"
                f"os.chmod({tmp_server!r}, 0o600)\n"
                f"os.replace({tmp_server!r}, {server_path!r})\n"
                "print('__SN_REPLACED__')\n"
            )
            rep = run_python(code, timeout=30)
            if "__SN_REPLACED__" not in str((rep.get("data") or {}).get("stdout") or ""):
                raise Bridge("upload_atomic_replace_failed")
        else:
            rn = _call_js("rename_path", {"old_path": tmp_contents, "new_path": contents},
                          tab, 30, session_id) or {}
            if not rn.get("ok"):
                raise Bridge(f"upload_rename_failed: {rn.get('error')}")
    except Exception:
        try:
            _call_js("delete_path", {"path": tmp_contents}, tab, 15, session_id)
        except Exception:
            pass
        raise
    after = _remote_stat(contents, session_id, tab)
    if after["size"] != len(raw):
        raise Bridge("upload_size_mismatch")
    return {"ok": True, "path": server_path, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "transport": "chunked_http"}


def put_file(local, remote, overwrite=False, allow_large=False):
    """Upload one local file under the Contents root; no-clobber unless overwrite is explicit."""
    server_path, contents = _transfer_path(remote)
    limit = MAX_CHUNKED_UPLOAD_BYTES if allow_large else MAX_UPLOAD_BYTES
    try:
        with open(local, "rb") as fh:
            raw = fh.read(limit + 1)
    except OSError as exc:
        raise ValueError(f"local_file_read_failed: {exc}") from exc
    if len(raw) > limit:
        raise ValueError(f"upload_too_large: limit {limit} bytes")
    if allow_large and len(raw) > MAX_UPLOAD_BYTES:
        return _put_chunked_bytes(raw, remote, overwrite=overwrite)
    _write_bytes(server_path, raw, overwrite, "/data")
    after = _remote_stat(contents)
    if after["size"] != len(raw):
        raise Bridge("upload_size_mismatch")
    return {"ok": True, "path": server_path, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


_SYNC_DEFAULT_EXCLUDES = (".git", "__pycache__", ".DS_Store", "*.pyc", ".sn-sync-*.tar.gz", "__sn_sync_*.tar.gz")


def _build_sync_tar(local_dir, excludes=()):
    root = os.path.abspath(local_dir)
    if not os.path.isdir(root):
        raise ValueError("local_dir_not_found")
    patterns = tuple(_SYNC_DEFAULT_EXCLUDES) + tuple(excludes or ())
    buf = io.BytesIO()
    files = []
    total_bytes = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for base, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs
                if not os.path.islink(os.path.join(base, d))
                and not any(fnmatch.fnmatch(d, pat) for pat in patterns)
            )
            for name in sorted(names):
                if any(fnmatch.fnmatch(name, pat) for pat in patterns):
                    continue
                full = os.path.join(base, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if any(fnmatch.fnmatch(rel, pat) for pat in patterns):
                    continue
                st = os.lstat(full)
                total_bytes += st.st_size
                if total_bytes > MAX_CHUNKED_UPLOAD_BYTES * 2:
                    raise ValueError(f"sync_dir_too_large: raw bytes exceed {MAX_CHUNKED_UPLOAD_BYTES * 2}")
                tf.add(full, arcname=rel, recursive=False)
                files.append({"path": rel, "size": st.st_size})
    archive = buf.getvalue()
    if len(archive) > MAX_CHUNKED_UPLOAD_BYTES:
        raise ValueError(f"sync_archive_too_large: limit {MAX_CHUNKED_UPLOAD_BYTES} bytes")
    return archive, files, total_bytes


def sync_up(local_dir, remote_dir, excludes=(), chmod_scripts=True):
    """Bundle a local directory safely, upload atomically, and extract on the server."""
    server_dir, contents_dir = _transfer_path(remote_dir)
    if server_dir != WORK_ROOT and not server_dir.startswith(WORK_ROOT + "/"):
        raise ValueError("sync_remote_must_be_under_work_root")
    archive, files, total_bytes = _build_sync_tar(local_dir, excludes=excludes)
    archive_sha = hashlib.sha256(archive).hexdigest()
    tmp_name = f"__sn_sync_{uuid.uuid4().hex}.tar.gz"
    tmp_remote = f"{contents_dir}/{tmp_name}" if contents_dir else tmp_name
    tmp_server = posixpath.join(server_dir, tmp_name)
    _put_chunked_bytes(archive, tmp_remote, overwrite=True)
    extract_code = f'''import hashlib, json, os, stat, tarfile
dst = os.path.realpath({server_dir!r}) if os.path.exists({server_dir!r}) else {server_dir!r}
os.makedirs(dst, mode=0o700, exist_ok=True)
dst = os.path.realpath(dst)
arc = {tmp_server!r}
expected_sha = {archive_sha!r}
chmod_sh = {bool(chmod_scripts)!r}
h = hashlib.sha256()
with open(arc, 'rb') as fh:
    for blk in iter(lambda: fh.read(1048576), b''):
        h.update(blk)
if h.hexdigest() != expected_sha:
    os.unlink(arc)
    raise ValueError('sync_archive_sha256_mismatch')
extracted = 0
try:
    with tarfile.open(arc, 'r:gz') as tf:
        for m in tf.getmembers():
            if not (m.isfile() or m.isdir()):
                raise ValueError('unsafe_tar_member:' + m.name)
            norm = os.path.normpath(m.name)
            if os.path.isabs(norm) or norm.startswith('..'):
                raise ValueError('unsafe_tar_path:' + m.name)
            target = os.path.realpath(os.path.join(dst, norm))
            if os.path.commonpath((dst, target)) != dst:
                raise ValueError('tar_escape:' + m.name)
            if m.isdir():
                os.makedirs(target, mode=0o700, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                src_fh = tf.extractfile(m)
                tmp_f = target + '.sn.tmp'
                with open(tmp_f, 'wb') as out_fh:
                    out_fh.write(src_fh.read())
                mode = 0o700 if (chmod_sh and target.endswith('.sh')) else (0o700 if (m.mode & 0o111) else 0o600)
                os.chmod(tmp_f, mode)
                os.replace(tmp_f, target)
                extracted += 1
finally:
    if os.path.exists(arc):
        os.unlink(arc)
print('SYNC_JSON=' + json.dumps({{'ok': True, 'extracted': extracted, 'remote_dir': dst}}))
'''
    res = run_python(extract_code, timeout=MAX_SYNC_SECONDS)
    parsed = _pull_json((res.get("data") or {}).get("stdout"), marker="SYNC_JSON=")
    if not parsed or not parsed.get("ok"):
        raise Bridge("sync_up_extract_failed: " + str((res.get("data") or {}).get("stderr") or "")[:200])
    return {
        "ok": True,
        "remote_dir": server_dir,
        "file_count": len(files),
        "extracted": parsed.get("extracted"),
        "total_bytes": total_bytes,
        "archive_bytes": len(archive),
        "archive_sha256": archive_sha,
    }


def sync_down(remote_dir, out_dir, glob_patterns="*", overwrite=False, verify=True,
              max_files=100, skip_existing=False):
    """Download matching files from a remote directory tree under /data into a local folder."""
    server_dir, contents_dir = _transfer_path(remote_dir)
    max_files = _validate_bounded_int(max_files, 1, 500, "invalid_max_files")
    pats = [p.strip() for p in str(glob_patterns or "*").split(",") if p.strip()] or ["*"]
    walk_code = f'''import fnmatch, json, os, stat
root = os.path.realpath({server_dir!r})
pats = {pats!r}
limit = {max_files!r}
items = []
truncated = False
if not os.path.isdir(root):
    print('WALK_JSON=' + json.dumps({{'ok': False, 'error': 'remote_dir_not_found'}}))
else:
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(base, d)))
        for name in sorted(names):
            full = os.path.join(base, name)
            try:
                info = os.lstat(full)
                if not stat.S_ISREG(info.st_mode):
                    continue
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, '/')
            if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in pats):
                if len(items) >= limit:
                    truncated = True
                    break
                items.append({{'rel': rel, 'size': info.st_size}})
        if truncated:
            break
    print('WALK_JSON=' + json.dumps({{'ok': True, 'files': items, 'truncated': truncated}}))
'''
    res = run_python(walk_code, timeout=MAX_SYNC_SECONDS)
    listing = _pull_json((res.get("data") or {}).get("stdout"), marker="WALK_JSON=")
    if not listing or not listing.get("ok"):
        raise Bridge(f"sync_down_list_failed: {(listing or {}).get('error') or 'unknown'}")
    out_root = os.path.abspath(out_dir)
    os.makedirs(out_root, exist_ok=True)
    downloaded, skipped, failed = [], [], []
    total_bytes = 0
    for item in listing.get("files", []):
        rel = item["rel"]
        if rel.startswith("/") or ".." in rel.split("/"):
            continue
        local_dst = os.path.join(out_root, *rel.split("/"))
        remote_src = f"{contents_dir}/{rel}" if contents_dir else rel
        if skip_existing and os.path.isfile(local_dst) and os.path.getsize(local_dst) == item["size"]:
            skipped.append({"rel": rel, "bytes": item["size"]})
            continue
        try:
            r = get_file(remote_src, local_dst, overwrite=overwrite, verify=verify)
            total_bytes += int(r.get("bytes") or 0)
            downloaded.append({"rel": rel, "bytes": r.get("bytes"), "sha256": r.get("sha256")})
        except (Bridge, ValueError, OSError) as exc:
            failed.append({"rel": rel, "error": str(exc)[:200]})
    return {
        "ok": len(failed) == 0,
        "remote_dir": server_dir,
        "out_dir": out_root,
        "matched": len(listing.get("files", [])),
        "truncated": bool(listing.get("truncated")),
        "downloaded": len(downloaded),
        "skipped": len(skipped),
        "failed": len(failed),
        "total_bytes": total_bytes,
        "files": downloaded,
        "errors": failed,
    }


_HOST_HEALTH_PY = r'''import json, os, shutil, socket, tempfile, time
info = {"hostname": socket.gethostname(), "physical_cpus": os.cpu_count() or 1}
try:
    la = os.getloadavg()
    info["loadavg"] = {"1m": round(la[0], 2), "5m": round(la[1], 2), "15m": round(la[2], 2)}
    info["load_per_cpu"] = round(la[0] / max(1, info["physical_cpus"]), 2)
except OSError:
    info["loadavg"] = None
    info["load_per_cpu"] = None

def read_txt(p):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None

# cgroup CPU quota (v2 then v1)
cg_cpu = None
v2 = read_txt("/sys/fs/cgroup/cpu.max")
if v2:
    parts = v2.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) > 0:
        cg_cpu = round(int(parts[0]) / int(parts[1]), 2)
else:
    q = read_txt("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    p = read_txt("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if q and p and q.lstrip("-").isdigit() and p.isdigit() and int(q) > 0 and int(p) > 0:
        cg_cpu = round(int(q) / int(p), 2)
info["cgroup_cpu_quota"] = cg_cpu

mem = {}
mi = read_txt("/proc/meminfo") or ""
for line in mi.splitlines():
    for key in ("MemTotal:", "MemAvailable:"):
        if line.startswith(key):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                mem[key[:-1]] = round(int(parts[1]) / (1024 * 1024), 1)
info["mem_total_gb"] = mem.get("MemTotal")
info["mem_avail_gb"] = mem.get("MemAvailable")

for label, path in (("tmp", "/tmp"), ("work", "/data/work")):
    try:
        du = shutil.disk_usage(path)
        info[f"disk_{label}_free_gb"] = round(du.free / (1024 ** 3), 1)
    except OSError:
        info[f"disk_{label}_free_gb"] = None

def bench_write(directory, mb=16):
    t0 = time.monotonic()
    fd, path = tempfile.mkstemp(prefix=".sn-bench-", dir=directory)
    try:
        chunk = b"\0" * (1024 * 1024)
        for _ in range(mb):
            os.write(fd, chunk)
        os.fsync(fd)
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
    dt = max(0.001, time.monotonic() - t0)
    return round(mb / dt, 1)

for label, path in (("tmp", "/tmp"), ("work", "/data/work")):
    try:
        info[f"write_mb_s_{label}"] = bench_write(path, 16)
    except OSError:
        info[f"write_mb_s_{label}"] = None

lpc = info.get("load_per_cpu")
if lpc is not None and lpc >= 1.5:
    info["recommendation"] = "use_offline_dedicated_node"
    info["note"] = "Shared host CPU is heavily oversubscribed (load/cpu >= 1.5); use StereoNote for setup/sync/light checks and submit heavy multi-sample compute to dedicated offline nodes."
elif lpc is not None and lpc >= 0.9:
    info["recommendation"] = "moderate_load_limit_concurrency"
    info["note"] = "Shared host has moderate load; cap --max-concurrent and pin --threads."
else:
    info["recommendation"] = "healthy_interactive"
    info["note"] = "Host load looks healthy for interactive and small-batch runs."

print("HEALTH_JSON=" + json.dumps(info, separators=(",", ":")))
'''


def host_health(timeout=MAX_SYNC_SECONDS):
    """Inspect server CPU/cgroup/load/memory/disk and recommend interactive vs offline compute."""
    res = run_python(_HOST_HEALTH_PY, timeout=min(timeout, MAX_SYNC_SECONDS))
    parsed = _pull_json((res.get("data") or {}).get("stdout"), marker="HEALTH_JSON=")
    if not parsed:
        return {"ok": False, "error": "host_health_failed",
                "stderr": str((res.get("data") or {}).get("stderr") or "")[:MAX_INSPECT_VALUE_CHARS]}
    parsed["ok"] = True
    return parsed


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
           idempotency_key=None, threads=None, local_tmp=False, batch_id=None, sample_id=None):
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
    launcher_kwargs = {"cwd": cwd}
    if threads is not None:
        launcher_kwargs["threads"] = threads
    if local_tmp:
        launcher_kwargs["local_tmp"] = True
    if batch_id:
        launcher_kwargs["batch_id"] = batch_id
    if sample_id:
        launcher_kwargs["sample_id"] = sample_id
    launcher = build_job_launcher(jid, cmd, **launcher_kwargs)
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


def _tail_lines(text, count):
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    return "".join(lines[-count:]) if len(lines) > count else text


def _parse_heartbeat_age(hb_obj):
    if not isinstance(hb_obj, dict):
        return None
    epoch = hb_obj.get("epoch")
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
        return max(0, int(time.time() - epoch))
    return None


def poll_http(job_id, tail=40):
    """Kernel-free pure-HTTP poll via batch_http + HTTP Range log tails.

    Because no kernel is invoked, this remains fast even under extreme host CPU
    load, while honestly reporting `process='http_unverified'` and heartbeat age
    for running jobs instead of claiming PID liveness.
    """
    job_id = validate_job_id(job_id)
    tail = validate_tail(tail)
    jcontents = to_contents(f"{JOB_ROOT}/{job_id}")
    reqs = [
        {"id": "status", "kind": "read", "path": f"{jcontents}/status.json", "max_bytes": MAX_STATUS_BYTES},
        {"id": "stage", "kind": "read", "path": f"{jcontents}/stage.json", "max_bytes": MAX_STATUS_BYTES},
        {"id": "heartbeat", "kind": "read", "path": f"{jcontents}/heartbeat.json", "max_bytes": MAX_STATUS_BYTES},
        {"id": "stdout", "kind": "read_range", "path": f"{jcontents}/stdout.log", "mode": "tail", "max_bytes": MAX_LOG_BYTES},
        {"id": "stderr", "kind": "read_range", "path": f"{jcontents}/stderr.log", "mode": "tail", "max_bytes": MAX_LOG_BYTES},
    ]
    res = evaluate_op("batch_http", {"requests": reqs}, timeout=30)
    results = (res.get("data") or {}).get("results") or {}
    errors = {}
    missing = {}

    status = None
    st_rec = results.get("status") or {}
    if st_rec.get("ok") and isinstance(st_rec.get("content"), str):
        missing["status"] = False
        try:
            parsed = json.loads(st_rec["content"])
            status = parsed if isinstance(parsed, dict) else None
            if status is None:
                errors["status"] = "status_parse_error"
        except (TypeError, ValueError):
            errors["status"] = "status_parse_error"
    elif st_rec.get("status") == 404:
        missing["status"] = True
    else:
        missing["status"] = False
        errors["status"] = str(st_rec.get("error") or "status_read_error")

    stage = None
    stage_rec = results.get("stage") or {}
    if stage_rec.get("ok") and isinstance(stage_rec.get("content"), str):
        try:
            parsed = json.loads(stage_rec["content"])
            if isinstance(parsed, dict):
                stage = parsed
        except (TypeError, ValueError):
            pass

    hb = None
    hb_rec = results.get("heartbeat") or {}
    if hb_rec.get("ok") and isinstance(hb_rec.get("content"), str):
        try:
            parsed = json.loads(hb_rec["content"])
            if isinstance(parsed, dict):
                hb = parsed
        except (TypeError, ValueError):
            pass

    tails = {}
    for key in ("stdout", "stderr"):
        rec = results.get(key) or {}
        if rec.get("ok") and isinstance(rec.get("text"), str):
            missing[key] = False
            tails[key] = _tail_lines(rec["text"], tail)
        elif rec.get("status") == 404 or rec.get("error") == "not_found":
            missing[key] = True
            tails[key] = ""
        else:
            missing[key] = False
            tails[key] = ""
            errors[key] = str(rec.get("error") or f"{key}_read_error")

    job_present = not (missing.get("status") and missing.get("stdout") and missing.get("stderr"))
    missing["dir"] = not job_present
    missing["live"] = False
    receipt = _read_receipt(job_id)
    local_state = (receipt or {}).get("state")
    if status is not None:
        raw_state = status.get("state") or "unknown"
        outcome = "running_unverified" if raw_state == "running" else raw_state
    elif job_present:
        outcome = "starting"
    elif local_state in ("pending", "setup_failed"):
        outcome = "not_dispatched"
    else:
        outcome = "unknown"
    pid = status.get("pid") if isinstance(status, dict) else None
    out = {
        "job_id": job_id, "tail": tail, "mode": "http_lite",
        "status": status, "stage": stage,
        "heartbeat_age_sec": _parse_heartbeat_age(hb),
        "stdout_tail": tails["stdout"], "stderr_tail": tails["stderr"],
        "missing": missing, "read_errors": errors,
        "job_dir_present": job_present, "local_state": local_state,
        "outcome": outcome, "process": "http_unverified", "pid": pid,
    }
    return out


def poll(job_id, tail=40, lite=False):
    """Return durable state and bounded log tails as structured controller data."""
    if lite:
        return poll_http(job_id, tail=tail)
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


_PRUNE_JOBS = r'''
import datetime, json, os, re, shutil, stat, time
root = %(root)r
cutoff = time.time() - %(days)d * 86400
dry_run = %(dry)r
job_pattern = re.compile(%(job_pattern)r)
batch_filter = %(batch_filter)r
result = {'ok': True, 'removed': 0, 'eligible': 0, 'skipped': 0,
          'freed_bytes': None, 'job_ids': [], 'job_ids_truncated': False, 'errors': []}

def read_regular(path, limit):
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise ValueError('not_regular_file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('not_regular_file')
        value = os.read(fd, limit + 1)
        if len(value) > limit:
            raise ValueError('record_too_large')
        return value.decode('utf-8')
    finally:
        os.close(fd)

def eligible(path, name):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or not job_pattern.fullmatch(name):
        return None
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(root):
        return None
    status = json.loads(read_regular(os.path.join(path, 'status.json'), 4096))
    manifest = json.loads(read_regular(os.path.join(path, 'manifest.json'), 512 * 1024))
    if (not isinstance(status, dict) or not isinstance(manifest, dict) or
            status.get('job_id') != name or manifest.get('job_id') != name or
            status.get('state') != 'finished'):
        return None
    if batch_filter is not None and manifest.get('batch_id') != batch_filter:
        return None
    ended = datetime.datetime.strptime(status['ended_at'], '%%Y-%%m-%%dT%%H:%%M:%%SZ')
    if ended.replace(tzinfo=datetime.timezone.utc).timestamp() >= cutoff:
        return None
    pid = int(read_regular(os.path.join(path, 'pid'), 32).strip())
    if pid <= 1:
        return None
    # A dead runner can leave live children in its process group.
    for check in (os.kill, getattr(os, 'killpg', os.kill)):
        try:
            check(pid, 0)
            return None
        except ProcessLookupError:
            pass
        except OSError:
            return None
    return info.st_dev, info.st_ino

try:
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise ValueError('unsafe_root')
    with os.scandir(root) as entries:
        for entry in entries:
            try:
                identity = eligible(entry.path, entry.name)
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                identity = None
            if identity is None:
                result['skipped'] += 1
                continue
            result['eligible'] += 1
            if len(result['job_ids']) < 100:
                result['job_ids'].append(entry.name)
            else:
                result['job_ids_truncated'] = True
            if not dry_run:
                try:
                    if eligible(entry.path, entry.name) != identity:
                        result['skipped'] += 1
                        continue
                    shutil.rmtree(entry.path)
                    result['removed'] += 1
                except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
                    result['ok'] = False
                    result['error'] = 'prune_partial_failure'
                    if len(result['errors']) < 20:
                        result['errors'].append({'job_id': entry.name, 'error': str(exc)[:256]})
    result['remaining'] = sum(1 for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False))
except (OSError, ValueError) as exc:
    result.update(ok=False, error='prune_scan_failed', detail=str(exc)[:256])
print('PRUNE_JSON=' + json.dumps(result, separators=(',', ':')))
'''


def prune_jobs(days=7, dry_run=False, batch_id=None):
    """Delete finished job directories older than `days`, never a live owner."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
        raise ValueError("invalid_days: use 1-3650")
    if batch_id is not None:
        batch_id = validate_batch_id(batch_id)
    code = _PRUNE_JOBS % {"root": JOB_ROOT, "days": days, "dry": bool(dry_run),
                          "job_pattern": _JOB_ID_RE.pattern, "batch_filter": batch_id}
    cmd = "python3 -c " + shlex.quote(code)
    res = run_shell(cmd, cwd=WORK_ROOT, timeout=MAX_SYNC_SECONDS,
                    max_bytes=MAX_INTERNAL_SHELL_BYTES)
    out = str(res.get("stdout") or "")
    line = next((item for item in out.splitlines() if item.startswith("PRUNE_JSON=")), None)
    if res.get("timedOut"):
        return {"ok": False, "error": "prune_timeout",
                "detail": f"operation exceeded {MAX_SYNC_SECONDS}s; deletion may be partial; inspect jobs before retrying"}
    if line is None or res.get("ok") is not True or res.get("returncode") != 0:
        return {"ok": False, "error": "prune_protocol_error",
                "stderr": (res.get("stderr") or "")[:MAX_INSPECT_VALUE_CHARS]}
    try:
        result = json.loads(line[len("PRUNE_JSON="):])
    except ValueError:
        return {"ok": False, "error": "prune_protocol_error"}
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        return {"ok": False, "error": "prune_protocol_error"}
    result.update(days=days, dry_run=bool(dry_run), root=JOB_ROOT)
    if batch_id is not None:
        result["batch_id"] = batch_id
    return result


# ─────────────────────────── server-side batch queue engine ───────────────────────────
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TEMPLATE_KEY_RE = re.compile(r"\{([A-Za-z0-9_.-]+)\}")
MAX_BATCH_SAMPLES = 500


def validate_batch_id(value):
    if not isinstance(value, str) or not _BATCH_ID_RE.fullmatch(value):
        raise ValueError("invalid_batch_id: use [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return value


def validate_sample_id(value):
    if not isinstance(value, str) or not _SAMPLE_ID_RE.fullmatch(value):
        raise ValueError(f"invalid_sample_id: {value!r}")
    return value


def parse_batch_manifest(manifest_path, cmd_template):
    """Parse a local TSV/CSV/JSON sample manifest and safely render shell commands."""
    raw = read_local_text(manifest_path, MAX_WRITE_BYTES, "manifest_too_large")
    ext = os.path.splitext(manifest_path)[1].lower()
    if ext == ".json":
        try:
            rows = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"invalid_manifest_json: {exc}") from exc
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError("manifest_json_must_be_list_of_objects")
    else:
        delimiter = "," if ext == ".csv" else "\t"
        reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError("manifest_missing_header")
        rows = [dict(r) for r in reader if any(str(v or "").strip() for v in r.values())]
    if not rows:
        raise ValueError("manifest_empty")
    if len(rows) > MAX_BATCH_SAMPLES:
        raise ValueError(f"manifest_too_many_samples: limit {MAX_BATCH_SAMPLES}")
    needed_keys = _TEMPLATE_KEY_RE.findall(cmd_template)
    seen = set()
    samples = []
    for idx, row in enumerate(rows):
        clean = {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in row.items() if k is not None}
        sid = clean.get("sample_id") or clean.get("sample") or clean.get("id")
        if not sid:
            raise ValueError(f"manifest_row_{idx + 1}_missing_sample_id")
        sid = validate_sample_id(sid)
        if sid in seen:
            raise ValueError(f"duplicate_sample_id: {sid}")
        seen.add(sid)
        clean["sample_id"] = sid
        missing_keys = [k for k in needed_keys if k not in clean]
        if missing_keys:
            raise ValueError(f"manifest_missing_template_keys: {','.join(missing_keys)}")
        rendered = _TEMPLATE_KEY_RE.sub(lambda m: shlex.quote(clean[m.group(1)]), cmd_template)
        validate_text_payload(rendered, MAX_COMMAND_BYTES, "rendered_command_too_large")
        samples.append({"sample_id": sid, "cmd": rendered, "row": clean})
    return samples


def _make_sample_job_id(batch_id, sample_id, attempt):
    identity = json.dumps([*_workspace_ids(WORKSPACE_URL), "batch", batch_id, sample_id, int(attempt)])
    return f"job_{uuid.uuid5(uuid.NAMESPACE_URL, 'stereonote-batch:' + identity)}"


_BATCH_DAEMON_PY = r'''#!/usr/bin/env python3
"""Server-side detached batch queue daemon for Orange Note v5."""
import hashlib, json, os, shlex, signal, stat, subprocess, sys, time

BATCH_DIR = sys.argv[1]
JOB_ROOT = sys.argv[2]
CONTROLLER_JOB_ID = sys.argv[3]
MANIFEST_PATH = os.path.join(BATCH_DIR, "batch_manifest.json")
STATUS_PATH = os.path.join(BATCH_DIR, "batch_status.json")
SAMPLES_DIR = os.path.join(BATCH_DIR, "samples")
os.makedirs(SAMPLES_DIR, mode=0o700, exist_ok=True)


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write_json(path, obj):
    tmp = path + ".tmp." + str(os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def make_sample_runner(job_id, command, cwd, batch_id, sample_id, threads, local_tmp):
    jdir = os.path.join(JOB_ROOT, job_id)
    os.makedirs(os.path.join(jdir, "artifacts"), mode=0o700, exist_ok=True)
    os.chmod(jdir, 0o700)
    for fn in ("stdout.log", "stderr.log"):
        p = os.path.join(jdir, fn)
        with open(p, "wb"):
            pass
        os.chmod(p, 0o600)
    manifest = {
        "schema_version": 2, "job_id": job_id, "command": command, "cwd": cwd,
        "batch_id": batch_id, "sample_id": sample_id,
    }
    if threads is not None:
        manifest["threads"] = threads
    if local_tmp:
        manifest["local_tmp"] = True
    atomic_write_json(os.path.join(jdir, "manifest.json"), manifest)
    atomic_write_json(os.path.join(jdir, "status.json"), {"state": "queued", "job_id": job_id})
    cmd_path = os.path.join(jdir, "cmd.sh")
    with open(cmd_path, "w", encoding="utf-8") as fh:
        fh.write(command + "\n")
    os.chmod(cmd_path, 0o600)

    qdir = shlex.quote(jdir)
    qcwd = shlex.quote(cwd)
    qjob = shlex.quote(json.dumps(job_id))
    env_lines = []
    if threads is not None:
        env_lines.append(
            f"export OPENBLAS_NUM_THREADS={threads} OMP_NUM_THREADS={threads} "
            f"MKL_NUM_THREADS={threads} NUMEXPR_NUM_THREADS={threads}"
        )
    tmp_setup = ""
    tmp_cleanup = ""
    if local_tmp:
        tdir = shlex.quote(f"/tmp/sn_job_{job_id}")
        tmp_setup = f"mkdir -p {tdir} && chmod 700 {tdir}\nexport TMPDIR={tdir} SN_TMPDIR={tdir}"
        tmp_cleanup = f"  rm -rf {tdir} 2>/dev/null || true"
    env_block = "\n".join(x for x in (*env_lines, tmp_setup) if x)
    runner = f"""#!/usr/bin/env bash
set +e
umask 077
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\\n' "$$" > {qdir}/pid.tmp.$$ && mv {qdir}/pid.tmp.$$ {qdir}/pid
chmod 600 {qdir}/pid
printf '{{"state":"running","job_id":%s,"pid":%s,"started_at":"%s"}}\\n' {qjob} "$$" "$started_at" > {qdir}/status.json.tmp.$$
mv {qdir}/status.json.tmp.$$ {qdir}/status.json
chmod 600 {qdir}/status.json
printf '{{"job_id":%s,"pid":%s,"ts":"%s","epoch":%s}}\\n' {qjob} "$$" "$started_at" "$(date -u +%s)" > {qdir}/heartbeat.json.tmp.$$
mv {qdir}/heartbeat.json.tmp.$$ {qdir}/heartbeat.json
chmod 600 {qdir}/heartbeat.json
sn_stage() {{
  local _s="${{1:-unknown}}" _d="${{2:-}}" _t="${{3:-}}" _n="${{4:-}}"
  python3 -c 'import json, os, sys, time
p, s, d, t, n = sys.argv[1:6]
obj = {{"stage": s, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}
if d.isdigit(): obj["done"] = int(d)
if t.isdigit(): obj["total"] = int(t)
if n: obj["note"] = n[:256]
tmp = p + ".tmp." + str(os.getpid())
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(obj, fh, ensure_ascii=False); fh.write("\\n")
os.chmod(tmp, 0o600); os.replace(tmp, p)' {qdir}/stage.json "$_s" "$_d" "$_t" "$_n" 2>/dev/null || true
}}
export -f sn_stage
_SN_HB_PID=""
(
  _sp=""
  trap '[ -n "$_sp" ] && kill "$_sp" 2>/dev/null || true; exit 0' TERM INT
  while true; do
    sleep 60 &
    _sp=$!
    wait "$_sp" 2>/dev/null || break
    _now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    _ep=$(date -u +%s)
    printf '{{"job_id":%s,"pid":%s,"ts":"%s","epoch":%s}}\\n' {qjob} "$$" "$_now" "$_ep" > {qdir}/heartbeat.json.tmp.$$ 2>/dev/null && mv {qdir}/heartbeat.json.tmp.$$ {qdir}/heartbeat.json 2>/dev/null || true
  done
) </dev/null >/dev/null 2>&1 &
_SN_HB_PID=$!
finalize() {{
  rc=$?
  trap - EXIT TERM INT
  if [ -n "$_SN_HB_PID" ]; then kill "$_SN_HB_PID" 2>/dev/null || true; wait "$_SN_HB_PID" 2>/dev/null || true; fi
{tmp_cleanup}
  ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '%s\\n' "$ended_at" > {qdir}/ended_at.tmp.$$ && mv {qdir}/ended_at.tmp.$$ {qdir}/ended_at
  printf '%s\\n' "$rc" > {qdir}/exit_code.tmp.$$ && mv {qdir}/exit_code.tmp.$$ {qdir}/exit_code
  printf '{{"state":"finished","job_id":%s,"pid":%s,"started_at":"%s","ended_at":"%s","exit_code":%s}}\\n' {qjob} "$$" "$started_at" "$ended_at" "$rc" > {qdir}/status.json.tmp.$$
  mv {qdir}/status.json.tmp.$$ {qdir}/status.json
  chmod 600 {qdir}/status.json {qdir}/ended_at {qdir}/exit_code
  exit "$rc"
}}
trap finalize EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
export SN_JOB_DIR={qdir}
export SN_ARTIFACT_DIR={qdir}/artifacts
export SN_BATCH_ID={shlex.quote(batch_id)}
export SN_SAMPLE_ID={shlex.quote(sample_id)}
{env_block}
cd {qcwd} || exit 72
bash {qdir}/cmd.sh > {qdir}/stdout.log 2> {qdir}/stderr.log
"""
    runner_path = os.path.join(jdir, "runner.sh")
    with open(runner_path, "w", encoding="utf-8") as fh:
        fh.write(runner)
    os.chmod(runner_path, 0o700)
    return jdir, runner_path


manifest = read_json(MANIFEST_PATH)
if not manifest:
    sys.stderr.write("batch_manifest_missing\n")
    sys.exit(2)

batch_id = manifest["batch_id"]
cwd = manifest["cwd"]
max_concurrent = max(1, int(manifest.get("max_concurrent") or 1))
threads = manifest.get("threads")
local_tmp = bool(manifest.get("local_tmp"))
generation = int(manifest.get("generation") or 1)

prev_status = read_json(STATUS_PATH) or {}
prev_samples = prev_status.get("samples") if isinstance(prev_status.get("samples"), dict) else {}

sample_states = {}
queue = []
for item in manifest.get("samples", []):
    sid = item["sample_id"]
    old = prev_samples.get(sid) or {}
    if old.get("state") == "finished" and old.get("exit_code") == 0 and not item.get("force_rerun"):
        sample_states[sid] = dict(old)
    else:
        sample_states[sid] = {
            "sample_id": sid,
            "job_id": item["job_id"],
            "attempt": int(item.get("attempt") or 1),
            "state": "queued",
            "exit_code": None,
            "started_at": None,
            "ended_at": None,
            "stage": None,
        }
        queue.append(item)

stop_requested = False


def on_signal(signum, frame):
    global stop_requested
    stop_requested = True


signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)

active = {}  # sid -> (Popen, jdir, item)


def publish(batch_state):
    counts = {"total": len(sample_states), "queued": 0, "running": 0,
              "finished": 0, "failed": 0, "cancelled": 0}
    for sid, st in sample_states.items():
        if st["state"] == "running":
            stage = read_json(os.path.join(JOB_ROOT, st["job_id"], "stage.json"))
            if stage:
                st["stage"] = stage
        bucket = st["state"] if st["state"] in counts else "failed"
        counts[bucket] += 1
        atomic_write_json(os.path.join(SAMPLES_DIR, f"{sid}.json"), st)
    payload = {
        "schema_version": 1,
        "batch_id": batch_id,
        "generation": generation,
        "controller_job_id": CONTROLLER_JOB_ID,
        "state": batch_state,
        "max_concurrent": max_concurrent,
        "updated_at": utc_now(),
        "counts": counts,
        "samples": sample_states,
    }
    atomic_write_json(STATUS_PATH, payload)
    return counts


publish("running")

while queue or active:
    if stop_requested:
        for sid, (proc, jdir, item) in list(active.items()):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass
        deadline = time.monotonic() + 8.0
        while active and time.monotonic() < deadline:
            for sid, (proc, jdir, item) in list(active.items()):
                rc = proc.poll()
                if rc is not None:
                    active.pop(sid, None)
                    sample_states[sid]["state"] = "cancelled"
                    sample_states[sid]["exit_code"] = rc
                    sample_states[sid]["ended_at"] = utc_now()
            if active:
                time.sleep(0.25)
        for sid, (proc, jdir, item) in list(active.items()):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            sample_states[sid]["state"] = "cancelled"
            sample_states[sid]["ended_at"] = utc_now()
        active.clear()
        for item in queue:
            sid = item["sample_id"]
            sample_states[sid]["state"] = "cancelled"
            sample_states[sid]["ended_at"] = utc_now()
        queue.clear()
        publish("cancelled")
        sys.exit(143)

    for sid, (proc, jdir, item) in list(active.items()):
        rc = proc.poll()
        if rc is not None:
            active.pop(sid, None)
            st = sample_states[sid]
            st["exit_code"] = rc
            st["ended_at"] = utc_now()
            st["state"] = "finished" if rc == 0 else "failed"
            stage = read_json(os.path.join(jdir, "stage.json"))
            if stage:
                st["stage"] = stage

    while queue and len(active) < max_concurrent and not stop_requested:
        item = queue.pop(0)
        sid = item["sample_id"]
        jdir, runner_path = make_sample_runner(
            item["job_id"], item["cmd"], cwd, batch_id, sid, threads, local_tmp
        )
        launcher_log = open(os.path.join(jdir, "launcher.log"), "ab")
        proc = subprocess.Popen(
            ["bash", runner_path],
            stdin=subprocess.DEVNULL,
            stdout=launcher_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        launcher_log.close()
        sample_states[sid]["state"] = "running"
        sample_states[sid]["started_at"] = utc_now()
        sample_states[sid]["pid"] = proc.pid
        active[sid] = (proc, jdir, item)

    publish("running")
    if queue or active:
        time.sleep(1.5)

counts = publish("finished")
sys.exit(0 if counts["failed"] == 0 and counts["cancelled"] == 0 else 1)
'''


def _batch_dir(batch_id):
    return f"{BATCH_ROOT}/{validate_batch_id(batch_id)}"


def batch_submit(batch_id, manifest_path, cmd_template, cwd=None, max_concurrent=2,
                 threads=None, local_tmp=False):
    """Upload a manifest + server-side queue daemon and submit it as a detached job."""
    batch_id = validate_batch_id(batch_id)
    max_concurrent = _validate_bounded_int(max_concurrent, 1, 64, "invalid_max_concurrent")
    if threads is not None:
        threads = _validate_bounded_int(threads, 1, 256, "invalid_threads")
    execution_cwd = to_server(cwd or WORK_ROOT)
    samples = parse_batch_manifest(manifest_path, cmd_template)
    bdir = _batch_dir(batch_id)
    manifest_obj = {
        "schema_version": 1,
        "batch_id": batch_id,
        "generation": 1,
        "created_at": _utc_now(),
        "cwd": execution_cwd,
        "max_concurrent": max_concurrent,
        "threads": threads,
        "local_tmp": bool(local_tmp),
        "cmd_template": cmd_template,
        "samples": [
            {
                "sample_id": s["sample_id"],
                "cmd": s["cmd"],
                "attempt": 1,
                "job_id": _make_sample_job_id(batch_id, s["sample_id"], 1),
            }
            for s in samples
        ],
    }
    write_text(f"{bdir}/batch_manifest.json",
               json.dumps(manifest_obj, ensure_ascii=False, indent=2) + "\n",
               overwrite=False)
    write_text(f"{bdir}/queue_daemon.py", _BATCH_DAEMON_PY, overwrite=True)
    cmd = (
        f"python3 {shlex.quote(bdir + '/queue_daemon.py')} "
        f"{shlex.quote(bdir)} {shlex.quote(JOB_ROOT)} \"$SN_JOB_DIR\""
    )
    # Pass controller job_id (basename of SN_JOB_DIR) to daemon
    cmd = (
        f"python3 {shlex.quote(bdir + '/queue_daemon.py')} "
        f"{shlex.quote(bdir)} {shlex.quote(JOB_ROOT)} \"$(basename \"$SN_JOB_DIR\")\""
    )
    job = submit(
        cmd,
        name="batch",
        cwd=WORK_ROOT,
        idempotency_key=f"batch:{batch_id}:gen1",
        batch_id=batch_id,
    )
    return {
        "ok": True,
        "batch_id": batch_id,
        "generation": 1,
        "batch_dir": bdir,
        "controller_job_id": job["job_id"],
        "reused": bool(job.get("reused")),
        "max_concurrent": max_concurrent,
        "sample_count": len(samples),
        "samples": [{"sample_id": s["sample_id"], "job_id": s["job_id"]} for s in manifest_obj["samples"]],
    }


def batch_status(batch_id, include_logs=False, tail_bytes=2048):
    """Kernel-free pure-HTTP batch dashboard (reads batch_status + failed logs in 1-2 HTTP ops)."""
    batch_id = validate_batch_id(batch_id)
    tail_bytes = _validate_bounded_int(tail_bytes, 256, MAX_LOG_BYTES, "invalid_tail_bytes")
    bcontents = to_contents(_batch_dir(batch_id))
    res = evaluate_op("read", {"path": f"{bcontents}/batch_status.json",
                               "max_bytes": MAX_INSPECT_BYTES}, timeout=30)
    data = res.get("data") or {}
    if not data.get("ok") or not isinstance(data.get("content"), str):
        return {"ok": False, "batch_id": batch_id, "error": "batch_status_not_found"}
    try:
        st = json.loads(data["content"])
    except ValueError:
        return {"ok": False, "batch_id": batch_id, "error": "batch_status_parse_error"}
    controller_job_id = st.get("controller_job_id")
    samples = st.get("samples") if isinstance(st.get("samples"), dict) else {}
    reqs = []
    if controller_job_id:
        cj = to_contents(f"{JOB_ROOT}/{controller_job_id}")
        reqs.append({"id": "__ctrl_status", "kind": "read", "path": f"{cj}/status.json", "max_bytes": MAX_STATUS_BYTES})
        reqs.append({"id": "__ctrl_hb", "kind": "read", "path": f"{cj}/heartbeat.json", "max_bytes": MAX_STATUS_BYTES})
    log_targets = []
    for sid, info in samples.items():
        if info.get("state") in ("failed", "cancelled") or (include_logs and info.get("state") == "running"):
            if info.get("job_id") and len(log_targets) < 16:
                log_targets.append((sid, info["job_id"]))
                sj = to_contents(f"{JOB_ROOT}/{info['job_id']}")
                reqs.append({"id": f"err:{sid}", "kind": "read_range",
                             "path": f"{sj}/stderr.log", "mode": "tail", "max_bytes": tail_bytes})
    ctrl_status = None
    ctrl_hb_age = None
    failed_stderr = {}
    if reqs:
        bres = evaluate_op("batch_http", {"requests": reqs}, timeout=30)
        bmap = (bres.get("data") or {}).get("results") or {}
        cs = bmap.get("__ctrl_status") or {}
        if cs.get("ok") and isinstance(cs.get("content"), str):
            try:
                ctrl_status = json.loads(cs["content"])
            except ValueError:
                pass
        chb = bmap.get("__ctrl_hb") or {}
        if chb.get("ok") and isinstance(chb.get("content"), str):
            try:
                ctrl_hb_age = _parse_heartbeat_age(json.loads(chb["content"]))
            except ValueError:
                pass
        for sid, _ in log_targets:
            rec = bmap.get(f"err:{sid}") or {}
            if rec.get("ok") and isinstance(rec.get("text"), str) and rec["text"].strip():
                failed_stderr[sid] = rec["text"]

    state = st.get("state") or "unknown"
    if state == "running" and isinstance(ctrl_status, dict) and ctrl_status.get("state") == "finished":
        # Avoid a 1-tick read race if the daemon finished between reading batch_status and ctrl_status.
        reread = evaluate_op("read", {"path": f"{bcontents}/batch_status.json",
                                      "max_bytes": MAX_INSPECT_BYTES}, timeout=15)
        rdata = (reread or {}).get("data") or {}
        if rdata.get("ok") and isinstance(rdata.get("content"), str):
            try:
                st2 = json.loads(rdata["content"])
                if isinstance(st2, dict) and st2.get("state") != "running":
                    st = st2
                    state = st2.get("state") or state
                    samples = st2.get("samples") if isinstance(st2.get("samples"), dict) else samples
            except ValueError:
                pass
        if state == "running":
            state = "stale_daemon"
    counts = st.get("counts") or {}
    return {
        "ok": True,
        "batch_id": batch_id,
        "generation": st.get("generation"),
        "state": state,
        "controller_job_id": controller_job_id,
        "controller_state": (ctrl_status or {}).get("state"),
        "controller_heartbeat_age_sec": ctrl_hb_age,
        "max_concurrent": st.get("max_concurrent"),
        "updated_at": st.get("updated_at"),
        "counts": counts,
        "samples": samples,
        "stderr_tails": failed_stderr,
    }


def batch_retry_failed(batch_id, max_concurrent=None):
    """Re-queue only failed/cancelled samples of an existing batch; skip finished samples."""
    batch_id = validate_batch_id(batch_id)
    bdir = _batch_dir(batch_id)
    bcontents = to_contents(bdir)
    bres = evaluate_op("batch_http", {"requests": [
        {"id": "manifest", "kind": "read", "path": f"{bcontents}/batch_manifest.json", "max_bytes": MAX_INSPECT_BYTES},
        {"id": "status", "kind": "read", "path": f"{bcontents}/batch_status.json", "max_bytes": MAX_INSPECT_BYTES},
    ]}, timeout=30)
    bmap = (bres.get("data") or {}).get("results") or {}
    m_rec = bmap.get("manifest") or {}
    s_rec = bmap.get("status") or {}
    if not m_rec.get("ok") or not isinstance(m_rec.get("content"), str):
        raise Bridge("batch_manifest_not_found")
    manifest = json.loads(m_rec["content"])
    status = json.loads(s_rec["content"]) if (s_rec.get("ok") and isinstance(s_rec.get("content"), str)) else {}
    cur_samples = status.get("samples") if isinstance(status.get("samples"), dict) else {}
    if status.get("state") == "running":
        ctrl_id = status.get("controller_job_id")
        if ctrl_id:
            cp = poll_http(ctrl_id, tail=5)
            if (cp.get("status") or {}).get("state") == "running" and (cp.get("heartbeat_age_sec") or 0) < 180:
                raise Bridge(f"batch_still_running: controller_job_id={ctrl_id}")
    retried = []
    for item in manifest.get("samples", []):
        sid = item["sample_id"]
        st = cur_samples.get(sid) or {}
        if st.get("state") == "finished" and st.get("exit_code") == 0:
            item["force_rerun"] = False
            continue
        next_attempt = int(item.get("attempt") or 1) + 1
        item["attempt"] = next_attempt
        item["job_id"] = _make_sample_job_id(batch_id, sid, next_attempt)
        item["force_rerun"] = True
        retried.append({"sample_id": sid, "attempt": next_attempt, "job_id": item["job_id"]})
    if not retried:
        return {"ok": True, "batch_id": batch_id, "retried": 0, "note": "no_failed_samples"}
    next_gen = int(manifest.get("generation") or 1) + 1
    manifest["generation"] = next_gen
    if max_concurrent is not None:
        manifest["max_concurrent"] = _validate_bounded_int(max_concurrent, 1, 64, "invalid_max_concurrent")
    write_text(f"{bdir}/batch_manifest.json",
               json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
               overwrite=True)
    write_text(f"{bdir}/queue_daemon.py", _BATCH_DAEMON_PY, overwrite=True)
    cmd = (
        f"python3 {shlex.quote(bdir + '/queue_daemon.py')} "
        f"{shlex.quote(bdir)} {shlex.quote(JOB_ROOT)} \"$(basename \"$SN_JOB_DIR\")\""
    )
    job = submit(
        cmd,
        name="batch",
        cwd=WORK_ROOT,
        idempotency_key=f"batch:{batch_id}:gen{next_gen}",
        batch_id=batch_id,
    )
    return {
        "ok": True,
        "batch_id": batch_id,
        "generation": next_gen,
        "controller_job_id": job["job_id"],
        "retried": len(retried),
        "samples": retried,
    }


def batch_cancel(batch_id):
    """Cancel a running batch queue daemon (forwards SIGTERM to active sample workers)."""
    st = batch_status(batch_id)
    if not st.get("ok"):
        return st
    ctrl_id = st.get("controller_job_id")
    if not ctrl_id:
        return {"ok": False, "batch_id": batch_id, "error": "missing_controller_job_id"}
    res = cancel_job(ctrl_id)
    res["batch_id"] = batch_id
    return res


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
# File size comes from stat: counting newlines meant reading the whole file, which
# timed out on multi-GB CSV for a field nothing consumed.
size_bytes=os.path.getsize(p)
selected=list(itertools.islice(df.columns, MAX_ITEMS))
rows=[]
for record in df.loc[:, selected].head(5).to_dict(orient="records"):
    rows.append({clip(k): clip(v) for k, v in itertools.islice(record.items(), MAX_ITEMS)})
out={"format":"table","path":p,"sampled_rows":int(len(df)),"size_bytes":size_bytes,
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

FASTQ_INSPECT = _INSPECT_PRELUDE + r'''
import gzip, statistics
p = safe_path(%(p)r)
size_bytes = os.path.getsize(p)
is_gz = p.lower().endswith(".gz")
opener = (lambda path: gzip.open(path, "rt", encoding="ascii", errors="replace")) if is_gz else (lambda path: open(path, "r", encoding="ascii", errors="replace"))
lengths, qmeans, ids = [], [], []
with opener(p) as fh:
    for _ in range(500):
        h = fh.readline()
        if not h: break
        seq = fh.readline().strip()
        fh.readline()
        qual = fh.readline().strip()
        if not seq: break
        lengths.append(len(seq))
        if len(ids) < 3:
            ids.append(clip(h.strip().split()[0]))
        if qual:
            qmeans.append(sum(ord(c) - 33 for c in qual) / max(1, len(qual)))
out = {
    "format": "fastq", "path": p, "size_bytes": size_bytes, "compressed": is_gz,
    "sampled_reads": len(lengths), "first_read_ids": ids,
    "length_min": min(lengths) if lengths else None,
    "length_median": int(statistics.median(lengths)) if lengths else None,
    "length_mean": round(sum(lengths) / len(lengths), 1) if lengths else None,
    "length_max": max(lengths) if lengths else None,
    "mean_qscore": round(sum(qmeans) / len(qmeans), 2) if qmeans else None,
}
emit_summary(out)
'''

BAM_INSPECT = _INSPECT_PRELUDE + r'''
import shutil, subprocess
p = safe_path(%(p)r)
size_bytes = os.path.getsize(p)
has_bai = os.path.isfile(p + ".bai") or os.path.isfile(os.path.splitext(p)[0] + ".bai")
out = {"format": "bam", "path": p, "size_bytes": size_bytes, "has_bai": has_bai}
try:
    import pysam
    with pysam.AlignmentFile(p, "rb") as bam:
        hdr = bam.header.to_dict()
        out["n_references"] = len(hdr.get("SQ") or [])
        out["first_refs"] = [clip(r.get("SN")) for r in (hdr.get("SQ") or [])[:10]]
        out["programs"] = [clip(pg.get("ID") or pg.get("PN")) for pg in (hdr.get("PG") or [])[:10]]
        out["read_groups"] = [clip(rg.get("ID")) for rg in (hdr.get("RG") or [])[:10]]
        n = sec = supp = unm = 0
        lens = []
        for aln in bam.fetch(until_eof=True):
            n += 1
            if aln.is_unmapped: unm += 1
            if aln.is_secondary: sec += 1
            if aln.is_supplementary: supp += 1
            if aln.query_length: lens.append(aln.query_length)
            if n >= 2000: break
        out.update({"sampled_records": n, "unmapped": unm, "secondary_0x100": sec,
                    "supplementary_0x800": supp,
                    "mean_query_len": round(sum(lens)/len(lens), 1) if lens else None,
                    "backend": "pysam"})
except Exception:
    samtools = shutil.which("samtools")
    if not samtools:
        out["error"] = "pysam_and_samtools_unavailable"
    else:
        hdr = subprocess.run([samtools, "view", "-H", p], capture_output=True, text=True, timeout=20)
        sq = [l.split("\t")[1].replace("SN:", "") for l in hdr.stdout.splitlines() if l.startswith("@SQ") and "\t" in l]
        pg = [clip(l) for l in hdr.stdout.splitlines() if l.startswith("@PG")][:10]
        out["n_references"] = len(sq)
        out["first_refs"] = [clip(x) for x in sq[:10]]
        out["programs"] = pg
        proc = subprocess.Popen([samtools, "view", p], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        n = sec = supp = unm = 0
        lens = []
        try:
            for line in proc.stdout:
                parts = line.split("\t", 11)
                if len(parts) < 10: continue
                flag = int(parts[1])
                n += 1
                if flag & 4: unm += 1
                if flag & 256: sec += 1
                if flag & 2048: supp += 1
                if parts[9] != "*": lens.append(len(parts[9]))
                if n >= 2000: break
        finally:
            proc.terminate()
            try: proc.wait(timeout=3)
            except Exception: proc.kill()
        out.update({"sampled_records": n, "unmapped": unm, "secondary_0x100": sec,
                    "supplementary_0x800": supp,
                    "mean_query_len": round(sum(lens)/len(lens), 1) if lens else None,
                    "backend": "samtools"})
emit_summary(out)
'''

GTF_INSPECT = _INSPECT_PRELUDE + r'''
import gzip, re
p = safe_path(%(p)r)
size_bytes = os.path.getsize(p)
is_gz = p.lower().endswith(".gz")
opener = (lambda path: gzip.open(path, "rt", encoding="utf-8", errors="replace")) if is_gz else (lambda path: open(path, "r", encoding="utf-8", errors="replace"))
comments, features, chroms, gene_ids = [], {}, [], []
gid_re = re.compile(r'gene_id[ =]"?([^";\s]+)')
n = 0
with opener(p) as fh:
    for line in fh:
        if line.startswith("#"):
            if len(comments) < 5: comments.append(clip(line.strip()))
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 9: continue
        n += 1
        if len(chroms) < 5 and parts[0] not in chroms: chroms.append(clip(parts[0]))
        features[parts[2]] = features.get(parts[2], 0) + 1
        if len(gene_ids) < 5:
            m = gid_re.search(parts[8])
            if m and m.group(1) not in gene_ids: gene_ids.append(clip(m.group(1)))
        if n >= 500: break
out = {"format": "gtf_gff", "path": p, "size_bytes": size_bytes, "compressed": is_gz,
       "sampled_records": n, "header_comments": comments, "sample_chroms": chroms,
       "chrom_prefix_chr": any(c.startswith("chr") for c in chroms),
       "feature_counts": {clip(k): v for k, v in list(features.items())[:20]},
       "sample_gene_ids": gene_ids,
       "gene_id_has_version": any("." in g for g in gene_ids)}
emit_summary(out)
'''

PAF_INSPECT = _INSPECT_PRELUDE + r'''
import gzip
p = safe_path(%(p)r)
size_bytes = os.path.getsize(p)
is_gz = p.lower().endswith(".gz")
opener = (lambda path: gzip.open(path, "rt", encoding="utf-8", errors="replace")) if is_gz else (lambda path: open(path, "r", encoding="utf-8", errors="replace"))
n = 0; qlens, mapqs, targets = [], [], []
with opener(p) as fh:
    for line in fh:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 12: continue
        n += 1
        if parts[1].isdigit(): qlens.append(int(parts[1]))
        if parts[11].isdigit(): mapqs.append(int(parts[11]))
        if len(targets) < 5 and parts[5] not in targets: targets.append(clip(parts[5]))
        if n >= 1000: break
out = {"format": "paf", "path": p, "size_bytes": size_bytes, "compressed": is_gz,
       "sampled_records": n,
       "mean_query_len": round(sum(qlens)/len(qlens), 1) if qlens else None,
       "mean_mapq": round(sum(mapqs)/len(mapqs), 1) if mapqs else None,
       "sample_targets": targets}
emit_summary(out)
'''

VCF_INSPECT = _INSPECT_PRELUDE + r'''
import gzip
p = safe_path(%(p)r)
size_bytes = os.path.getsize(p)
is_gz = p.lower().endswith(".gz")
opener = (lambda path: gzip.open(path, "rt", encoding="utf-8", errors="replace")) if is_gz else (lambda path: open(path, "r", encoding="utf-8", errors="replace"))
samples, chroms, filters = [], [], {}
n_contigs = 0; n_rec = 0
with opener(p) as fh:
    for line in fh:
        if line.startswith("##contig"):
            n_contigs += 1; continue
        if line.startswith("##"):
            continue
        if line.startswith("#CHROM"):
            cols = line.rstrip("\n").split("\t")
            samples = [clip(s) for s in cols[9:59]]
            continue
        parts = line.rstrip("\n").split("\t", 8)
        if len(parts) < 7: continue
        n_rec += 1
        if len(chroms) < 10 and parts[0] not in chroms: chroms.append(clip(parts[0]))
        filters[parts[6]] = filters.get(parts[6], 0) + 1
        if n_rec >= 200: break
out = {"format": "vcf", "path": p, "size_bytes": size_bytes, "compressed": is_gz,
       "n_contigs": n_contigs, "sample_count_preview": len(samples), "samples": samples,
       "sampled_variants": n_rec, "sample_chroms": chroms,
       "filter_counts": {clip(k): v for k, v in list(filters.items())[:20]}}
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


def cat_path(path, tail=False, max_bytes=DEFAULT_TEXT_PREVIEW_BYTES):
    """Always use a bounded text preview; never download full files."""
    if tail:
        return inspect_text(path, max_bytes=max_bytes, tail=True)
    return inspect_text(path, max_bytes=max_bytes)


def multi_cat(paths, tail=False, max_bytes=DEFAULT_TEXT_PREVIEW_BYTES):
    """Read multiple Contents-root text previews in a single browser round trip."""
    if not paths:
        raise ValueError("paths_required")
    if len(paths) > 64:
        raise ValueError("too_many_paths: limit 64")
    limit = validate_inspect_bytes(max_bytes)
    reqs = []
    meta_by_id = {}
    external = []
    for idx, raw_path in enumerate(paths):
        srv = to_server(raw_path)
        contents = contents_path(raw_path)
        key = str(idx)
        meta_by_id[key] = (raw_path, srv)
        if contents is None:
            external.append((key, raw_path, srv))
        else:
            reqs.append({
                "id": key,
                "kind": "read_range",
                "path": contents,
                "mode": "tail" if tail else "head",
                "max_bytes": limit,
            })
    results_map = {}
    if reqs:
        res = evaluate_op("batch_http", {"requests": reqs}, timeout=MAX_SYNC_SECONDS)
        raw_results = (res.get("data") or {}).get("results") or {}
        for key, item in raw_results.items():
            _, srv = meta_by_id[key]
            if isinstance(item, dict) and item.get("ok"):
                field = "tail" if tail else "head"
                results_map[key] = {
                    "ok": True, "format": "text", "path": srv,
                    "size": item.get("size"), "bytes_read": item.get("bytes_read"),
                    "truncated": bool(item.get("truncated")),
                    field: item.get("text", ""),
                }
            else:
                results_map[key] = {
                    "ok": False, "format": "text", "path": srv,
                    "error": (item or {}).get("error") if isinstance(item, dict) else "read_failed",
                    "status": (item or {}).get("status") if isinstance(item, dict) else None,
                }
    for key, raw_path, srv in external:
        entry = inspect_text(raw_path, max_bytes=limit, tail=tail)
        entry["ok"] = not bool(entry.get("error"))
        results_map[key] = entry
    ordered = [results_map[str(i)] for i in range(len(paths))]
    return {"ok": all(item.get("ok", False) for item in ordered), "count": len(ordered), "files": ordered}


def inspect(path, timeout=300):
    raw = fix_msys(path)
    clean = raw.split("?")[0]
    low = clean.lower()
    ext = os.path.splitext(clean)[1].lower()
    srv = to_server(raw)

    source, extra = {
        ".h5ad": (H5AD, {}),
        ".parquet": (PARQUET, {}),
        ".csv": (TABLE, {"sep": ","}),
        ".tsv": (TABLE, {"sep": "\t"}),
        ".bam": (BAM_INSPECT, {}),
        ".fastq": (FASTQ_INSPECT, {}),
        ".fq": (FASTQ_INSPECT, {}),
        ".gtf": (GTF_INSPECT, {}),
        ".gff": (GTF_INSPECT, {}),
        ".gff3": (GTF_INSPECT, {}),
        ".paf": (PAF_INSPECT, {}),
        ".vcf": (VCF_INSPECT, {}),
    }.get(ext, (None, {}))
    if source is None and ext == ".gz":
        for suffix, tmpl in (
            (".fastq.gz", FASTQ_INSPECT), (".fq.gz", FASTQ_INSPECT),
            (".gtf.gz", GTF_INSPECT), (".gff.gz", GTF_INSPECT), (".gff3.gz", GTF_INSPECT),
            (".paf.gz", PAF_INSPECT), (".vcf.gz", VCF_INSPECT),
        ):
            if low.endswith(suffix):
                source = tmpl
                break
    if source is not None:
        return _bounded_inspection_result(
            run_python(source % {"p": srv, **extra}, timeout=min(timeout, MAX_SYNC_SECONDS)),
            srv, "inspect_failed")

    if ext == ".ipynb":
        return inspect_ipynb(raw)

    if ext in (".rds",):
        return inspect_rds(srv, timeout=timeout)

    # scripts, text and unknown extensions all get the bounded text preview
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


def _contents_range_text(contents, server_path, limit, tail=False):
    """Read bounded head/tail text via HTTP Range without starting a kernel."""
    value = evaluate_op("read_range", {"path": contents, "mode": "tail" if tail else "head",
                                       "max_bytes": limit}, timeout=MAX_SYNC_SECONDS)
    data = value.get("data") or {}
    text = data.get("text")
    if data.get("ok") is not True or not isinstance(text, str):
        return None
    out = {"format": "text", "path": server_path, "size": data.get("size"),
           "bytes_read": data.get("bytes_read", len(text.encode("utf-8"))),
           "truncated": bool(data.get("truncated"))}
    out["tail" if tail else "head"] = text
    return out


def inspect_text(path, max_bytes=DEFAULT_TEXT_PREVIEW_BYTES, tail=False):
    """Return a byte-bounded text preview; pure HTTP (Contents/Range) first, kernel for the rest."""
    limit = validate_inspect_bytes(max_bytes)
    srv = to_server(path)
    contents = contents_path(path)
    if contents is not None:
        if tail:
            try:
                preview = _contents_range_text(contents, srv, limit, tail=True)
                if preview is not None:
                    return preview
            except Bridge as exc:
                if "status" in str(exc) or "auth" in str(exc) or "read_failed" in str(exc):
                    raise
        else:
            preview = _contents_text(contents, srv, limit)
            if preview is not None:
                return preview
            try:
                ranged = _contents_range_text(contents, srv, limit, tail=False)
                if ranged is not None:
                    return ranged
            except Bridge:
                pass
    if tail:
        tail_code = _INSPECT_PRELUDE + (
            "import os, stat\n"
            f"p=safe_path({srv!r}); limit={limit!r}\n"
            "try:\n"
            "    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)\n"
            "    fd = os.open(p, flags)\n"
            "    try:\n"
            "        info = os.fstat(fd)\n"
            "        if not stat.S_ISREG(info.st_mode): raise ValueError('not_regular_file')\n"
            "        size = info.st_size\n"
            "        start = max(0, size - limit)\n"
            "        os.lseek(fd, start, os.SEEK_SET)\n"
            "        with os.fdopen(fd, 'rb') as fh:\n"
            "            fd = None; raw = fh.read(limit)\n"
            "    finally:\n"
            "        if fd is not None: os.close(fd)\n"
            "    out = {'format':'text','path':p,'size':size,'bytes_read':len(raw),'truncated':start>0,"
            "'tail':raw.decode('utf-8', errors='replace')}\n"
            "except Exception as exc:\n"
            "    out = {'format':'text','path':p,'error':'bounded_read_failed','detail':clip(exc)}\n"
            "emit_summary(out)\n"
        )
        response = run_python(tail_code, timeout=MAX_SYNC_SECONDS)
        return _bounded_inspection_result(response, srv, "text_preview_failed")
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
    # Fresh jobs avoid stale results and collisions with pruned or other-session jobs.
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
            kernels = (_call_js("cleanup", {}, tab_id, 30, session_id) or {}).get("data")
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
        "controller_platform_supported": os.name == "nt" or sys.platform in ("darwin", "linux"),
        "controller_lock_backend": "msvcrt" if os.name == "nt" else "fcntl",
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
                # Full probe (not the routine lite one) so a read-only workspace is
                # reported here instead of surfacing later as a write failure.
                detail = {}
                ready = _probe_tab(tab_id, lite=False, detail=detail)
                writable = detail.get("work_writable")
                shown = {"status_code": detail.get("status_code", 200 if ready else None)}
                if writable is not None:
                    shown["work_writable"] = writable
                action = None
                if not ready:
                    action = "工作区 Jupyter 未就绪；检查启动或队列状态"
                elif writable is False:
                    action = "work 目录只读；write/put 会失败，请检查工作区权限"
                add("jupyter", "ok" if ready else "failed", shown, action)
            except Bridge as exc:
                message = str(exc)
                if message.startswith("jupyter_auth_required:"):
                    add("jupyter", "failed", {"error": message[:200]},
                        "在同一个浏览器 profile 中重新登录 DCS/Jupyter")
                elif _is_cooperation_error(exc):
                    add("jupyter", "failed", {"error": message[:200]},
                        "检查 BrowserSkill cooperate 状态；保留当前页面，等待人工操作结束后再试")
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
    if LOGICAL_SESSION_ID and isinstance(o, dict):
        o = dict(o, logical_session_id=LOGICAL_SESSION_ID,
                 state_dir=os.path.dirname(config_path()))
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
    ap.add_argument("--session", help="logical session ID returned by session create (place before the command)")
    sub = ap.add_subparsers(dest="subcmd", required=True)

    p = sub.add_parser("session", help="create or list independent conversation sessions")
    sessions = p.add_subparsers(dest="session_command", required=True)
    p = sessions.add_parser("create", help="allocate an independent session and connect; retain its ID on failure")
    p.add_argument("--url", required=True, help="user-selected supported workspace URL")
    sessions.add_parser("list", help="list saved logical sessions without contacting the browser")

    p = sub.add_parser("connect", help="connect only to the user-selected workspace URL and confirm its iframe")
    p.add_argument("--url", help="current workspace URL copied after the user manually opens Personal Analysis; "
                                 "a supplied URL is saved only after the live Jupyter probe succeeds")
    p.add_argument("--refresh", action="store_true",
                   help="force re-navigating the controller tab to recover from a stuck page or container restart")
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
    p.add_argument("--tail", action="store_true", help="read the trailing bytes via pure HTTP Range when possible")
    p.add_argument("--bytes", dest="max_bytes", type=int, default=DEFAULT_TEXT_PREVIEW_BYTES,
                   help=f"maximum bytes to read (1-{MAX_INSPECT_BYTES}, default {DEFAULT_TEXT_PREVIEW_BYTES})")
    p = sub.add_parser("multi-cat", help="read multiple work/... text previews in one browser round trip")
    p.add_argument("paths", nargs="+", help="1-64 paths to read concurrently")
    p.add_argument("--tail", action="store_true", help="read trailing bytes of each file")
    p.add_argument("--bytes", dest="max_bytes", type=int, default=DEFAULT_TEXT_PREVIEW_BYTES,
                   help=f"maximum bytes per file (1-{MAX_INSPECT_BYTES}, default {DEFAULT_TEXT_PREVIEW_BYTES})")
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
    p = sub.add_parser("put", help="upload a local file under the Contents root (<= 4 MiB, or <= 64 MiB with --allow-large)")
    p.add_argument("local", help="local source path")
    p.add_argument("--remote", required=True, help="work/... destination under the Contents root")
    p.add_argument("--overwrite", action="store_true", help="replace an existing remote file")
    p.add_argument("--allow-large", action="store_true",
                   help=f"use chunked Contents HTTP upload up to {MAX_CHUNKED_UPLOAD_BYTES // (1024 * 1024)} MiB")

    p = sub.add_parser("sync-up", help="bundle and atomically sync a local directory to work/...")
    p.add_argument("local_dir", help="local directory to upload")
    p.add_argument("--remote", required=True, help="remote destination directory under work/...")
    p.add_argument("--exclude", action="append", default=[], help="glob pattern to exclude (repeatable)")
    p.add_argument("--no-chmod-sh", action="store_true", help="do not auto-chmod +x on *.sh files")

    p = sub.add_parser("sync-down", help="download matching files from a remote directory tree")
    p.add_argument("remote_dir", help="remote directory under work/...")
    p.add_argument("--out", required=True, help="local destination directory")
    p.add_argument("--glob", dest="glob_patterns", default="*",
                   help="comma-separated filename/relative-path globs, e.g. '*.tsv,*.pdf,*.png'")
    p.add_argument("--overwrite", action="store_true", help="overwrite existing local files")
    p.add_argument("--skip-existing", action="store_true", help="skip local files matching remote size")
    p.add_argument("--no-verify", action="store_true", help="skip remote sha256 verification")
    p.add_argument("--max-files", type=int, default=100, help="maximum files to download (1-500)")

    sub.add_parser("host-health", help="check server CPU/cgroup/load/memory/disk speed and compute routing advice")

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
    p.add_argument("--threads", type=int,
                   help="pin OPENBLAS/OMP/MKL/NUMEXPR_NUM_THREADS to N (1-256) to avoid shared-host thrashing")
    p.add_argument("--local-tmp", action="store_true",
                   help="create private /tmp/sn_job_<id> as TMPDIR and clean it up automatically on exit")
    p = sub.add_parser("jobs", help="job registry: server job dirs merged with local receipts")
    p.add_argument("--remote", action="store_true", help="also scan /data/work/agent_jobs")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("prune", help="delete finished job dirs older than N days (never a live owner)")
    p.add_argument("--days", type=int, default=7, help="age threshold in days (default: 7)")
    p.add_argument("--dry-run", action="store_true", help="report what would be removed")
    p.add_argument("--batch", dest="batch_id", help="limit cleanup to finished jobs belonging to one batch ID")

    bp = sub.add_parser("batch", help="server-side multi-sample batch queue orchestration")
    bsub = bp.add_subparsers(dest="batch_command", required=True)
    p = bsub.add_parser("submit", help="submit a manifest-driven multi-sample batch with concurrency control")
    p.add_argument("--batch", dest="batch_id", required=True, help="unique batch identifier")
    p.add_argument("--manifest", required=True, help="local TSV, CSV, or JSON manifest with a sample_id column")
    p.add_argument("--cmd-template", required=True, help="shell command template using {column} placeholders")
    p.add_argument("--cwd", default=WORK_ROOT, help="server working directory for each sample")
    p.add_argument("--max-concurrent", type=int, default=2, help="max simultaneous sample workers (1-64)")
    p.add_argument("--threads", type=int, help="pin BLAS/OpenMP threads per sample (1-256)")
    p.add_argument("--local-tmp", action="store_true", help="assign private auto-cleaned /tmp TMPDIR per sample")
    p = bsub.add_parser("status", help="kernel-free HTTP dashboard for a batch")
    p.add_argument("batch_id")
    p.add_argument("--logs", action="store_true", help="also include stderr tails for running samples")
    p.add_argument("--tail-bytes", type=int, default=2048)
    p = bsub.add_parser("retry-failed", help="re-queue only failed/cancelled samples of a batch")
    p.add_argument("batch_id")
    p.add_argument("--max-concurrent", type=int, help="override concurrency for the retry generation")
    p = bsub.add_parser("cancel", help="gracefully stop a running batch queue and its active workers")
    p.add_argument("batch_id")
    p = sub.add_parser("cancel", help="SIGTERM a job whose PID still belongs to its job directory")
    p.add_argument("job_id")
    p = sub.add_parser("accept", help="record that a job's result was accepted")
    p.add_argument("job_id")
    p.add_argument("--note")
    p = sub.add_parser("poll")
    p.add_argument("job_id")
    p.add_argument("--tail", type=int, default=40)
    p.add_argument("--lite", action="store_true",
                   help="kernel-free pure-HTTP poll (reads status/stage/heartbeat/log tails in one call)")
    p = sub.add_parser("artifacts", help="inventory regular files under a job's artifacts directory")
    p.add_argument("job_id")

    sub.add_parser("close", help="only when requested: close this controller's browser page and owned kernel")

    a = ap.parse_args(argv)
    session_id = a.session
    if session_id is None and a.subcmd != "session":
        env_session = os.environ.get("SN_SESSION", "").strip()
        if env_session:
            session_id = env_session
    try:
        if a.subcmd == "session":
            if session_id:
                raise ValueError("session_management_does_not_take_session_id")
            if a.session_command == "list":
                return _emit(list_logical_sessions())
            session_id = create_logical_session(a.url)
            a.subcmd, a.url = "connect", None
        with selected_session(session_id):
            return _dispatch(a)
    except (Bridge, ValueError, OSError) as exc:
        result = {"ok": False, "error": str(exc)}
        if session_id:
            result["logical_session_id"] = session_id
        return _emit(result, failure_code=2 if isinstance(exc, ValueError) else 1)


def _dispatch(a):
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
            if getattr(a, "refresh", False):
                tab_id = ensure_tab(force=True, full_probe=True)
            else:
                tab_id = ensure_tab(full_probe=True)
            if supplied_url:
                save_url(supplied_url)
            state = _browser_state()
            _print({"ok": True, "session_id": state.get("session_id"),
                    "browser_session_id": state.get("session_id"), "tab_id": tab_id,
                    "task_base": state.get("task_base"), "url_saved": bool(supplied_url)})
        elif a.subcmd == "probe":
            _print(evaluate_op("probe", {}))
        elif a.subcmd == "doctor":
            return _emit(doctor(deep=a.deep))
        elif a.subcmd == "ls":
            return _emit(list_path(a.path, include_hidden=a.all))
        elif a.subcmd == "cat":
            return _emit(cat_path(a.path, tail=a.tail, max_bytes=a.max_bytes))
        elif a.subcmd == "multi-cat":
            return _emit(multi_cat(a.paths, tail=a.tail, max_bytes=a.max_bytes))
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
            return _emit(put_file(a.local, a.remote, overwrite=a.overwrite, allow_large=a.allow_large))
        elif a.subcmd == "sync-up":
            return _emit(sync_up(a.local_dir, a.remote, excludes=a.exclude,
                                 chmod_scripts=not a.no_chmod_sh))
        elif a.subcmd == "sync-down":
            return _emit(sync_down(a.remote_dir, a.out, glob_patterns=a.glob_patterns,
                                   overwrite=a.overwrite, verify=not a.no_verify,
                                   max_files=a.max_files, skip_existing=a.skip_existing))
        elif a.subcmd == "host-health":
            return _emit(host_health())
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
                          idempotency_key=a.idempotency_key,
                          threads=a.threads, local_tmp=a.local_tmp))
        elif a.subcmd == "jobs":
            return _emit(list_jobs(limit=a.limit, remote=a.remote))
        elif a.subcmd == "prune":
            return _emit(prune_jobs(days=a.days, dry_run=a.dry_run, batch_id=a.batch_id))
        elif a.subcmd == "batch":
            if a.batch_command == "submit":
                return _emit(batch_submit(
                    a.batch_id, a.manifest, a.cmd_template, cwd=a.cwd,
                    max_concurrent=a.max_concurrent, threads=a.threads,
                    local_tmp=a.local_tmp,
                ))
            if a.batch_command == "status":
                return _emit(batch_status(a.batch_id, include_logs=a.logs, tail_bytes=a.tail_bytes))
            if a.batch_command == "retry-failed":
                return _emit(batch_retry_failed(a.batch_id, max_concurrent=a.max_concurrent))
            if a.batch_command == "cancel":
                return _emit(batch_cancel(a.batch_id))
        elif a.subcmd == "cancel":
            return _emit(cancel_job(a.job_id))
        elif a.subcmd == "accept":
            return _emit(accept_job(a.job_id, note=a.note))
        elif a.subcmd == "poll":
            return _emit(poll(a.job_id, tail=a.tail, lite=a.lite))
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
