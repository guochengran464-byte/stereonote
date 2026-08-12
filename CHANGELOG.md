# Changelog

## 3.2.1 - 2026-08-12

Workflow-correctness and public-format release.

- Require the user to manually open Personal Analysis and provide its current URL; only `connect --url` may navigate.
- Recreate a WebBridge tab after both `no tab` and `tab was closed`, using only the explicit URL supplied for that connection.
- Save workspace identifiers only after a live Jupyter probe succeeds.
- Strictly match the `inner.dcs.cloud/notebook/st/` iframe and restrict the daemon endpoint to loopback HTTP with an explicit port.
- Replace the Contents API check-then-PUT write with server-side `O_EXCL` atomic no-clobber, mode `0600`, and readback verification.
- Apply `umask 077` to job-controller state and document that submitted commands are persisted and must not contain secrets.
- Keep synchronous Python/shell work within 50 seconds; direct longer work to detached `submit` jobs.
- Conform `SKILL.md` frontmatter and `agents/openai.yaml` to the current OpenAI skill schema.
- Document file-API versus shell path bases and the accepted `submit --name` pattern.

## 3.2.0 - 2026-08-12

Public-release hardening release.

### Security and correctness

- Propagate Jupyter `list` / `read` / `write` failures to the outer operation result instead of reporting false success.
- Make `write` no-clobber by default. Replacing an existing file now requires explicit `--overwrite`.
- Persist only `projectId` and `workspaceId` in the per-user config; arbitrary URL query parameters and tokens are discarded.
- Move runtime config out of the Git checkout to the user's config directory.
- Disable implicit invocation by default for Codex and Claude Code because this skill can execute server-side code.

### Long jobs and bounded I/O

- Add `submit --cwd`; long jobs now default to `/data/work`, so commands such as `python big.py` resolve predictably.
- Export `SN_JOB_DIR` and `SN_ARTIFACT_DIR` to submitted commands.
- Bound `run-shell` output using temporary files instead of `capture_output=True` in memory.
- Bound Jupyter kernel stream/result/error accumulation.
- Reject write/Python payloads larger than 1 MiB and raw shell commands larger than 64 KiB instead of moving accidental huge text through the bridge.
- Make `ls` and `cat` use bounded server-side implementations.
- Skip full SHA-256 hashing for artifacts larger than 64 MiB; record size, mtime and an explicit `skipped_large_file` checksum state instead.

### Distribution

- Correct Codex user skill path to `$HOME/.agents/skills/stereonote`.
- Add `agents/openai.yaml`, MIT license, CI, release checklist, requirements, and `.gitignore`.
- Add public-release regression and local launcher integration tests, including post-navigation Jupyter liveness and CLI failure-exit semantics.
- Normalize the release archive to POSIX `/` entry separators.
