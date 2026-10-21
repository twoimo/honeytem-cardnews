"""CLI integration tests with in-process stubs, not model/encode evidence."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from support import FixtureCase
from honeytem.cli import main
from honeytem.schema import ValidationError


class ExtensionCLITests(FixtureCase):
    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def provider_stub(self, *, filename=None, failure=False):
        module = ModuleType("honeytem.providers")
        # Real error contract, transport itself remains a stub in this file.
        from honeytem.providers import ProviderError
        module.ProviderError = ProviderError
        module.load_provider_config = Mock(return_value=object())
        module.execute_provider = Mock()
        if failure:
            module.execute_provider.side_effect = ProviderError("transport_failed", outcome_unknown=True, trace={"status": "failed"})
        else:
            module.execute_provider.return_value = SimpleNamespace(
                adapter_result={"reviewed": False}, trace={"model_executed": "stub_only"},
                image_filename=filename, image_bytes=b"stub" if filename is not None else None)
        return module

    def arguments(self, output):
        material = self.base_dir / "cli-source-material.json"
        material.write_text("[]")
        return ["execute-provider", str(self.manifest_path), "01", "editorial_copy", "--config", "unused.json",
                "--source-material", str(material), "--output", str(output)]

    def test_provider_candidate_saved_unreviewed_and_network_default_false(self):
        destination = self.output_directory() / "attempt"
        module = self.provider_stub()
        with patch.dict("sys.modules", {"honeytem.providers": module}):
            rc, stdout, stderr = self.invoke(self.arguments(destination))
        self.assertEqual(rc, 0, stderr)
        self.assertFalse(json.loads(stdout)["publish_ready"])
        self.assertFalse(json.loads((destination / "candidate.json").read_text())["reviewed"])
        self.assertFalse(module.execute_provider.call_args.kwargs["allow_network"])
        self.assertFalse(module.execute_provider.call_args.kwargs["allow_remote"])

    def test_existing_attempt_never_calls_provider(self):
        destination = self.output_directory()
        module = self.provider_stub()
        with patch.dict("sys.modules", {"honeytem.providers": module}):
            rc, _, _ = self.invoke(self.arguments(destination))
        self.assertEqual(rc, 2)
        module.execute_provider.assert_not_called()

    def test_failed_request_leaves_attempt_and_does_not_retry(self):
        destination = self.output_directory() / "attempt"
        module = self.provider_stub(failure=True)
        with patch.dict("sys.modules", {"honeytem.providers": module}):
            rc, _, _ = self.invoke(self.arguments(destination))
            rc2, _, _ = self.invoke(self.arguments(destination))
        self.assertEqual((rc, rc2), (2, 2))
        self.assertEqual(module.execute_provider.call_count, 1)
        self.assertEqual(json.loads((destination / "attempt.json").read_text())["status"], "failed")
        self.assertTrue(json.loads((destination / "attempt.json").read_text())["outcome_unknown"])

    def test_provider_cannot_overwrite_reserved_or_escape_image_name(self):
        for filename in ("candidate.json", "attempt.json", "trace.json", "../outside.png", "x\\outside.png"):
            with self.subTest(filename=filename):
                destination = self.output_directory() / "attempt"
                module = self.provider_stub(filename=filename)
                with patch.dict("sys.modules", {"honeytem.providers": module}):
                    rc, _, _ = self.invoke(self.arguments(destination))
                self.assertEqual(rc, 2)
                self.assertFalse((destination / "candidate.json").exists())

    def test_reel_exact_decimal_strings_and_rights_flag_forwarded(self):
        module = ModuleType("honeytem.reels")
        module.export_reel = Mock(return_value={"publish_ready": False})
        with patch.dict("sys.modules", {"honeytem.reels": module}):
            rc, _, err = self.invoke(["reel", "source", "--output", "new", "--durations", "0.1234567890123456789", "--allow-affiliate-transcoding"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(module.export_reel.call_args.kwargs["durations"], "0.1234567890123456789")
        self.assertTrue(module.export_reel.call_args.kwargs["allow_affiliate_transcoding"])

    def test_verify_reel_rejects_wrong_trusted_manifest_hash(self):
        module = ModuleType("honeytem.reels")
        module.verify_reel = Mock(return_value={"manifest_sha256": "a" * 64})
        with patch.dict("sys.modules", {"honeytem.reels": module}):
            rc, _, _ = self.invoke(["verify-reel", "export", "--expected-manifest-sha256", "b" * 64])
        self.assertEqual(rc, 2)
