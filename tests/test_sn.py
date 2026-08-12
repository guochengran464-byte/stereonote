import importlib.util
import base64
import contextlib
import io
import json
import os
import re
import tempfile
import subprocess
import shutil
import time
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sn.py"
SPEC = importlib.util.spec_from_file_location("sn", SCRIPT)
sn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sn)


class JobValidationTests(unittest.TestCase):
    def test_validate_job_id_accepts_canonical_uuid_id(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"

        self.assertEqual(sn.validate_job_id(job_id), job_id)

    def test_validate_job_id_rejects_shell_metacharacters(self):
        for value in ("job;id", "job id", "job$(whoami)", "job/../x", "job`id`", "job|cat"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sn.validate_job_id(value)

    def test_make_job_id_returns_a_uuid_based_id(self):
        job_id = sn.make_job_id()

        self.assertRegex(job_id, r"^job_[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertEqual(sn.validate_job_id(job_id), job_id)

    def test_validate_tail_bounds_values(self):
        self.assertEqual(sn.validate_tail(1), 1)
        self.assertEqual(sn.validate_tail("40"), 40)
        self.assertEqual(sn.validate_tail(200), 200)
        for value in (0, -1, 201, "many", "40; rm -rf /"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sn.validate_tail(value)


class JobLauncherTests(unittest.TestCase):
    def test_launcher_creates_durable_job_record_files(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launcher = sn.build_job_launcher(job_id, "python analysis.py")

        self.assertIn("setsid", launcher)
        self.assertIn("status.json", launcher)
        self.assertIn('"job_550e8400-e29b-41d4-a716-446655440000"', launcher)
        self.assertIn("stdout.log", launcher)
        self.assertIn("stderr.log", launcher)
        self.assertIn("exit_code", launcher)
        self.assertIn("python analysis.py", launcher)

    def test_launcher_uses_a_unique_heredoc_delimiter_for_command_content(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        command = "echo first\n__SN_COMMAND__\necho still-in-command"
        launcher = sn.build_job_launcher(job_id, command)

        match = re.search(r"cmd\.sh <<'([^']+)'\n(.*?)\n\1\n", launcher, re.DOTALL)
        self.assertIsNotNone(match)
        self.assertNotEqual(match.group(1), "__SN_COMMAND__")
        self.assertEqual(match.group(2), command)

    def test_launcher_creates_job_directory_exclusively(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launcher = sn.build_job_launcher(job_id, "echo safe")

        self.assertIn(f"mkdir {sn.JOB_ROOT}/{job_id}", launcher)
        self.assertNotIn("mkdir -p", launcher)

    def test_launcher_stops_on_setup_failures_and_prepares_artifacts(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launcher = sn.build_job_launcher(job_id, "pwd > working-directory.txt")
        setup = launcher.split("cat >", 1)[0]

        self.assertIn("set -e", launcher)
        self.assertIn(f"mkdir {sn.JOB_ROOT}/{job_id}/artifacts", launcher)
        self.assertNotIn("status.json.tmp && mv", setup)
        self.assertLess(launcher.index("mkdir " + sn.JOB_ROOT + "/" + job_id + "/artifacts"),
                        launcher.index("setsid"))

    def test_launcher_explicitly_restricts_controller_and_artifact_permissions(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launcher = sn.build_job_launcher(job_id, "echo safe")
        self.assertIn("umask 077", launcher)
        self.assertIn(f"chmod 700 {sn.JOB_ROOT}/{job_id}", launcher)
        self.assertIn(f"chmod 700 {sn.JOB_ROOT}/{job_id}/artifacts", launcher)
        self.assertIn(f"chmod 600 {sn.JOB_ROOT}/{job_id}/manifest.json", launcher)
        self.assertIn("os.chmod(path, 0o600)", launcher)

    def test_launcher_runs_payload_from_explicit_server_cwd_and_exports_job_dirs(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launcher = sn.build_job_launcher(job_id, "pwd", cwd="/data/work/project")

        self.assertIn("cd /data/work/project || exit 72", launcher)
        self.assertIn(f"export SN_JOB_DIR={sn.JOB_ROOT}/{job_id}", launcher)
        self.assertIn(f"export SN_ARTIFACT_DIR={sn.JOB_ROOT}/{job_id}/artifacts", launcher)
        self.assertIn('"cwd":"/data/work/project"', launcher)


class JobLifecycleTests(unittest.TestCase):
    def test_submit_uses_a_generated_validated_id_and_durable_launcher(self):
        job_id = "analysis_550e8400-e29b-41d4-a716-446655440000"
        with patch.object(sn, "make_job_id", return_value=job_id) as make_id, \
             patch.object(sn, "build_job_launcher", return_value="safe launcher") as build, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": f"__SN_SUBMITTED__={job_id}",
                                                           "stderr": ""}) as run, \
             patch.object(sn, "poll", return_value={"status": {"state": "running"}}) as poll:
            result = sn.submit("python analysis.py", name="analysis")

        make_id.assert_called_once_with("analysis")
        build.assert_called_once_with(job_id, "python analysis.py", cwd=None)
        run.assert_called_once_with("safe launcher", cwd=sn.WORK_ROOT, timeout=sn.MAX_SYNC_SECONDS)
        poll.assert_called_once_with(job_id, tail=1)
        self.assertEqual(result["job_id"], job_id)
        self.assertEqual(result["dir"], f"{sn.JOB_ROOT}/{job_id}")

    def test_submit_raises_when_launcher_setup_fails(self):
        with patch.object(sn, "make_job_id", return_value="job_550e8400-e29b-41d4-a716-446655440000"), \
             patch.object(sn, "build_job_launcher", return_value="launcher"), \
             patch.object(sn, "run_shell", return_value={"ok": False, "returncode": 1,
                                                           "stderr": "mkdir: permission denied"}):
            with self.assertRaisesRegex(sn.Bridge, "job_setup_failed"):
                sn.submit("echo work")

    def test_rds_inspection_polls_the_id_returned_by_submit(self):
        job_id = "rdsinspect_550e8400-e29b-41d4-a716-446655440000"
        with patch.object(sn, "submit", return_value={"job_id": job_id}) as submit, \
             patch.object(sn, "poll", return_value={
                 "status": {"state": "finished", "exit_code": 0},
                 "stdout_tail": "INSPECT_JSON={\"type\": \"generic\"}",
                 "stderr_tail": "", "read_errors": {},
             }) as poll:
            result = sn.inspect_rds("/data/work/input.rds", timeout=1)

        self.assertEqual(result, {"type": "generic"})
        self.assertEqual(poll.call_args.args[0], job_id)
        self.assertIn("Rscript -e", submit.call_args.args[0])

    def test_poll_rejects_invalid_job_id_and_tail_before_remote_shell(self):
        with patch.object(sn, "run_shell") as run:
            with self.assertRaises(ValueError):
                sn.poll("job; rm -rf /", tail=40)
            with self.assertRaises(ValueError):
                sn.poll("job_550e8400-e29b-41d4-a716-446655440000", tail="1;id")
        run.assert_not_called()

    def test_poll_reads_status_and_bounded_log_tails(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        status = base64.b64encode(b'{"state":"finished","exit_code":0}').decode()
        stdout = base64.b64encode(b'last output').decode()
        stderr = base64.b64encode(b'last error').decode()
        raw = "\n".join((f"__SN_POLL_fixed__ status ok {status}",
                          f"__SN_POLL_fixed__ stdout ok {stdout}",
                          f"__SN_POLL_fixed__ stderr ok {stderr}"))
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": raw, "stderr": ""}) as run:
            make_uuid.return_value.hex = "fixed"
            result = sn.poll(job_id, tail=7)

        command = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs.get("max_bytes"), sn.MAX_INTERNAL_SHELL_BYTES)
        self.assertIn("status.json", command)
        self.assertIn("tail -n 7", command)
        self.assertIn("tail -c", command)
        self.assertIn("stdout.log", command)
        self.assertIn("stderr.log", command)
        self.assertEqual(result, {"job_id": job_id, "tail": 7,
                                  "status": {"state": "finished", "exit_code": 0},
                                  "stdout_tail": "last output", "stderr_tail": "last error",
                                  "missing": {"status": False, "stdout": False, "stderr": False},
                                  "read_errors": {}})

    def test_poll_reports_missing_and_invalid_status_without_mixing_log_headings(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        invalid = base64.b64encode(b'not json').decode()
        raw = "\n".join((f"__SN_POLL_fixed__ status ok {invalid}",
                          "__SN_POLL_fixed__ stdout missing ",
                          "__SN_POLL_fixed__ stderr missing "))
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": raw, "stderr": ""}):
            make_uuid.return_value.hex = "fixed"
            result = sn.poll(job_id)

        self.assertIsNone(result["status"])
        self.assertEqual(result["stdout_tail"], "")
        self.assertEqual(result["stderr_tail"], "")
        self.assertEqual(result["missing"], {"status": False, "stdout": True, "stderr": True})
        self.assertIn("status", result["read_errors"])


class ArtifactInventoryTests(unittest.TestCase):
    def test_artifacts_rejects_invalid_ids_before_remote_execution(self):
        with patch.object(sn, "run_python") as run:
            with self.assertRaises(ValueError):
                sn.artifacts("job/../escape")
        run.assert_not_called()

    def test_artifacts_returns_only_regular_files_with_size_and_sha256(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        payload = {"files": [{"path": "result.txt", "size": 5,
                              "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"}]}
        with patch.object(sn, "run_python", return_value={"data": {"stdout": "ARTIFACTS_JSON=" + json.dumps(payload)}}) as run:
            result = sn.artifacts(job_id)

        code = run.call_args.args[0]
        self.assertIn("artifacts", code)
        self.assertIn("S_ISREG", code)
        self.assertEqual(result, {"job_id": job_id, "files": payload["files"]})

    def test_oversized_inventory_propagates_explicit_limit_error(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        payload = {"files": [{"path": "x" * sn.MAX_INSPECT_RESULT_BYTES,
                               "size": 1, "sha256": "0" * 64}]}
        response = {"data": {"stdout": "ARTIFACTS_JSON=" + json.dumps(payload)}}
        with patch.object(sn, "run_python", return_value=response):
            result = sn.artifacts(job_id)

        self.assertEqual(result, {
            "job_id": job_id,
            "error": "bounded_summary_too_large",
            "limit_bytes": sn.MAX_INSPECT_RESULT_BYTES,
        })
        self.assertNotIn("files", result)
        self.assertIn("MAX_RESULT_BYTES", sn._ARTIFACT_INVENTORY)

    def test_inventory_rejects_a_symlinked_artifacts_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            root = os.path.join(tmp, "artifacts")
            os.mkdir(target)
            with open(os.path.join(target, "outside.txt"), "w", encoding="utf-8") as fh:
                fh.write("outside")
            try:
                os.symlink(target, root, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(sn._ARTIFACT_INVENTORY % {"root": root}, {})

        payload = json.loads(output.getvalue().split("=", 1)[1])
        self.assertEqual(payload["files"], [])

    def test_cli_artifacts_dispatches_to_inventory(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        with patch.object(sn, "artifacts", return_value={"job_id": job_id, "files": []}) as inventory, \
             patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["artifacts", job_id]), 0)
        inventory.assert_called_once_with(job_id)
        output.assert_called_once_with({"job_id": job_id, "files": []})


class BoundedInspectionTests(unittest.TestCase):
    def test_text_preview_uses_server_side_byte_limit(self):
        with patch.object(sn, "run_python", return_value={"data": {"stdout": (
                'INSPECT_JSON={"format":"text","path":"/data/work/a.py",'
                '"bytes_read":8,"truncated":true,"head":"print(1)"}'
        )}}) as run:
            result = sn.inspect_text("/data/work/a.py", max_bytes=8)

        self.assertEqual(result["bytes_read"], 8)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["head"], "print(1)")
        code = run.call_args.args[0]
        self.assertIn("read(limit + 1)", code)
        self.assertIn("/data/work/a.py", code)
        self.assertNotIn("evaluate_op", str(run.mock_calls))

    def test_text_preview_rejects_limits_outside_the_bound(self):
        with patch.object(sn, "run_python") as run:
            with self.assertRaises(ValueError):
                sn.inspect_text("/data/work/a.py", max_bytes=0)
            with self.assertRaises(ValueError):
                sn.inspect_text("/data/work/a.py", max_bytes=sn.MAX_INSPECT_BYTES + 1)
        run.assert_not_called()

    def test_notebook_summary_is_server_side_and_omits_outputs(self):
        payload = {"format": "ipynb", "path": "/data/work/a.ipynb", "nbformat": 4,
                   "kernel": "python3", "n_cells": 1,
                   "cells": [{"i": 0, "type": "code", "lines": 1, "preview": "x = 1"}]}
        with patch.object(sn, "run_python", return_value={"data": {"stdout": "INSPECT_JSON=" + json.dumps(payload)}}) as run:
            result = sn.inspect_ipynb("/data/work/a.ipynb")

        self.assertEqual(result, payload)
        code = run.call_args.args[0]
        self.assertIn("read(limit + 1)", code)
        self.assertIn("pop('outputs', None)", code)
        self.assertNotIn("api/contents", code)

    def test_inspect_routes_notebooks_and_text_to_bounded_helpers(self):
        with patch.object(sn, "inspect_ipynb", return_value={"format": "ipynb"}) as notebook, \
             patch.object(sn, "inspect_text", return_value={"format": "text"}) as text:
            self.assertEqual(sn.inspect("work/a.ipynb"), {"format": "ipynb"})
            self.assertEqual(sn.inspect("work/a.py"), {"format": "text"})
        notebook.assert_called_once_with("work/a.ipynb")
        text.assert_called_once_with("work/a.py")


class InspectPathSafetyTests(unittest.TestCase):
    def test_server_paths_can_read_any_linux_path_the_dcs_account_can_access(self):
        self.assertEqual(sn.to_server("work/a.h5ad"), "/data/work/a.h5ad")
        self.assertEqual(sn.to_server("a.h5ad"), "/data/work/a.h5ad")
        self.assertEqual(sn.to_server("/data/work/a.h5ad"), "/data/work/a.h5ad")
        self.assertEqual(sn.to_server("/data/users/alice/input.h5ad"),
                         "/data/users/alice/input.h5ad")
        self.assertEqual(sn.to_server("/mnt/reference/genes.tsv"), "/mnt/reference/genes.tsv")
        self.assertEqual(sn.to_server("/data/work/../users/alice/input.h5ad"),
                         "/data/users/alice/input.h5ad")
        for path in ("C:/secret.h5ad", "\x00bad"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "invalid_inspect_path|inspect_path_not_linux_path"):
                    sn.to_server(path)

    def test_contents_paths_map_server_data_paths_to_the_jupyter_contents_root(self):
        self.assertEqual(sn.to_contents("/data/work/a.py"), "work/a.py")
        self.assertEqual(sn.to_contents("/data/users/alice/a.ipynb"),
                         "users/alice/a.ipynb")

    def test_external_absolute_paths_bypass_the_jupyter_contents_root(self):
        self.assertIsNone(sn.contents_path("/mnt/reference/genes.tsv"))
        self.assertEqual(sn.contents_path("/data/users/alice/a.ipynb"),
                         "users/alice/a.ipynb")

    def test_ls_uses_a_server_kernel_for_an_external_absolute_directory(self):
        payload = {"format": "directory", "path": "/mnt/reference", "entries": []}
        with patch.object(sn, "run_python", return_value={"data": {"stdout": "LIST_JSON=" + json.dumps(payload)}}) as run:
            self.assertEqual(sn.list_path("/mnt/reference"), payload)
        self.assertIn("/mnt/reference", run.call_args.args[0])

    def test_external_directory_listing_is_streaming_and_reports_truncation(self):
        self.assertIn("itertools.islice(os.scandir(p), MAX_ENTRIES + 1)", sn._SERVER_LISTING)
        self.assertIn('"truncated": truncated', sn._SERVER_LISTING)
        self.assertNotIn("sorted(os.scandir(p)", sn._SERVER_LISTING)

    def test_cat_uses_the_bounded_server_reader_for_an_external_absolute_file(self):
        expected = {"format": "text", "path": "/mnt/reference/genes.tsv", "head": "gene"}
        with patch.object(sn, "inspect_text", return_value=expected) as inspect_text:
            self.assertEqual(sn.cat_path("/mnt/reference/genes.tsv"), expected)
        inspect_text.assert_called_once_with("/mnt/reference/genes.tsv", max_bytes=sn.DEFAULT_TEXT_PREVIEW_BYTES)

    def test_inspect_uses_an_external_path_without_rejecting_it_before_kernel_execution(self):
        with patch.object(sn, "run_python", return_value={"data": {"stdout": "INSPECT_JSON={\"format\":\"table\"}"}}) as run:
            self.assertEqual(sn.inspect("/data/users/alice/a.csv"), {"format": "table"})
        self.assertIn("/data/users/alice/a.csv", run.call_args.args[0])

    def test_csv_inspection_does_not_interpolate_paths_into_a_shell(self):
        self.assertNotIn("subprocess", sn.TABLE)
        self.assertNotIn("shell=True", sn.TABLE)
        self.assertNotIn("wc -l", sn.TABLE)

    def test_server_inspectors_resolve_paths_and_require_regular_files_without_a_root_allowlist(self):
        for source in (sn.H5AD, sn.TABLE, sn.PARQUET, sn._TEXT_PREVIEW, sn._NOTEBOOK_SUMMARY):
            with self.subTest(source=source[:20]):
                self.assertIn("realpath", source)
                self.assertIn("inspect_path_not_regular_file", source)
                self.assertNotIn("inspect_path_outside_work_root", source)


class FailurePropagationTests(unittest.TestCase):
    def test_evaluate_op_raises_for_inner_operation_failure(self):
        response = {"result": {"value": {"ok": False, "error": "kernel_failed"}}}
        with patch.object(sn, "ensure_daemon"), patch.object(sn, "ensure_iframe", return_value="f"), \
             patch.object(sn, "make_context", return_value=1), patch.object(sn, "cdp", return_value=response):
            with self.assertRaisesRegex(sn.Bridge, "kernel_failed"):
                sn.evaluate_op("run_python", {"code": "raise RuntimeError()"})

    def test_run_shell_requires_a_decodable_frame_and_integer_return_code(self):
        with patch.object(sn, "evaluate_op", return_value={"ok": True, "data": {"stdout": "unframed"}}):
            result = sn.run_shell("true")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["returncode"])
        self.assertIn("shell_protocol_error", result["stderr"])

    def test_run_shell_marks_nonzero_exit_as_failure(self):
        payload = base64.b64encode(json.dumps({
            "returncode": 7, "stdout": "", "stderr": "bad", "timedOut": False,
        }).encode()).decode()
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "evaluate_op", return_value={"ok": True, "data": {
                 "stdout": "__SN_SHELL_fixed__:" + payload}}):
            make_uuid.return_value.hex = "fixed"
            result = sn.run_shell("exit 7")
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 7)

    def test_runtime_propagates_kernel_error_and_timeout(self):
        source = (Path(sn.RUNTIME_JS_PATH).read_text(encoding="utf-8"))
        self.assertIn("out.ok = false", source)
        self.assertIn("ok: !!data.ok", source)

    def test_submit_requires_exact_launcher_acknowledgement(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        with patch.object(sn, "make_job_id", return_value=job_id), \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": "unverified", "stderr": ""}):
            with self.assertRaisesRegex(sn.Bridge, "launcher_ack_missing"):
                sn.submit("echo work")

    def test_submit_rejects_outer_ack_when_runner_never_reaches_running(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launch = {"ok": True, "returncode": 0,
                  "stdout": f"__SN_SUBMITTED__={job_id}", "stderr": ""}
        marked = {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
        missing = {"status": None, "missing": {"status": True}, "read_errors": {},
                   "stdout_tail": "", "stderr_tail": ""}
        with patch.object(sn, "make_job_id", return_value=job_id), \
             patch.object(sn, "run_shell", side_effect=[launch, marked]) as run, \
             patch.object(sn, "poll", return_value=missing) as poll, \
             patch.object(sn.time, "monotonic", side_effect=[0.0, 1.0]), \
             patch.object(sn.time, "sleep"):
            with self.assertRaisesRegex(sn.Bridge, "runner_not_ready"):
                sn.submit("echo work", readiness_timeout=0.5)
        poll.assert_called_once_with(job_id, tail=1)
        self.assertIn('"state":"setup_error"', run.call_args_list[-1].args[0])


class DurableRunnerTests(unittest.TestCase):
    def test_controller_job_root_is_fixed_and_not_derived_from_config(self):
        self.assertEqual(sn.JOB_ROOT, "/data/work/agent_jobs")

    def test_launcher_rejects_symlinked_controller_roots(self):
        launcher = sn.build_job_launcher("job_550e8400-e29b-41d4-a716-446655440000", "true")
        root_check = launcher.index("-L /data/work/agent_jobs")
        job_creation = launcher.index("mkdir /data/work/agent_jobs/job_")
        self.assertLess(root_check, job_creation)

    def test_runner_persists_complete_lifecycle_and_artifact_manifest(self):
        launcher = sn.build_job_launcher("job_550e8400-e29b-41d4-a716-446655440000", "true")
        for required in ("manifest.json", "pid", "started_at", "ended_at", "exit_code",
                         "trap finalize EXIT", "trap 'exit 143' TERM", "trap 'exit 130' INT",
                         "artifacts.json", "status.json.tmp.$$"):
            with self.subTest(required=required):
                self.assertIn(required, launcher)
        self.assertLess(launcher.index("artifacts.json.tmp.$$"),
                        launcher.rindex("status.json.tmp.$$"))

    def test_launcher_only_acknowledges_after_runner_readiness_and_marks_timeout(self):
        launcher = sn.build_job_launcher("job_550e8400-e29b-41d4-a716-446655440000", "true")
        self.assertIn("runner.ready", launcher)
        self.assertIn("runner_start_timeout", launcher)
        self.assertIn('"state":"setup_error"', launcher)
        self.assertLess(launcher.index("runner.ready"), launcher.rindex("__SN_SUBMITTED__="))


class BoundedStructuredInspectorsTests(unittest.TestCase):
    def test_all_structured_inspectors_limit_collection_and_value_sizes(self):
        for source in (sn.H5AD, sn.TABLE, sn.PARQUET, sn.RDS_R):
            with self.subTest(source=source[:20]):
                self.assertIn("MAX_ITEMS", source)
                self.assertIn("MAX_VALUE", source)

    def test_inspect_json_parser_rejects_oversized_payloads(self):
        oversized = "INSPECT_JSON=" + json.dumps({"value": "x" * sn.MAX_INSPECT_RESULT_BYTES})
        self.assertEqual(sn._pull_json(oversized), {
            "error": "bounded_summary_too_large",
            "limit_bytes": sn.MAX_INSPECT_RESULT_BYTES,
        })

    def test_notebook_summary_uses_named_cell_and_value_limits(self):
        self.assertIn("MAX_CELLS", sn._NOTEBOOK_SUMMARY)
        self.assertIn("MAX_VALUE", sn._NOTEBOOK_SUMMARY)

    def test_clipped_consumes_only_the_requested_generator_prefix(self):
        namespace = {}
        exec(sn._INSPECT_PRELUDE, namespace)
        consumed = []

        def values():
            for value in range(10000):
                consumed.append(value)
                yield value

        self.assertEqual(namespace["clipped"](values(), 3), ["0", "1", "2"])
        self.assertEqual(consumed, [0, 1, 2])

    def test_oversized_valid_inspection_result_returns_explicit_limit_error(self):
        payload = {"format": "table", "path": "/data/work/a.csv",
                   "head": ["x" * sn.MAX_INSPECT_RESULT_BYTES]}
        response = {"data": {"stdout": "INSPECT_JSON=" + json.dumps(payload)}}
        with patch.object(sn, "run_python", return_value=response):
            result = sn.inspect("/data/work/a.csv")
        self.assertEqual(result, {"error": "bounded_summary_too_large",
                                  "limit_bytes": sn.MAX_INSPECT_RESULT_BYTES})

    def test_every_inspector_emits_one_globally_byte_bounded_json_result(self):
        for source in (sn.H5AD, sn.TABLE, sn.PARQUET,
                       sn._TEXT_PREVIEW, sn._NOTEBOOK_SUMMARY):
            with self.subTest(source=source[:20]):
                self.assertIn("MAX_RESULT_BYTES", source)
                self.assertIn("emit_summary", source)
        self.assertIn("MAX_RESULT_BYTES", sn.RDS_R)
        self.assertIn("bounded_summary_too_large", sn.RDS_R)
        self.assertIn('INSPECT_JSON=', sn.RDS_R)


class BoundedPollProtocolTests(unittest.TestCase):
    def test_poll_uses_byte_caps_base64_and_a_per_call_frame(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": "", "stderr": ""}) as run:
            make_uuid.return_value.hex = "abc123"
            sn.poll(job_id, tail=7)
        command = run.call_args.args[0]
        self.assertIn("__SN_POLL_abc123__", command)
        self.assertIn("head -c", command)
        self.assertIn("tail -c", command)
        self.assertIn("base64", command)
        self.assertIn(str(sn.MAX_STATUS_BYTES + 1), command)
        self.assertIn(str(sn.MAX_LOG_BYTES), command)

    def test_poll_decodes_base64_without_marker_collisions(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        status = base64.b64encode(b'{"state":"finished","exit_code":0}').decode()
        stdout = base64.b64encode(b'line\n__SN_STDOUT_END__\nstill data').decode()
        stderr = base64.b64encode(b'').decode()
        raw = "\n".join((f"__SN_POLL_fixed__ status ok {status}",
                          f"__SN_POLL_fixed__ stdout ok {stdout}",
                          f"__SN_POLL_fixed__ stderr ok {stderr}"))
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": raw, "stderr": ""}):
            make_uuid.return_value.hex = "fixed"
            result = sn.poll(job_id)
        self.assertEqual(result["stdout_tail"], "line\n__SN_STDOUT_END__\nstill data")
        self.assertEqual(result["status"]["exit_code"], 0)

class PublicReleaseSafetyTests(unittest.TestCase):
    def test_workspace_url_is_canonicalized_and_drops_unrelated_parameters(self):
        url = ("https://www.dcs.cloud/stereonote/?token=SECRET#/notebookEmbed?"
               "projectId=p-123&workspaceId=w-456&token=ALSO_SECRET&extra=x")
        canonical = sn.canonical_workspace_url(url)
        self.assertEqual(canonical,
                         "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p-123&workspaceId=w-456")
        self.assertNotIn("SECRET", canonical)
        self.assertNotIn("extra", canonical)

    def test_workspace_url_rejects_non_dcs_hosts(self):
        with self.assertRaisesRegex(ValueError, "dcs_cloud"):
            sn.canonical_workspace_url(
                "https://evil.example/#/notebookEmbed?projectId=p&workspaceId=w")

    def test_daemon_url_is_restricted_to_loopback_http_with_a_port(self):
        for value in ("http://127.0.0.1:10086", "http://localhost:10086", "http://[::1]:10086"):
            with self.subTest(value=value), patch.object(sn, "DAEMON", value):
                self.assertEqual(sn.daemon_base(), value.rstrip("/"))
        for value in ("https://127.0.0.1:10086", "http://evil.example:10086",
                      "http://127.0.0.1", "http://127.0.0.1:10086/path"):
            with self.subTest(value=value), patch.object(sn, "DAEMON", value):
                with self.assertRaisesRegex(sn.Bridge, "loopback"):
                    sn.daemon_base()

    def test_save_url_writes_only_ids_to_user_config(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"SN_CONFIG_DIR": tmp}, clear=False):
            path = Path(tmp) / "config.json"
            with patch.object(sn, "config_path", return_value=str(path)):
                sn.save_url("https://www.dcs.cloud/stereonote/#/notebookEmbed?"
                            "projectId=p123&workspaceId=w456&token=SECRET")
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["project_id"], "p123")
        self.assertEqual(data["workspace_id"], "w456")
        self.assertNotIn("workspace_url", data)
        self.assertNotIn("SECRET", json.dumps(data))

    def test_atomic_write_uses_exclusive_create_private_mode_and_readback(self):
        payload = {"ok": True, "path": "/data/work/out.txt", "bytes": 5, "overwritten": False}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        response = {"data": {"stdout": "__SN_WRITE_fixed__:" + encoded}}
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "evaluate_op", return_value=response) as evaluate:
            make_uuid.return_value.hex = "fixed"
            result = sn.write_text("work/out.txt", "hello")
        code = evaluate.call_args.args[1]["code"]
        self.assertIn("os.O_EXCL", code)
        self.assertIn("os.O_NOFOLLOW", code)
        self.assertIn("os.fchmod(_fd, 0o600)", code)
        self.assertIn("write_readback_mismatch", code)
        self.assertEqual(result["data"], payload)

    def test_atomic_write_propagates_file_exists_as_failure(self):
        payload = {"ok": False, "error": "file_exists_use_overwrite", "path": "/data/work/out.txt"}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        response = {"data": {"stdout": "__SN_WRITE_fixed__:" + encoded}}
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "evaluate_op", return_value=response):
            make_uuid.return_value.hex = "fixed"
            with self.assertRaisesRegex(sn.Bridge, "file_exists_use_overwrite"):
                sn.write_text("work/out.txt", "hello")

    def test_write_rejects_paths_outside_work_before_remote_execution(self):
        with patch.object(sn, "evaluate_op") as evaluate:
            with self.assertRaisesRegex(ValueError, "under_work_root"):
                sn.write_text("/data/users/alice/out.txt", "hello")
        evaluate.assert_not_called()

    def test_shell_template_is_disk_backed_and_bounded(self):
        self.assertIn("tempfile.TemporaryFile", sn._SHELL_TMPL)
        self.assertIn("MAX_BYTES", sn._SHELL_TMPL)
        self.assertIn("stdout_truncated", sn._SHELL_TMPL)
        source = Path(sn.RUNTIME_JS_PATH).read_text(encoding="utf-8")
        self.assertIn("streamLimitChars", source)
        self.assertIn("655360", source)

    def test_probe_outer_result_requires_healthy_jupyter_status(self):
        source = Path(sn.RUNTIME_JS_PATH).read_text(encoding="utf-8")
        self.assertIn("probeOk = pr.status_code === 200", source)
        self.assertIn("jupyter_probe_failed", source)

    def test_navigated_iframe_must_pass_live_probe_before_connect_succeeds(self):
        frames = [{"id": "frame-1", "url": "https://inner.dcs.cloud/notebook/st/abc/"}]
        url = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w"
        with patch.object(sn, "get_frames", side_effect=[(False, []), (True, frames)]), \
             patch.object(sn, "_iframe_alive", return_value=True) as alive, \
             patch.object(sn, "command") as command, patch.object(sn.time, "sleep"):
            self.assertEqual(sn.ensure_iframe(navigate=True, settle=6, workspace_url=url,
                                              force_navigation=True), "frame-1")
        self.assertEqual(alive.call_count, 1)
        command.assert_called_once_with("navigate", {"url": url, "newTab": True}, timeout=60)

    def test_closed_session_tab_is_treated_as_missing(self):
        with patch.object(sn, "cdp", side_effect=sn.Bridge(
                'command_failed(cdp): session "stereonote" tab was closed — navigate first')):
            self.assertEqual(sn.get_frames(), (False, []))

    def test_iframe_match_is_exact_origin_and_path(self):
        frames = [
            {"id": "evil", "url": "https://evil.example/?next=inner.dcs.cloud/notebook/st/x"},
            {"id": "wrong-scheme", "url": "http://inner.dcs.cloud/notebook/st/x"},
            {"id": "good", "url": "https://inner.dcs.cloud/notebook/st/x/"},
        ]
        self.assertEqual(sn._iframe_id(frames), "good")

    def test_connect_saves_url_only_after_live_connection_succeeds(self):
        url = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w"
        with patch.object(sn, "ensure_daemon", return_value={"version": "1", "extension_connected": True}), \
             patch.object(sn, "ensure_iframe", return_value="frame") as iframe, \
             patch.object(sn, "save_url") as save, patch.object(sn, "_print"):
            self.assertEqual(sn.main(["connect", "--url", url]), 0)
        iframe.assert_called_once_with(navigate=True, workspace_url=url, force_navigation=True)
        save.assert_called_once_with(url)

    def test_failed_connect_does_not_save_url(self):
        url = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w"
        with patch.object(sn, "ensure_daemon", return_value={"version": "1", "extension_connected": True}), \
             patch.object(sn, "ensure_iframe", side_effect=sn.Bridge("jupyter_iframe_not_found")), \
             patch.object(sn, "save_url") as save, patch.object(sn, "_print"):
            self.assertEqual(sn.main(["connect", "--url", url]), 1)
        save.assert_not_called()

    def test_unsafe_artifact_root_is_reported_not_silently_empty(self):
        self.assertIn("unsafe_artifact_root", sn._ARTIFACT_INVENTORY)

    def test_large_artifacts_skip_full_sha256(self):
        self.assertIn("MAX_HASH_BYTES", sn._ARTIFACT_INVENTORY)
        self.assertIn("skipped_large_file", sn._ARTIFACT_INVENTORY)
        self.assertIn(str(sn.MAX_ARTIFACT_HASH_BYTES), sn._ARTIFACT_INVENTORY)

    def test_large_write_and_python_payloads_are_rejected_before_remote_execution(self):
        too_large = "x" * (sn.MAX_WRITE_BYTES + 1)
        with patch.object(sn, "write_text") as write, patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["write", "work/x.txt", "--content", too_large]), 2)
        write.assert_not_called()
        output.assert_called_once_with({"ok": False, "error": "write_content_too_large"})
        with patch.object(sn, "evaluate_op") as evaluate:
            with self.assertRaisesRegex(ValueError, "python_code_too_large"):
                sn.run_python("x" * (sn.MAX_CODE_BYTES + 1))
        evaluate.assert_not_called()

    def test_runtime_bounds_python_errors(self):
        source = Path(sn.RUNTIME_JS_PATH).read_text(encoding="utf-8")
        self.assertIn("boundedError", source)
        self.assertIn("traceback: clipText", source)


@unittest.skipIf(os.name == "nt", "local launcher integration uses POSIX setsid/bash")
class LocalLauncherIntegrationTests(unittest.TestCase):
    def test_launcher_executes_from_requested_cwd_and_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "jobs"
            cwd = tmp_path / "project"
            cwd.mkdir()
            (cwd / "big.py").write_text(
                "import os, pathlib; pathlib.Path(os.environ['SN_ARTIFACT_DIR'], 'done.txt').write_text('ok')\n",
                encoding="utf-8")
            job_id = "job_550e8400-e29b-41d4-a716-446655440000"
            with patch.object(sn, "JOB_ROOT", str(root)):
                launcher = sn.build_job_launcher(job_id, "python3 big.py", cwd=str(cwd))
            proc = subprocess.run(["bash", "-lc", launcher], capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("__SN_SUBMITTED__=" + job_id, proc.stdout)
            status_path = root / job_id / "status.json"
            deadline = time.time() + 10
            status = {}
            while time.time() < deadline:
                if status_path.exists():
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    if status.get("state") == "finished":
                        break
                time.sleep(0.05)
            self.assertEqual(status.get("state"), "finished", status)
            self.assertEqual(status.get("exit_code"), 0, status)
            self.assertEqual((root / job_id / "artifacts" / "done.txt").read_text(), "ok")
            manifest = json.loads((root / job_id / "manifest.json").read_text())
            self.assertEqual(manifest["cwd"], str(cwd))


class ReleaseLayoutTests(unittest.TestCase):
    def test_versions_are_consistent_and_legacy_config_is_not_shipped(self):
        root = Path(sn.SKILL_DIR)
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("v" + sn.CONTROLLER_VERSION, skill)
        self.assertIn("## " + sn.CONTROLLER_VERSION, changelog)
        self.assertFalse((root / "config.json").exists())
        self.assertTrue((root / "config.example.json").exists())

    def test_skill_frontmatter_uses_only_official_fields_and_codex_is_explicit(self):
        root = Path(sn.SKILL_DIR)
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        openai = (root / "agents" / "openai.yaml").read_text(encoding="utf-8")
        frontmatter = skill.split("---", 2)[1]
        top_level = [line.split(":", 1)[0] for line in frontmatter.splitlines()
                     if line and not line.startswith((" ", "\t"))]
        self.assertEqual(top_level, ["name", "description"])
        self.assertIn("allow_implicit_invocation: false", openai)
        self.assertIn("$stereonote", openai)
        short = re.search(r'short_description: "([^"]+)"', openai).group(1)
        self.assertGreaterEqual(len(short), 25)
        self.assertLessEqual(len(short), 64)
        self.assertNotIn("mentions\n  StereoNote / DCS / dcs.cloud, a /data/work path", skill)

    def test_public_docs_use_current_codex_and_claude_user_skill_paths(self):
        readme = (Path(sn.SKILL_DIR) / "README.md").read_text(encoding="utf-8")
        self.assertIn("~/.claude/skills/stereonote/", readme)
        self.assertIn("~/.agents/skills/stereonote/", readme)
        self.assertNotIn("~/.codex/skills/stereonote/", readme)


class CliValidationTests(unittest.TestCase):
    def test_run_shell_failure_sets_nonzero_cli_exit(self):
        result = {"ok": False, "returncode": 7, "stdout": "", "stderr": "boom"}
        with patch.object(sn, "run_shell", return_value=result), \
             patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["run-shell", "--cmd", "exit 7"]), 1)
        output.assert_called_once_with(result)

    def test_structured_inspect_error_sets_nonzero_cli_exit(self):
        result = {"error": "inspect_failed", "path": "/data/work/x.h5ad"}
        with patch.object(sn, "inspect", return_value=result), \
             patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["inspect", "/data/work/x.h5ad"]), 1)
        output.assert_called_once_with(result)

    def test_doctor_failure_sets_nonzero_cli_exit(self):
        with patch.object(sn, "doctor", return_value={"ok": False, "daemon_binary_exists": False}), \
             patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["doctor"]), 1)
        output.assert_called_once_with({"ok": False, "daemon_binary_exists": False})

    def test_failed_finished_job_sets_nonzero_poll_cli_exit(self):
        result = {"job_id": "job_x", "status": {"state": "finished", "exit_code": 7},
                  "missing": {"status": False, "stdout": False, "stderr": False},
                  "read_errors": {}, "stdout_tail": "", "stderr_tail": ""}
        with patch.object(sn, "poll", return_value=result), patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["poll", "job_550e8400-e29b-41d4-a716-446655440000"]), 1)
        output.assert_called_once_with(result)

    def test_value_error_is_rendered_as_structured_json(self):
        with patch.object(sn, "poll", side_effect=ValueError("invalid_job_id")), \
             patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["poll", "bad"]), 2)
        output.assert_called_once_with({"ok": False, "error": "invalid_job_id"})


if __name__ == "__main__":
    unittest.main()
