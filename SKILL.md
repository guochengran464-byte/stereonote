---
name: stereonote
description: >
  Operate a user-selected DCS / StereoNote server-side JupyterLab workspace after the
  user manually opens the target Personal Analysis workspace and provides its current
  URL. Use this skill for explicitly requested DCS/StereoNote file inspection, bounded
  Python or shell execution, and detached server jobs. Do not trigger merely because a
  generic Linux path such as /data/work appears.
---

# StereoNote JupyterLab controller — v3.2.1

This is a **privileged execution skill**. It drives the user's existing, logged-in
DCS/StereoNote browser session and can execute code with that user's Jupyter
permissions. Treat shell execution and file writes as real server operations, not
as a sandbox.

## Supported controller environment

The local controller is **Windows-native** in v3.2.1:

- Microsoft Edge is open with the compatible kimi-webbridge extension enabled.
- The user is already logged into DCS/StereoNote in that Edge profile.
- `~/.kimi-webbridge/bin/kimi-webbridge.exe` is installed.
- Local Python 3 and `requests` are available.
- The remote DCS/StereoNote Jupyter environment is Linux.

Run a local preflight before first use:

```bash
python scripts/sn.py doctor
```

If `doctor` fails, report the failed prerequisite. Do not improvise SSH, external
ports, VS Code Remote, a fresh browser profile, or credentials.

## Execution path

```text
local Windows controller
  -> kimi-webbridge daemon
  -> CDP navigation in the user's logged-in Edge
  -> inner.dcs.cloud JupyterLab iframe
  -> same-origin Jupyter APIs
  -> Jupyter kernel / server-side subprocesses
```

Browser credentials, cookies, Authorization values and `_xsrf` values must never be
printed or returned. Only operation results should cross the bridge.

## First connection and configuration

The workspace address is runtime state because `projectId` and `workspaceId` vary by
user/workspace.

At the start of each browser/workspace session, ask the user to manually open the
desired Personal Analysis workspace in their normal Edge profile, wait until it has
started, and provide the current address-bar URL. Then run:

```bash
python scripts/sn.py connect --url "<StereoNote workspace URL>"
```

v3.2.1 validates that the URL is HTTPS on `dcs.cloud`, navigates only to that explicit
URL, requires a live Jupyter probe, and only then stores `projectId` and `workspaceId`
in the **per-user config**. Failed connections do not replace the saved IDs. The
complete URL and unrelated query/fragment parameters are never persisted.

Only `connect --url` may create or replace the controlled browser tab. Other commands
must reuse the live tab; if it is missing or closed, ask for the current URL again.

Default Windows config location:

```text
%APPDATA%\stereonote\config.json
```

`SN_CONFIG_DIR` can override the config directory. `SN_URL` and `SN_SESSION` can
override runtime values for one invocation.

For an old checkout containing repository-local `config.json`, v3.2.1 may read it for
migration compatibility but never writes it. Remove that legacy file after the user
has reconnected successfully.

## Core commands

Run commands from the skill repository root:

```bash
python scripts/sn.py connect --url "<current workspace URL>"
python scripts/sn.py probe
python scripts/sn.py ls work/project
python scripts/sn.py cat work/project/script.py
python scripts/sn.py inspect /data/work/project/data.h5ad
python scripts/sn.py run-python --code "print(1 + 1)"
python scripts/sn.py run-shell --cmd "ls -la /data/work | head"
```

`ls`, `cat`, inspectors, shell output and Jupyter stream/error capture are bounded.
Single `write` and `run-python` text payloads are capped at 1 MiB; raw shell command
text is capped at 64 KiB. Oversized executable content fails instead of being silently
truncated. If a result says it was truncated, report that explicitly instead of
treating the preview as complete.

Synchronous Python and shell execution is limited to 50 seconds. Use `submit` for
anything that may run longer.

### Writes are no-clobber by default

```bash
python scripts/sn.py write work/agent_outputs/result.txt --content "..."
```

Creation uses server-side `O_EXCL`, so concurrent no-clobber writers cannot both win.
If the target already exists, the write must fail with
`file_exists_use_overwrite`. Do **not** silently retry with overwrite.

Only after the user explicitly approves replacing that specific file may you use:

```bash
python scripts/sn.py write work/agent_outputs/result.txt --content "..." --overwrite
```

A successful write uses mode `0600` and performs readback verification. Any kernel
failure, wrong target type, or readback mismatch is a hard failure.

## Inspecting bioinformatics files

