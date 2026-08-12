#!/usr/bin/env python3
"""
sn.py — StereoNote (DCS) server-side JupyterLab controller, v3.2.1.

Entry is the user's already-logged-in Edge, driven through the kimi-webbridge
Go daemon (HTTP POST /command) -> CDP -> the inner.dcs.cloud JupyterLab iframe
-> same-origin Jupyter API (Contents + kernel websocket).

v3 changes vs the original skill:
  * self-healing daemon:   auto-start the Go daemon, clear a stale pid file,
                           wait for the extension to reconnect.
  * navigate-based entry:  `find_tab active` (borrowing a pre-existing tab) does
                           NOT work with this extension build, so we `navigate`
                           to the configured workspace URL instead, and rediscover
                           the iframe dynamically (container id changes each run).
  * async file-queue:      long jobs run detached (setsid) and are polled through
                           bounded shell reads, so no single call sits on the 60s
                           bridge ceiling.
  * bioinformatics inspect: h5ad / rds / csv|tsv / parquet / ipynb / scripts ->
                           compact JSON summaries, computed server-side, so big
                           files never move across the bridge.

Secrets (token / _xsrf) never leave the browser: jupyter_runtime.js reads them
inside the iframe and only operations cross the bridge.

Usage (run from this scripts/ dir; needs Python 3 + requests locally):
    python sn.py connect
    python sn.py probe
    python sn.py ls   work
    python sn.py cat  work/foo.py
    python sn.py write work/agent_outputs/x.txt --content "..."
    python sn.py run-python --code "print(1+1)"
    python sn.py run-shell  --cmd "ls -la /data/work | head"
    python sn.py inspect /data/work/xxx.h5ad
    python sn.py inspect /data/work/seurat.rds
    python sn.py submit --cwd /data/work --cmd "python big_job.py"  # -> job id
    python sn.py poll   <job_id>
    python sn.py close                                  # close extra workspace tabs
"""
from __future__ import annotations
import argparse, base64, binascii, json, os, posixpath, re, shlex, subprocess, sys, time, uuid
from urllib.parse import parse_qs, urlencode, urlsplit

try:
    import requests
except Exception as exc:  # pragma: no cover
    print(json.dumps({"ok": False, "error": f"requests_required: {exc}"})); sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
RUNTIME_JS_PATH = os.path.join(HERE, "jupyter_runtime.js")
CONTROLLER_VERSION = "3.2.1"


DEFAULT_CFG = {
    "session": "stereonote",
    "work_root": "/data/work",
    "daemon_url": "http://127.0.0.1:10086",
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
DAEMON = os.environ.get("WEBBRIDGE_URL", CFG.get("daemon_url", DEFAULT_CFG["daemon_url"]))
SESSION = os.environ.get("SN_SESSION", CFG.get("session", DEFAULT_CFG["session"]))
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
    for key in ("session", "work_root", "daemon_url"):
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
KWB_HOME = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".kimi-webbridge")
DAEMON_BIN = os.path.join(KWB_HOME, "bin", "kimi-webbridge.exe")
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


def make_job_id(label=None):
    """Create a safe UUID-backed job id, optionally namespaced by a label."""
    prefix = "job"
    if label is not None:
        if not isinstance(label, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", label):
            raise ValueError("invalid_job_label: use [a-z][a-z0-9_-]{0,31}, for example analysis_1")
        prefix = label
    return f"{prefix}_{uuid.uuid4()}"


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


# ─────────────────────────── daemon self-heal ───────────────────────────
def daemon_base():
    """Return a normalized loopback-only WebBridge origin."""
    try:
        parts = urlsplit(str(DAEMON).strip())
        host = (parts.hostname or "").lower()
        if (parts.scheme != "http" or host not in ("127.0.0.1", "localhost", "::1") or
                parts.username is not None or parts.password is not None or
                parts.query or parts.fragment or parts.path not in ("", "/")):
            raise ValueError
        if parts.port is None:
            raise ValueError
    except (TypeError, ValueError):
        raise Bridge("daemon_url_must_be_loopback_http_with_explicit_port")
    return str(DAEMON).strip().rstrip("/")


def daemon_status():
    try:
        r = requests.get(daemon_base() + "/status", timeout=5)
        return r.json()
    except (Bridge, requests.RequestException, ValueError):
        return None


def _pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=15).stdout
        return str(pid) in out
    except Exception:
        return False


def _start_daemon():
    if not os.path.exists(DAEMON_BIN):
        raise Bridge(f"daemon_binary_missing: {DAEMON_BIN} — 需先安装 kimi-webbridge")
    p = subprocess.run([DAEMON_BIN, "start"], capture_output=True, text=True, timeout=60)
    out = (p.stdout or "") + (p.stderr or "")
    if "pid file" in out and "exists" in out:
        pidf = os.path.join(KWB_HOME, "daemon.pid")
        pid = None
        try:
            pid = int(open(pidf).read().strip())
        except Exception:
            pid = None
        if pid is None or not _pid_alive(pid):
            try:
                os.remove(pidf)
            except OSError:
                pass
            subprocess.run([DAEMON_BIN, "start"], capture_output=True, text=True, timeout=60)


