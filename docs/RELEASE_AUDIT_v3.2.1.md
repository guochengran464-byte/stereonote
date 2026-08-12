# StereoNote v3.2.1 release audit

> **Publication note:** This document preserves the RC1 audit state. The maintainer elected to publish v3.2.1 with the remaining items documented as known limitations / maintenance backlog rather than silently marking them complete.

Date: 2026-08-12

## Verdict

**Release candidate built; final public tag remains blocked by one live verification.**

The code, unit tests, OpenAI skill schema, archive layout, and core live DCS operations pass. The DCS shared filesystem was observed to defeat `umask 077` through its default ACLs, so v3.2.1 now applies explicit `chmod 700/600`. That exact post-fix job-permission path still needs one live rerun after the currently queued 32c/256g container starts.

## Implemented changes

- `SKILL.md` frontmatter now contains only `name` and `description`.
- `agents/openai.yaml` has a 25–64 character description, a `$stereonote` prompt, and `allow_implicit_invocation: false`.
- `connect --url` force-navigates only to the URL supplied for that invocation, recognizes both `no tab` and `tab was closed`, probes Jupyter before saving IDs, and does not save failed connections.
- Ordinary operations never silently navigate from stale saved IDs.
- The WebBridge origin is restricted to loopback HTTP with an explicit port.
- Iframe matching requires exact HTTPS `inner.dcs.cloud` plus `/notebook/st/` path.
- `write` uses server-side `O_EXCL` atomic no-clobber, `O_NOFOLLOW`, mode `0600`, and readback verification.
- Synchronous Python and shell commands are capped at 50 seconds; longer work is directed to detached jobs.
- Job state uses `umask 077` plus explicit `chmod 700/600`; artifact permissions are tightened again during inventory.
- CLI help documents `/data/work` versus `work/...` path bases and the job-label regex.

## Local validation

- Python compile: pass.
- Unit tests: 79 passed; 1 POSIX-only launcher integration test skipped on Windows.
- Node syntax check: pass.
- Official `quick_validate.py`: pass with `PYTHONUTF8=1` (the script otherwise uses the Windows GBK locale for UTF-8 `SKILL.md`).
- Privacy scan: no live project ID, workspace ID, local username, GitHub token, or OpenAI-style token in the package.
- Archive: 21 entries, zero backslash entries, zero `__pycache__`, zero user `config.json`.

## Live DCS validation

Passed before the container returned to queue:

- Windows `doctor`, WebBridge v1.11.1, loopback validation.
- Explicit URL connection and live iframe probe.
- Jupyter status/root/work HTTP 200 and writable work directory.
- Bounded directory read and `run-python` output.
- Atomic new-file write, second-write rejection, unchanged readback, and verified file mode `0600`.

Live issue found and fixed in code:

- DCS default ACLs produced job directory/file modes `755/644` despite `umask 077`.
- Explicit `chmod 700/600` was added after observing this.

Pending:

- Re-run a detached job and verify job directory `0700`, manifest/cmd/log files `0600`, artifact `0600`, finished status, and artifact SHA-256. The target page currently reports “所需资源正在调度中”, so no Jupyter iframe exists.

## Candidate archive

- File: `stereonote-v3.2.1-public-rc1.zip`
- Size: 48,214 bytes
- SHA-256: `E12D027FBE929E826082C27E7056945FC6FFBCD032484FDC0BAEDEDEF8CA6EC9`

## Live artifacts left intentionally

- `/data/work/agent_outputs/stereonote_v3_2_1_smoke_20260812_b41e7d92.txt` — atomic-write smoke file, 41 bytes, mode `0600`.
- `/data/work/agent_jobs/smoke_321_9ea58fba-7524-48aa-bb09-1c8d8fb35b7f/` — pre-fix job used to detect the ACL issue; its artifact is not valid release evidence because the Windows command argument was split.

No existing research file was overwritten or deleted. The currently installed v3.2.0 skill was not replaced.
