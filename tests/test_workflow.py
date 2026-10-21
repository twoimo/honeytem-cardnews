"""Durable orchestration tests. All HTTP endpoints are local deterministic fixtures."""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import shutil
import time
import subprocess
import sys
import threading
from unittest.mock import patch

from PIL import Image

from support import FixtureCase, REPO
from test_providers import ContractHandler, chat
from honeytem import workflow as wf
from honeytem.schema import ValidationError, canonical, digest, load_manifest


class WorkflowTests(FixtureCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ContractHandler)
        cls.server.daemon_threads = True
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        super().setUp()
        self.server.requests = []
        self.server.responses = []
        self.work = self.output_directory()
        self.directory = self.work / "workflow"
        self.config = {"protocol": "chat_completions", "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
                       "model": "local-contract-fixture-not-model", "max_output_tokens": 64}
        self.copy = {"title": "차분한 도형", "comment": "형태와 여백을 살펴봅니다.", "evidence_urls": []}

    def spec(self, selected=None, *, budget=None, reel=None, sources=None, config=None):
        selected = selected if selected is not None else [("01", "editorial_copy")]
        config = config or self.config
        (self.work / "provider.json").write_bytes(canonical(config))
        items = []
        for product_id, kind in selected:
            source = sources.get(product_id, []) if sources else []
            source_file = product_id + "-" + kind + ".json"
            (self.work / source_file).write_bytes(canonical(source))
            items.append({"product_id": product_id, "kind": kind, "config": "provider.json", "source_material": source_file})
        spec = {"schema_version": 1, "tasks": items, "budget": budget or {"max_requests": 10, "max_output_tokens": 640, "max_request_bytes": 2_621_440}, "reel": reel}
        path = self.work / "spec.json"
        path.write_bytes(canonical(spec))
        return path

    def init(self, spec=None, manifest=None):
        return wf.init_workflow(manifest or self.manifest_path, self.directory, spec)

    def respond(self, output=None, **spec):
        self.server.responses.append({"body": chat(output or self.copy), **spec})

    def item(self, status, task_id="01-editorial_copy"):
        return next(item for item in status["tasks"] if item["task_id"] == task_id)

    def accept(self, status, task_id="01-editorial_copy", **options):
        return wf.review_workflow(self.directory, task_id, self.item(status, task_id)["candidate_sha256"], "Fixture operator", **options)

    def test_offline_plan_init_build_resume_and_snapshot(self):
        plan = wf.plan_workflow(self.manifest_path)
        self.assertFalse(plan["network_executed"])
        self.assertFalse(self.directory.exists())
        self.assertTrue(all(task["mode"] == "retain" for task in plan["plan"]["tasks"]))
        initial = self.init()
        self.assertTrue(all(task["status"] == "retained" for task in initial["tasks"]))
        with patch("honeytem.providers._post", side_effect=AssertionError("offline must never POST")):
            completed = wf.run_workflow(self.directory)
            self.assertEqual(completed["status"], "completed")
            stamp = (self.directory / "state.json").stat().st_mtime_ns
            resumed = wf.run_workflow(self.directory)
        self.assertEqual(stamp, (self.directory / "state.json").stat().st_mtime_ns)
        self.assertEqual(resumed["build"]["package_sha256"], completed["build"]["package_sha256"])
        self.assertFalse(completed["publish_ready"])
        self.assertEqual(completed["budget"]["requests_used"], 0)
        self.assertEqual(self.server.requests, [])

    def test_actual_http_candidate_stays_unreviewed_until_digest_bound_import(self):
        self.init(self.spec())
        with self.assertRaisesRegex(ValidationError, "network opt-in"):
            wf.run_workflow(self.directory)
        self.assertEqual(self.server.requests, [])
        self.respond()
        status = wf.run_workflow(self.directory, allow_network=True)
        item = self.item(status)
        self.assertEqual(item["status"], "awaiting_review")
        candidate = json.loads(Path(item["candidate_path"]).read_text())
        self.assertIs(candidate["reviewed"], False)
        self.assertEqual(status["budget"]["requests_used"], 1)
        self.assertEqual(status["budget"]["output_tokens_reserved"], 64)
        self.assertEqual(status["budget"]["request_bytes_reserved"], 262144)
        wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(len(self.server.requests), 1)
        with self.assertRaisesRegex(ValidationError, "exact pending"):
            wf.review_workflow(self.directory, item["task_id"], "0" * 64, "Operator")
        accepted = self.accept(status)
        self.assertEqual(accepted["revision"], 1)
        self.assertNotEqual(status["fingerprint"], accepted["fingerprint"])
        self.assertEqual(load_manifest(self.directory / "input/manifest-0001.json").manifest["products"][0]["title"], self.copy["title"])
        completed = wf.run_workflow(self.directory)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(self.server.requests), 1)

    def test_multi_product_requests_use_sequential_imported_fingerprints(self):
        self.init(self.spec([("01", "editorial_copy"), ("02", "editorial_copy")]))
        self.respond()
        first = wf.run_workflow(self.directory, allow_network=True)
        accepted = self.accept(first)
        self.respond({**self.copy, "title": "고요한 도형"})
        second = wf.run_workflow(self.directory, allow_network=True)
        candidate = json.loads(Path(self.item(second, "02-editorial_copy")["candidate_path"]).read_text())
        self.assertEqual(candidate["input_fingerprint"], accepted["fingerprint"])
        self.assertNotEqual(candidate["input_fingerprint"], first["fingerprint"])
        self.accept(second, "02-editorial_copy")
        self.assertEqual(wf.run_workflow(self.directory)["status"], "completed")
        self.assertEqual(len(self.server.requests), 2)

    def test_research_human_claims_gate_then_copy_receives_corrected_facts(self):
        manifest = self.affiliate()
        manifest_path = self.write_manifest(manifest, "workflow-affiliate.json")
        sources = {"01": [{"url": manifest["products"][0]["evidence"]["url"], "excerpt": "Test fixture product facts; not live evidence."}]}
        self.init(self.spec([("01", "product_research"), ("01", "editorial_copy")], sources=sources), manifest_path)
        product = manifest["products"][0]
        proposed = {key: deepcopy(product[key]) for key in ("title", "comment", "seller", "option", "quantity", "price")}
        proposed["price"]["amount"] = 15000
        proposed["evidence_urls"] = [product["evidence"]["url"]]
        self.respond(proposed)
        status = wf.run_workflow(self.directory, allow_network=True)
        with self.assertRaisesRegex(ValidationError, "claims verification"):
            self.accept(status, "01-product_research")
        self.accept(status, "01-product_research", claims_verified=True)
        self.respond({**self.copy, "evidence_urls": proposed["evidence_urls"]})
        result = wf.run_workflow(self.directory, allow_network=True)
        body = self.server.requests[-1]["body"]["messages"][-1]["content"]
        self.assertEqual(json.loads(body)["product"]["price"]["amount"], 15000)
        self.accept(result)
        self.assertEqual(wf.run_workflow(self.directory)["status"], "completed")

    def test_image_bytes_and_operator_rights_are_required_for_import(self):
        manifest = deepcopy(self.manifest)
        manifest["fixture"] = False
        for product in manifest["products"]:
            product["asset"]["kind"] = "ai_concept"
        path = self.write_manifest(manifest, "workflow-concept.json")
        config = {**self.config, "protocol": "images_generations"}
        self.init(self.spec([("01", "authorized_image")], config=config, sources={"01": {"prompt": "An abstract stone shape, no actual product."}}), path)
        stream = BytesIO()
        Image.new("RGB", (100, 100), "white").save(stream, format="PNG")
        raw = stream.getvalue()
        self.server.responses.append({"body": {"data": [{"b64_json": base64.b64encode(raw).decode()}]}})
        status = wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(status["budget"]["output_tokens_reserved"], 0)
        with self.assertRaisesRegex(ValidationError, "explicit usage"):
            self.accept(status, "01-authorized_image")
        accepted = self.accept(status, "01-authorized_image", image_license="Fixture operator authorized use, not actual vendor terms.", image_usage_confirmed=True, image_layout_permission_confirmed=True)
        job = load_manifest(self.directory / "input/manifest-0001.json")
        self.assertEqual(job.assets["01"], raw)
        self.assertEqual(self.item(accepted, "01-authorized_image")["status"], "imported")

    def test_every_retry_is_reserved_durably_before_real_http(self):
        config = {**self.config, "max_attempts": 3}
        self.init(self.spec(config=config, budget={"max_requests": 2, "max_output_tokens": 128, "max_request_bytes": 524288}))
        self.server.responses.extend([{"status": 429, "headers": {"Retry-After": "0"}}, {"status": 429, "headers": {"Retry-After": "0"}}])
        import honeytem.providers as providers
        real_post = providers._post
        reservations = []
        def inspect_before_post(*args):
            state = json.loads((self.directory / "state.json").read_text())
            reservations.append(sum(event["type"] == "reserve" for event in state["events"]))
            return real_post(*args)
        with patch("honeytem.providers._post", side_effect=inspect_before_post):
            status = wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(reservations, [1, 2])
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.item(status)["error_code"], "budget_exhausted")
        self.assertEqual(status["budget"]["requests_used"], 2)
        wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(len(self.server.requests), 2)

    def test_byte_budget_also_prevents_post(self):
        self.init(self.spec(budget={"max_requests": 2, "max_output_tokens": 128, "max_request_bytes": 1}))
        status = wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(self.item(status)["error_code"], "budget_exhausted")
        self.assertEqual(status["budget"]["requests_used"], 0)
        self.assertEqual(self.server.requests, [])

    def test_timeout_is_unknown_and_requires_explicit_resolution_before_retry(self):
        self.init(self.spec(config={**self.config, "timeout_seconds": 0.05}))
        self.respond(delay=0.2)
        status = wf.run_workflow(self.directory, allow_network=True)
        item = self.item(status)
        self.assertEqual(item["status"], "uncertain")
        self.assertEqual(len(self.server.requests), 1)
        wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(len(self.server.requests), 1)
        with self.assertRaisesRegex(ValidationError, "exact stopped"):
            wf.resolve_workflow(self.directory, item["task_id"], "0" * 64, "Operator", "retry", "Inspected provider records")
        resolved = wf.resolve_workflow(self.directory, item["task_id"], item["attempt_sha256"], "Operator", "retry", "Inspected local fixture logs; accept duplicate risk for this test.")
        self.assertEqual(self.item(resolved)["status"], "pending")
        self.respond()
        retried = wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(self.item(retried)["attempt"], 2)
        self.assertEqual(retried["budget"]["requests_used"], 2)
        self.assertEqual(len(self.server.requests), 2)

    def test_crash_after_reservation_never_automatically_resubmits(self):
        self.init(self.spec())
        with patch("honeytem.providers._post", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                wf.run_workflow(self.directory, allow_network=True)
        status = wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(self.item(status)["status"], "uncertain")
        self.assertEqual(status["budget"]["requests_used"], 1)
        self.assertEqual(self.server.requests, [])
        item = self.item(status)
        wf.resolve_workflow(self.directory, item["task_id"], item["attempt_sha256"], "Operator", "abandon", "Retain reviewed baseline after interruption.")
        self.assertEqual(wf.run_workflow(self.directory)["status"], "completed")

    def test_successful_http_then_candidate_checkpoint_crash_stays_uncertain(self):
        self.init(self.spec())
        self.respond()
        original = wf._append
        def interrupt(root, state, event):
            if event["type"] == "candidate":
                raise KeyboardInterrupt
            return original(root, state, event)
        with patch.object(wf, "_append", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                wf.run_workflow(self.directory, allow_network=True)
        self.assertTrue((self.directory / "candidates/01-editorial_copy/1/candidate.json").is_file())
        status = wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(self.item(status)["status"], "uncertain")
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(wf.run_workflow(self.directory, allow_network=True)["revision"], 0)
        self.assertEqual(len(self.server.requests), 1)

    def test_review_write_crash_recovers_without_duplicate_import(self):
        self.init(self.spec())
        self.respond()
        status = wf.run_workflow(self.directory, allow_network=True)
        original = wf._append
        def interrupt(root, state, event):
            if event["type"] == "review":
                raise KeyboardInterrupt
            return original(root, state, event)
        with patch.object(wf, "_append", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.accept(status)
        accepted = self.accept(status)
        self.assertEqual(accepted["revision"], 1)
        self.assertEqual(len(self.server.requests), 1)
        with self.assertRaises(ValidationError):
            self.accept(status)

    def test_reject_retains_baseline_without_import(self):
        initial = self.init(self.spec())
        self.respond()
        status = wf.run_workflow(self.directory, allow_network=True)
        rejected = self.accept(status, decision="reject")
        self.assertEqual(rejected["fingerprint"], initial["fingerprint"])
        self.assertEqual(rejected["revision"], 0)
        self.assertEqual(wf.run_workflow(self.directory)["status"], "completed")

    def test_snapshot_candidate_and_import_tampering_fail_closed(self):
        self.init(self.spec())
        self.respond()
        status = wf.run_workflow(self.directory, allow_network=True)
        candidate_path = Path(self.item(status)["candidate_path"])
        original = candidate_path.read_bytes()
        candidate = json.loads(original)
        candidate["reviewed"] = True
        candidate_path.write_bytes(canonical(candidate))
        with self.assertRaisesRegex(ValidationError, "digest mismatch"):
            self.accept(status)
        candidate_path.write_bytes(original)
        self.accept(status)
        revision = self.directory / "input/manifest-0001.json"
        changed = json.loads(revision.read_text())
        changed["products"][0]["comment"] = "다른 문장입니다."
        revision.write_bytes(canonical(changed))
        with self.assertRaisesRegex(ValidationError, "imported manifest mismatch"):
            wf.workflow_status(self.directory)

    def test_modified_config_and_unsafe_checkpoint_paths_are_rejected(self):
        self.init(self.spec())
        config_path = self.directory / "providers/01-editorial_copy.json"
        original = config_path.read_bytes()
        config_path.write_bytes(canonical({**self.config, "model": "changed"}))
        with self.assertRaisesRegex(ValidationError, "digest mismatch"):
            wf.run_workflow(self.directory, allow_network=True)
        config_path.write_bytes(original)
        plan_path = self.directory / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["files"]["../../escape"] = "0" * 64
        raw = canonical(plan)
        plan_path.write_bytes(raw)
        state_path = self.directory / "state.json"
        state = json.loads(state_path.read_text())
        state["plan_sha256"] = digest(raw)
        state_path.write_bytes(canonical(state))
        with self.assertRaisesRegex(ValidationError, "unexpected snapshot path"):
            wf.workflow_status(self.directory)
        self.assertEqual(self.server.requests, [])

    def test_remote_opt_in_and_runtime_fingerprint_fail_closed(self):
        self.init(self.spec(config={**self.config, "base_url": "https://api.provider.com/v1"}))
        with patch("honeytem.providers._post", side_effect=AssertionError("must not POST")):
            with self.assertRaisesRegex(ValidationError, "remote opt-in"):
                wf.run_workflow(self.directory, allow_network=True)
        with patch("honeytem.schema.platform.python_version", return_value="unexpected-runtime"):
            with self.assertRaisesRegex(ValidationError, "stale input"):
                wf.workflow_status(self.directory)
        self.assertEqual(self.server.requests, [])

    def test_concurrent_process_fails_lock_and_process_death_releases_it(self):
        self.init()
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        command = [sys.executable, "-c", "from honeytem.workflow import _locked; import sys,time;\nwith _locked(sys.argv[1]):\n print('locked',flush=True); time.sleep(30)", str(self.directory)]
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(ValidationError, "busy"):
                wf.workflow_status(self.directory)
        finally:
            process.kill()
            process.communicate(timeout=5)
        self.assertEqual(wf.workflow_status(self.directory)["status"], "pending")

    def test_parser_registration_and_handler_are_root_compatible(self):
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        wf.register_cli(commands)
        args = parser.parse_args(["workflow-plan", str(self.manifest_path)])
        self.assertFalse(wf.handle_cli(args)["network_executed"])
        args = parser.parse_args(["workflow-init", str(self.manifest_path), "--output", str(self.directory)])
        self.assertEqual(wf.handle_cli(args)["status"], "pending")
        args = parser.parse_args(["workflow-run", str(self.directory)])
        self.assertEqual(wf.handle_cli(args)["status"], "completed")

    def test_legacy_active_and_stale_locks_are_never_deleted(self):
        self.init()
        build_root = self.directory / "build"
        build_root.mkdir()
        lock = build_root / ".honeytem.lock"
        for owner in (str(os.getpid()), "999999999", "unknown"):
            lock.write_text(owner)
            with self.assertRaisesRegex(ValidationError, "operator must inspect ownership"):
                wf.run_workflow(self.directory)
            self.assertEqual(lock.read_text(), owner)
        lock.unlink()  # Test operator has verified there is no actual build process.
        self.assertEqual(wf.run_workflow(self.directory)["status"], "completed")

    def test_reservation_write_failure_occurs_before_http(self):
        self.init(self.spec())
        original = wf._append
        def fail(root, state, event):
            if event["type"] == "reserve":
                raise OSError("injected durable write failure")
            return original(root, state, event)
        with patch.object(wf, "_append", side_effect=fail):
            with self.assertRaises(OSError):
                wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(self.server.requests, [])
        status = wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(self.item(status)["status"], "uncertain")
        self.assertEqual(status["budget"]["requests_used"], 0)

    def test_build_commit_interruption_resumes_identical_package(self):
        self.init()
        original = wf._append
        def interrupt(root, state, event):
            if event["type"] == "build":
                raise KeyboardInterrupt
            return original(root, state, event)
        with patch.object(wf, "_append", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                wf.run_workflow(self.directory)
        package = next((self.directory / "build").glob("*/package.zip"))
        original_hash = digest(package.read_bytes())
        stamp = package.stat().st_mtime_ns
        resumed = wf.run_workflow(self.directory)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["build"]["package_sha256"], original_hash)
        self.assertEqual(package.stat().st_mtime_ns, stamp)

    def test_malformed_journal_and_symlink_artifacts_fail_closed(self):
        self.init(self.spec())
        path = self.directory / "state.json"
        original = path.read_bytes()
        for events in ([{"type": "start", "task_id": []}], [{"type": "reserve", "task_id": "01-editorial_copy", "attempt": True, "tokens": 64, "bytes": 262144}], [None]):
            state = json.loads(original)
            state["events"] = events
            path.write_bytes(canonical(state))
            with self.assertRaises(ValidationError):
                wf.workflow_status(self.directory)
        path.write_bytes(original)
        source = self.directory / "sources/01-editorial_copy.json"
        source.unlink()
        source.symlink_to(self.work / "01-editorial_copy.json")
        with self.assertRaisesRegex(ValidationError, "symlink"):
            wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(self.server.requests, [])

    def test_crash_before_receipt_cannot_orphan_revision_or_poison_next_task(self):
        self.init(self.spec([("01", "editorial_copy"), ("02", "editorial_copy")]))
        self.respond()
        status = wf.run_workflow(self.directory, allow_network=True)
        original = wf._once
        def interrupt(path, raw):
            if path.parent.name == "reviews":
                raise KeyboardInterrupt
            return original(path, raw)
        with patch.object(wf, "_once", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.accept(status)
        self.assertFalse((self.directory / "input/manifest-0001.json").exists())
        self.accept(status, decision="reject")
        self.respond({**self.copy, "title": "고요한 도형"})
        second = wf.run_workflow(self.directory, allow_network=True)
        accepted = self.accept(second, "02-editorial_copy")
        self.assertEqual(accepted["revision"], 1)

    def test_receipt_before_manifest_binds_interrupted_decision(self):
        self.init(self.spec())
        self.respond()
        status = wf.run_workflow(self.directory, allow_network=True)
        original = wf._once
        def interrupt(path, raw):
            if path.name == "manifest-0001.json":
                self.assertTrue((self.directory / "reviews/01-editorial_copy-1.json").is_file())
                raise KeyboardInterrupt
            return original(path, raw)
        with patch.object(wf, "_once", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.accept(status)
        self.assertFalse((self.directory / "input/manifest-0001.json").exists())
        with self.assertRaisesRegex(ValidationError, "different decision"):
            self.accept(status, decision="reject")
        self.assertEqual(self.accept(status)["revision"], 1)

    def test_killed_process_after_actual_post_does_not_resubmit(self):
        self.init(self.spec())
        self.respond(delay=2)
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        process = subprocess.Popen([sys.executable, "-c", "from honeytem.workflow import run_workflow; import sys; run_workflow(sys.argv[1], allow_network=True)", str(self.directory)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while not self.server.requests and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.02)
            self.assertEqual(len(self.server.requests), 1)
        finally:
            process.kill()
            process.communicate(timeout=5)
        recovered = wf.run_workflow(self.directory, allow_network=True)
        self.assertEqual(self.item(recovered)["status"], "uncertain")
        self.assertEqual(recovered["budget"]["requests_used"], 1)
        self.assertEqual(len(self.server.requests), 1)

    def test_optional_real_offline_reel_is_verified_and_resumable(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("optional existing FFmpeg/FFprobe unavailable")
        self.init(self.spec([], reel={"durations": "0.04", "fps": 25}))
        with patch("honeytem.providers._post", side_effect=AssertionError("offline must never POST")):
            status = wf.run_workflow(self.directory)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["reel"]["integrity"], "passed")
            self.assertFalse(status["reel"]["publish_ready"])
            stamp = (self.directory / "reel/reel.mp4").stat().st_mtime_ns
            resumed = wf.run_workflow(self.directory)
        self.assertEqual(resumed["reel"]["video_sha256"], status["reel"]["video_sha256"])
        self.assertEqual(stamp, (self.directory / "reel/reel.mp4").stat().st_mtime_ns)
        self.assertEqual(self.server.requests, [])

    def test_direct_builder_process_legacy_lock_is_preserved(self):
        self.init()
        output = self.directory / "build"
        output.mkdir()
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        code = "from honeytem.pipeline import locked; from pathlib import Path; import sys,time;\nwith locked(Path(sys.argv[1])):\n print('locked',flush=True); time.sleep(30)"
        process = subprocess.Popen([sys.executable, "-c", code, str(output)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(ValidationError, "operator must inspect ownership"):
                wf.run_workflow(self.directory)
            self.assertEqual((output / ".honeytem.lock").read_text(), str(process.pid))
        finally:
            process.kill()
            process.communicate(timeout=5)
        with self.assertRaisesRegex(ValidationError, "operator must inspect ownership"):
            wf.run_workflow(self.directory)
        self.assertTrue((output / ".honeytem.lock").is_file())
