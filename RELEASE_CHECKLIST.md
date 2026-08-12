# Release checklist

- [x] Controller, `SKILL.md`, changelog and intended `v3.2.1` Git tag are consistent.
- [x] `python -m unittest discover -s tests -v` passes locally (**79 tests; 1 POSIX-only test skipped on Windows**).
- [x] `python -m compileall -q scripts tests` passes.
- [x] `node --check scripts/jupyter_runtime.js` passes.
- [x] No repo-local `config.json`, workspace URL, token, cookie, `_xsrf`, patient identifier, or personal username was found in the release scan.
- [x] Test `python scripts/sn.py doctor` on Windows with WebBridge v1.11.1.
- [x] Validate explicit Codex metadata (`$stereonote`, `allow_implicit_invocation: false`) with the official skill validator.
- [ ] Test explicit invocation in Claude Code (`/stereonote`); Claude-specific frontmatter is intentionally not shipped in the OpenAI-valid package.
- [x] Run live connect/probe/read/run-python and atomic no-clobber/0600 write smoke checks in an isolated `agent_outputs` path.
- [ ] Re-run the detached-job permission smoke after the queued DCS container starts; explicit chmod hardening was added after DCS default ACLs defeated `umask 077` alone.
- [x] Release ZIP entries use POSIX `/`, not Windows `\` separators.

Publication decision for v3.2.1: remaining unchecked items are intentionally carried forward as documented maintenance backlog. Do not mark them complete until they are actually verified.
