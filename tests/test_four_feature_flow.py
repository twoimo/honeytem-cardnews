"""End-to-end local fixture, NOT evidence of real product rights or model quality."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest

from PIL import Image
from support import FixtureCase
from test_composition import specification
from honeytem.drafts import init_draft, review_draft, promote_draft
from honeytem.imageprep import prepare_image, verify_prepared_image
from honeytem.workflow import init_workflow, run_workflow, workflow_status
from honeytem.composition import compose_reel, verify_composition
from honeytem.schema import canonical, digest


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires actual local FFmpeg and FFprobe")
class FourFeatureFlowTests(FixtureCase):
    def test_prepared_source_draft_review_workflow_reel_and_audio(self):
        root = self.output_directory()
        large = root / "synthetic-original.png"
        Image.new("RGB", (1600, 1120), (225, 223, 216)).save(large)
        original_bytes = large.read_bytes()
        rights = {"schema_version": 1, "status": "granted", "operator": "TEST ONLY synthetic creator",
                  "source": "Locally generated solid-color test image, not a product",
                  "reference": "TEST ONLY own mathematical fixture", "purpose": "Offline integration test",
                  "program": None, "basis": "own_work", "allowed_operations": ["copy", "resize", "strip_metadata", "encode_png"]}
        prepared = prepare_image(large, root / "prepared", rights=rights, allow_transform=True)
        self.assertEqual(large.read_bytes(), original_bytes)
        self.assertEqual(prepared["size"], [800, 560])
        self.assertTrue(verify_prepared_image(root / "prepared", expected_manifest_sha256=prepared["manifest_sha256"])["verified"])
        # The existing affiliate fixture is a schema test, not a claim of actual
        # affiliation, live prices, account ownership, or third-party permissions.
        patch = self.affiliate()
        patch["products"] = patch["products"][:1]
        product_url = patch["products"][0]["original_url"]
        source = root / "public-test-source.json"
        source.write_text(json.dumps({"@context": "https://schema.org", "@type": "Product",
                                      "name": "TEST ONLY fixture", "offers": {"price": "12000", "priceCurrency": "KRW"}}))
        draft_dir = root / "draft"
        initial = init_draft(product_url, "toss_sharelink", draft_dir, source_path=source)
        self.assertGreater(initial["missing_count"], 0)
        self.assertFalse(initial["publish_ready"])
        shutil.copyfile(root / "prepared" / prepared["output_file"], draft_dir / "prepared-test.png")
        patch["products"][0]["asset"].update(path="prepared-test.png", source="TEST ONLY local generated fixture",
                                             license="TEST ONLY own fixture; no real product or platform rights")
        for key in ("regular", "bold"):
            patch["fonts"][key]["path"] = str((self.base_dir / self.original["fonts"][key]["path"]).resolve())
        reviewed = review_draft(draft_dir / "draft.json", patch, draft_dir / "reviewed.json",
                                reviewer="TEST ONLY", expected_draft_sha256=initial["draft_sha256"], reviewed=True)
        self.assertEqual(reviewed["missing_count"], 0)
        promoted = promote_draft(draft_dir / "reviewed.json", draft_dir / "manifest.json", reviewer="TEST ONLY",
                                expected_draft_sha256=reviewed["draft_sha256"], reviewed=True)
        self.assertFalse(promoted["publish_ready"])
        spec = {"schema_version": 1, "tasks": [], "budget": {"max_requests": 0, "max_output_tokens": 0, "max_request_bytes": 0},
                "reel": {"durations": "0.4", "fps": 10, "allow_affiliate_transcoding": True}}
        (root / "workflow-spec.json").write_bytes(canonical(spec))
        init_workflow(draft_dir / "manifest.json", root / "workflow", root / "workflow-spec.json")
        complete = run_workflow(root / "workflow")
        self.assertEqual(complete["status"], "completed")
        self.assertEqual(complete["budget"]["requests_used"], 0)
        self.assertEqual(run_workflow(root / "workflow")["build"]["package_sha256"], complete["build"]["package_sha256"])
        source_mp4 = (root / "workflow/reel/reel.mp4").read_bytes()
        compose_input = root / "composition-input"
        compose_input.mkdir()
        compose_spec = specification(compose_input, fps=10, total_frames=8)
        (compose_input / "spec.json").write_bytes(canonical(compose_spec))
        composed = compose_reel(root / "workflow/reel", compose_input / "spec.json", root / "composed")
        self.assertEqual((root / "workflow/reel/reel.mp4").read_bytes(), source_mp4)
        checked = verify_composition(root / "composed", expected_manifest_sha256=composed["manifest_sha256"])
        self.assertEqual(checked["integrity"], "passed")
        self.assertFalse(checked["publish_ready"])
        self.assertEqual(workflow_status(root / "workflow")["status"], "completed")