def ensure_daemon(wait_ext=45):
    daemon_base()
    st = daemon_status()
    if not (st and st.get("running")):
        _start_daemon()
    for _ in range(12):
        st = daemon_status()
        if st and st.get("running"):
            break
        time.sleep(1)
    else:
        raise Bridge("daemon_not_running: 端口可能被其它进程占用")
    if st.get("extension_connected"):
        return st
    deadline = time.time() + wait_ext
    while time.time() < deadline:
        time.sleep(3)
        st = daemon_status()
        if st and st.get("extension_connected"):
            return st
    raise Bridge("extension_not_connected: 请确保 Edge 已打开且 kimi-webbridge 扩展已启用")


# ─────────────────────────── transport ───────────────────────────
def command(action, args, timeout=60):
    body = json.dumps({"session": SESSION, "action": action, "args": args}).encode("utf-8")
    try:
        r = requests.post(daemon_base() + "/command", data=body,
                          headers={"Content-Type": "application/json"}, timeout=min(timeout, 60))
    except requests.RequestException as exc:
        raise Bridge(f"daemon_unreachable: {exc}") from exc
    try:
        payload = r.json()
    except ValueError:
        raise Bridge(f"bad_daemon_response: {r.status_code} {r.text[:200]}")
    if not payload.get("ok", False):
        raise Bridge(f"command_failed({action}): {json.dumps(payload.get('error', payload))[:300]}")
    return payload.get("data", {})


def cdp(method, params=None, timeout=60):
    return command("cdp", {"method": method, "params": params or {}}, timeout=timeout)


# ─────────────────────────── tab / iframe (navigate entry) ───────────────────────────
def _walk(node, acc):
    acc.append(node.get("frame", {}))
    for c in node.get("childFrames", []) or []:
        _walk(c, acc)


def get_frames():
    """Return (has_tab, [frame,...]). has_tab False if the session has no tab yet."""
    try:
        tree = cdp("Page.getFrameTree")
    except Bridge as e:
        message = str(e).lower()
        if "no tab" in message or "tab was closed" in message:
            return (False, [])
        raise
    root = tree.get("frameTree") or tree
    acc = []
    _walk(root, acc)
    return (True, acc)


def _iframe_id(frames):
    for fr in frames:
        try:
            parts = urlsplit(fr.get("url") or "")
            if (parts.scheme == "https" and (parts.hostname or "").lower() == "inner.dcs.cloud" and
                    parts.path.startswith("/notebook/st/")):
                return fr.get("id")
        except ValueError:
            continue
    return None


def _iframe_alive(fid):
    """Reused tab may hold a DEAD container (workspace restarted). Verify api/status==200."""
    try:
        ctx = make_context(fid)
        expr = (_runtime() + "\n;(async () => { return await window.__codexJupyter('probe', {}); })()")
        res = cdp("Runtime.evaluate", {"expression": expr, "contextId": ctx,
                                       "awaitPromise": True, "returnByValue": True, "userGesture": True})
        val = res.get("result", {}).get("value") or {}
        return (val.get("data", {}) or {}).get("status_code") == 200
    except Bridge:
        return False


