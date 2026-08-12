# Security model

StereoNote is a privileged controller: when explicitly invoked, it can execute code in the DCS/StereoNote Jupyter environment with the permissions of the currently logged-in user.

## Boundaries

- It does not use SSH, open external server ports, or export browser cookies, `_xsrf` values, or Jupyter tokens.
- The local controller talks to the already logged-in browser through kimi-webbridge and executes same-origin Jupyter operations inside the workspace iframe.
- `write` uses server-side `O_EXCL` for atomic no-clobber; overwriting requires `--overwrite`.
- Job-controller state is rooted at `/data/work/agent_jobs`, but `run-shell` and `submit --cmd` are **not OS sandboxes**. A submitted command can access anything the DCS account can access.
- Job-controller files use `umask 077`, but the submitted command is persisted in `manifest.json` and `cmd.sh`; never place secrets directly in `submit --cmd`.
- The WebBridge endpoint must be loopback HTTP with an explicit port, and Jupyter iframe matching requires the exact `https://inner.dcs.cloud/notebook/st/...` origin/path.
- Codex implicit invocation is disabled through `agents/openai.yaml`. Invoke the skill explicitly when server access is intended.

## Do not put secrets in issues

When reporting a bug, redact workspace identifiers, tokens, cookies, `_xsrf` values, patient identifiers, private paths, and research data. Prefer the output of `python scripts/sn.py doctor` plus a minimal sanitized error message.

## Destructive commands

Do not submit destructive commands (`rm -rf`, bulk moves, package installation, mass downloads, process termination, etc.) unless the user explicitly requested and reviewed them. The controller intentionally does not attempt to infer every dangerous shell command.