```bash
python scripts/sn.py inspect /data/work/project/x.h5ad
python scripts/sn.py inspect /data/work/project/x.rds
python scripts/sn.py inspect /data/work/project/x.csv
python scripts/sn.py inspect /data/work/project/x.tsv
python scripts/sn.py inspect /data/work/project/x.parquet
python scripts/sn.py inspect /data/work/project/run.ipynb
python scripts/sn.py inspect /data/work/project/pipeline.R
```

Design constraints:

- `.h5ad` is opened backed; the expression matrix is not transferred.
- tabular and object metadata are clipped to bounded summaries.
- notebooks omit cell outputs and use bounded cell/source previews.
- text previews are byte bounded.
- `.rds` inspection uses a detached server job because loading a large R object may
  be slow or memory intensive.
- inspection is read-only but can read any regular Linux file the DCS account itself
  is permitted to read; this is not a client-side authorization boundary.

## Shell execution

```bash
python scripts/sn.py run-shell --cwd /data/work/project --cmd "python check.py"
```

`run-shell` is **not an OS sandbox**. The command can do anything allowed by the DCS
account. stdout/stderr are captured through disk-backed temporary files and only
bounded tails are returned, so accidental huge output is truncated rather than
loaded wholesale into controller memory.

Never run destructive, installing, privilege-changing, process-killing, or large
data-transfer commands unless the user explicitly requested/approved that action.
Examples requiring explicit approval include `rm`, replacing user data, `pip/conda
install`, `kill`, and bulk downloads/uploads.

## Long-running jobs

Use detached jobs for substantial computation instead of holding an agent/tool call
open:

```bash
python scripts/sn.py submit \
  --cwd /data/work/project \
  --cmd "python big_pipeline.py"

python scripts/sn.py poll <job_id>
python scripts/sn.py artifacts <job_id>
```

Important semantics:

- the payload runs from `--cwd`; default is `/data/work`.
- controller state always lives under `/data/work/agent_jobs/<job_id>/`.
- `$SN_JOB_DIR` points to that job's controller directory.
- `$SN_ARTIFACT_DIR` points to its `artifacts/` directory; long scripts should place
  intended deliverables there when practical.
- submission is reported successful only after the detached runner acknowledges
  readiness and a durable running/finished status exists.
- job status, PID, timestamps, exit code and logs are persisted.
- controller-created job state uses a restrictive `umask 077`.
- files up to 64 MiB receive SHA-256 in artifact inventory; larger files record
  size/mtime with checksum state `skipped_large_file` instead of rereading the
  entire large dataset just to finish bookkeeping.

**Agent behavior for long jobs:** prepare/validate the command, submit it, verify that
submission reached a durable running/finished state, return the `job_id`, then stop.
Do not burn tokens by repeatedly polling a long computation unless the user asked for
a current status in that turn.

## Path conventions

- `work/...` corresponds to server `/data/work/...`.
- file API commands use `work/...`; `run-shell` already starts in `/data/work` by
  default, so shell paths are ordinary Linux paths relative to that directory.
- absolute `/data/...` paths are supported.
- other absolute Linux mounts can be inspected/listed through bounded server-side
  helpers when the DCS account can access them.
- local Windows paths such as `C:/...` are not remote inspect paths.
- `run-shell` and `submit --cmd` are raw server commands and therefore not confined by
  the read-path normalization helpers.

## Hard safety rules

1. Use only the user's current logged-in DCS/StereoNote browser session.
2. Never expose cookie, token, Authorization or `_xsrf` values.
   Never put tokens or passwords directly in `submit --cmd`, because job commands are
   intentionally persisted for reproducibility.
3. Treat `run-shell` and `submit --cmd` as privileged, unsandboxed server execution.
4. Probe/read before mutation when state is uncertain.
5. Never overwrite a user file unless the user explicitly approved that exact
   replacement; rely on default no-clobber enforcement.
6. Do not reinterpret a failed inner operation as success. A nonzero shell exit,
   failed write/read/list, protocol error, or explicit structured error is a failure.
7. Never delete data or install/change environments merely to make a task easier.
8. Do not automatically invoke this skill from generic `/data/work` mentions. Require
   explicit DCS/StereoNote operational intent or explicit skill invocation.
9. For long jobs, submit once and stop after launch verification rather than polling
   continuously.
10. Connect only to the current URL explicitly provided after the user opens the target
    Personal Analysis workspace. Never guess or select another workspace.

## Failure reporting

Report:

- the failed command/operation at a safe level of detail,
- the structured error or bounded stderr tail,
- whether anything was written/started,
- the safest next action.

Do not claim a file, job or command succeeded unless the controller returned a true
success state.