def ensure_iframe(navigate=False, settle=45, workspace_url=None, force_navigation=False):
    # 1. reuse a live JupyterLab tab this session already owns (across chats, same daemon)
    has, frames = get_frames()
    fid = _iframe_id(frames)
    if not force_navigation and fid and _iframe_alive(fid):
        return fid
    if not navigate:
        raise Bridge("workspace_not_connected: 请先手动打开目标个性分析,等待启动完成,然后把当前 URL 提供给 `connect --url`")
    # 2. navigate only to the URL explicitly supplied to this connect invocation.
    target_url = workspace_url or ""
    if not target_url:
        if WORKSPACE_URL_ERROR:
            raise Bridge(f"invalid_workspace_url: {WORKSPACE_URL_ERROR}")
        raise Bridge(
            "no_workspace_url: 没有可用的标签页。请让用户在 Edge 里打开自己的 "
            "StereoNote workspace,复制地址栏完整 URL(形如 "
            "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=...&workspaceId=...),"
            "然后 `python sn.py connect --url \"<粘贴的URL>\"`。")
    command("navigate", {"url": target_url, "newTab": (not has)}, timeout=60)
    for _ in range(settle // 3):
        time.sleep(3)
        _, frames = get_frames()
        fid = _iframe_id(frames)
        if fid and _iframe_alive(fid):
            return fid
    raise Bridge("jupyter_iframe_not_found: workspace 可能还在启动/排队,或 URL 已过期需重新登录/重新粘贴 "
                 "(workspaceId 每次可能变)。让用户复制当前 StereoNote 网址后 `connect --url \"<URL>\"`。")


def make_context(frame_id):
    res = cdp("Page.createIsolatedWorld",
              {"frameId": frame_id, "worldName": "sn-v2", "grantUniveralAccess": True})
    ctx = res.get("executionContextId")
    if ctx is None:
        raise Bridge(f"no_execution_context: {json.dumps(res)[:200]}")
    return ctx


def _runtime():
    with open(RUNTIME_JS_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def evaluate_op(op, payload, timeout=60):
    ensure_daemon()
    fid = ensure_iframe()
    ctx = make_context(fid)
    expr = (_runtime() + "\n;(async () => { return await window.__codexJupyter("
            + json.dumps(op) + ", " + json.dumps(payload) + "); })()")
    res = cdp("Runtime.evaluate", {"expression": expr, "contextId": ctx,
                                   "awaitPromise": True, "returnByValue": True, "userGesture": True},
              timeout=min(timeout, 60))
    if res.get("exceptionDetails"):
        d = res["exceptionDetails"]
        raise Bridge("iframe_js_exception: " +
                     str(d.get("exception", {}).get("description") or d.get("text")))
    val = res.get("result", {}).get("value")
    if val is None:
        raise Bridge(f"no_value_returned: {json.dumps(res)[:300]}")
    if not val.get("ok", False):
        detail = val.get("error") or (val.get("data") or {}).get("error") or "operation_failed"
        raise Bridge(f"jupyter_operation_failed({op}): {json.dumps(detail, ensure_ascii=False)[:300]}")
    return val


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
    server_path = to_server(path)
    if not server_path.startswith(WORK_ROOT + "/"):
        raise ValueError("write_path_must_be_under_work_root")
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    marker = f"__SN_WRITE_{uuid.uuid4().hex}__:"
    code = f'''import base64, json, os, stat
_path = {server_path!r}
_root = {WORK_ROOT!r}
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


def submit(cmd, name=None, cwd=None, timeout=MAX_SYNC_SECONDS, readiness_timeout=5.0):
    """Submit a raw shell command; its payload is intentionally not OS-sandboxed.

    Controller-created state is restricted to ``/data/work/agent_jobs``. The
    payload starts in ``cwd`` (default ``/data/work``), but ``cmd`` can still write
    wherever its server-side permissions allow.
    """
    jid = make_job_id(name)
    launcher = build_job_launcher(jid, cmd, cwd=cwd)
    res = run_shell(launcher, cwd=WORK_ROOT, timeout=timeout)
    if res.get("ok") is not True or res.get("returncode") != 0:
        raise Bridge(f"job_setup_failed: {res.get('stderr') or res.get('stdout') or 'launcher failed'}")
    acknowledgement = f"__SN_SUBMITTED__={jid}"
    if acknowledgement not in (res.get("stdout") or "").splitlines():
        raise Bridge("launcher_ack_missing")
    _wait_for_runner_ready(jid, readiness_timeout)
    return {"job_id": jid, "dir": f"{JOB_ROOT}/{jid}", "cwd": to_server(cwd or WORK_ROOT),
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
emit() {{ printf '%s %s %s %s\\n' "$frame" "$1" "$2" "$3"; }}
if [ -L {shlex.quote(JOB_ROOT)} ] || [ ! -d {shlex.quote(JOB_ROOT)} ] || [ -L {jdir} ] || [ ! -d {jdir} ]; then
  emit status unsafe_root ''; emit stdout unsafe_root ''; emit stderr unsafe_root ''
else
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
    records = {key: _poll_section(raw, frame, key) for key in ("status", "stdout", "stderr")}
    missing = {key: records[key][0] == "missing" for key in records}
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
    return {"job_id": job_id, "tail": tail, "status": status,
            "stdout_tail": tails["stdout"], "stderr_tail": tails["stderr"],
            "missing": missing, "read_errors": errors}


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


def list_path(path, timeout=MAX_SYNC_SECONDS):
    """Bounded server-side directory listing for any Linux-visible path."""
    server_path = to_server(path)
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


def inspect_text(path, max_bytes=DEFAULT_TEXT_PREVIEW_BYTES):
    """Return a byte-bounded text preview read by a temporary server-side kernel."""
    limit = validate_inspect_bytes(max_bytes)
    srv = to_server(path)
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
    try:
        return command("close_tab", {})
    except Bridge as e:
        return {"ok": False, "error": str(e)}


def doctor():
    """Local preflight only: report prerequisites without exposing secrets."""
    try:
        daemon_base()
        daemon_url_valid = True
    except Bridge:
        daemon_url_valid = False
    st = daemon_status()
    return {
        "ok": bool(os.name == "nt" and os.path.exists(DAEMON_BIN) and daemon_url_valid),
        "controller_version": CONTROLLER_VERSION,
        "platform": sys.platform,
        "windows_native_supported": os.name == "nt",
        "python": sys.version.split()[0],
        "requests": getattr(requests, "__version__", "unknown"),
        "config_path": config_path(),
        "saved_workspace_ids": bool(CFG.get("project_id") and CFG.get("workspace_id")),
        "legacy_repo_config_present": os.path.exists(LEGACY_CONFIG_PATH),
        "daemon_binary": DAEMON_BIN,
        "daemon_binary_exists": os.path.exists(DAEMON_BIN),
        "daemon_url_valid": daemon_url_valid,
        "daemon_running": bool(st and st.get("running")),
        "extension_connected": bool(st and st.get("extension_connected")),
    }


# ─────────────────────────── CLI ───────────────────────────
def _print(o):
    sys.stdout.write(json.dumps(o, ensure_ascii=False, indent=2) + "\n")


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
    ap = argparse.ArgumentParser(description=f"StereoNote server-side JupyterLab controller (v{CONTROLLER_VERSION})")
    sub = ap.add_subparsers(dest="subcmd", required=True)

    p = sub.add_parser("connect", help="connect only to the user-selected workspace URL and confirm its iframe")
    p.add_argument("--url", help="current workspace URL copied after the user manually opens Personal Analysis; "
                                 "a supplied URL is saved only after the live Jupyter probe succeeds")
    sub.add_parser("status", help="daemon /status only")
    sub.add_parser("probe", help="read-only Jupyter env probe")
    sub.add_parser("doctor", help="local prerequisite/config preflight (no secrets)")

    p = sub.add_parser("ls", help="list a Jupyter Contents path such as work/project")
    p.add_argument("path", nargs="?", default="work", help="use work/... for files under /data/work")
    p = sub.add_parser("cat", help="read a bounded preview; use work/... for /data/work files")
    p.add_argument("path")
    p = sub.add_parser("write", help="atomically create UTF-8 text under work/...; no-clobber by default")
    p.add_argument("path", help="target under work/... (server /data/work/...)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--content"); g.add_argument("--content-file")
    p.add_argument("--overwrite", action="store_true", help="explicitly allow replacing an existing file")

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
    p.add_argument("--cwd", default=WORK_ROOT,
                   help="server working directory for the payload (default: /data/work)")
    p = sub.add_parser("poll"); p.add_argument("job_id"); p.add_argument("--tail", type=int, default=40)
    p = sub.add_parser("artifacts", help="inventory regular files under a job's artifacts directory")
    p.add_argument("job_id")

    sub.add_parser("close", help="close the current workspace tab (cleanup extras)")

    a = ap.parse_args(argv)
    try:
        if a.subcmd == "status":
            return _emit(daemon_status() or {"ok": False, "error": "daemon_down"})
        elif a.subcmd == "connect":
            supplied_url = canonical_workspace_url(a.url) if getattr(a, "url", None) else None
            st = ensure_daemon()
            fid = ensure_iframe(navigate=bool(supplied_url), workspace_url=supplied_url,
                                force_navigation=bool(supplied_url))
            if supplied_url:
                save_url(supplied_url)
            _print({"ok": True, "daemon": st.get("version"),
                    "extension_connected": st.get("extension_connected"),
                    "iframe_frame_id": fid, "url_saved": bool(supplied_url)})
        elif a.subcmd == "probe":
            _print(evaluate_op("probe", {}))
        elif a.subcmd == "doctor":
            return _emit(doctor())
        elif a.subcmd == "ls":
            return _emit(list_path(a.path))
        elif a.subcmd == "cat":
            return _emit(cat_path(a.path))
        elif a.subcmd == "write":
            content = a.content
            if a.content_file:
                content = read_local_text(a.content_file, MAX_WRITE_BYTES, "write_content_too_large")
            validate_text_payload(content, MAX_WRITE_BYTES, "write_content_too_large")
            _print(write_text(a.path, content, overwrite=bool(a.overwrite)))
        elif a.subcmd == "run-python":
            code = a.code if a.code is not None else read_local_text(
                a.code_file, MAX_CODE_BYTES, "python_code_too_large")
            _print(run_python(code, timeout=a.timeout))
        elif a.subcmd == "run-shell":
            return _emit(run_shell(a.cmd, cwd=a.cwd, timeout=a.timeout))
        elif a.subcmd == "inspect":
            return _emit(inspect(a.path, timeout=a.timeout))
        elif a.subcmd == "submit":
            _print(submit(a.cmd, name=a.name, cwd=a.cwd))
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
