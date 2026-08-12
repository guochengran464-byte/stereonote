# Compatibility

StereoNote v3.2.1 has two separate environments:

1. **Local controller:** Windows + Edge + kimi-webbridge + Python.
2. **Remote target:** the Linux JupyterLab environment inside DCS/StereoNote.

## kimi-webbridge contract

This skill was developed against the **v1.x-style kimi-webbridge behavior** used by
the original deployment. It expects:

- local daemon on `http://127.0.0.1:10086` by default;
- `GET /status` returning daemon/extension connection state;
- `POST /command` support for the existing command envelope;
- browser actions used by the controller, including `navigate`, `cdp`, and
  `close_tab`;
- CDP access to the user's existing Edge session.

The controller does not assume that an arbitrary future WebBridge version is
compatible. In particular, do not upgrade the bridge solely because a newer major
version exists without running the smoke checks below.

`python scripts/sn.py doctor` reports the daemon state/version fields exposed by the
installed bridge, but v3.2.1 does not auto-install or auto-upgrade WebBridge.

## Required release smoke test

Before publishing a release as verified for a new WebBridge build, test on a
non-production DCS workspace:

```bash
python scripts/sn.py doctor
python scripts/sn.py connect --url "<current workspace URL>"
python scripts/sn.py probe
python scripts/sn.py ls work
python scripts/sn.py write work/agent_outputs/stereonote_smoke.txt --content "smoke"
python scripts/sn.py write work/agent_outputs/stereonote_smoke.txt --content "must-fail"
python scripts/sn.py submit --cwd /data/work --cmd "printf ok > \"$SN_ARTIFACT_DIR/smoke.txt\""
python scripts/sn.py poll <job_id>
python scripts/sn.py artifacts <job_id>
```

The second `write` must fail with `file_exists_use_overwrite`. Do not use a directory
containing research outputs for this smoke test.

## Support matrix

| Component | v3.2.1 status |
|---|---|
| Windows native controller | Supported |
| Microsoft Edge | Supported controller browser |
| DCS/StereoNote Linux Jupyter | Supported target |
| kimi-webbridge v1.x-style `/status` + `/command` contract | Expected/required |
| macOS controller | Not verified |
| Linux desktop controller | Not verified |
| WSL as controller | Not verified |
| arbitrary future/major WebBridge versions | Not verified |

When opening an issue about compatibility, include sanitized `doctor` output and the
bridge version/status fields, but never include tokens, cookies, workspace IDs or
research data.
