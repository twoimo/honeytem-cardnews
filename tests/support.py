"""Shared local-only fixtures. No account, API, URL fetch, or credentials."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
FONT_DEFAULTS = {
    "regular": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "bold": "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
}


def generator():
    spec = importlib.util.spec_from_file_location("offline_generator", REPO / "examples/offline/generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fonts = {key: Path(os.environ.get("HONEYTEM_TEST_" + key.upper() + "_FONT", default))
                 for key, default in FONT_DEFAULTS.items()}
        missing = [str(value) for value in fonts.values() if not value.is_file()]
        if missing:
            raise unittest.SkipTest("Set HONEYTEM_TEST_REGULAR_FONT and HONEYTEM_TEST_BOLD_FONT to Korean fonts; missing " + ", ".join(missing))
        cls.temporary = tempfile.TemporaryDirectory(prefix="honeytem-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.manifest_path = generator().generate(cls.root / "fixture", fonts["regular"], fonts["bold"])
        cls.base_dir = cls.manifest_path.parent
        cls.original = json.loads(cls.manifest_path.read_text(encoding="utf-8"))

    def setUp(self):
        self.manifest = copy.deepcopy(self.original)

    def write_manifest(self, data=None, name="case.json"):
        path = self.base_dir / name
        path.write_text(json.dumps(self.manifest if data is None else data, ensure_ascii=False), encoding="utf-8")
        return path

    def output_directory(self):
        temporary = tempfile.TemporaryDirectory(prefix="honeytem-output-", dir=self.root)
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def affiliate(self):
        """Validated-shape test records, never fetched or presented as actual evidence."""
        manifest = copy.deepcopy(self.original)
        manifest.update(mode="affiliate", fixture=False)
        manifest["channel"].update(status="confirmed", evidence_url="https://channel.vendor.co.kr/registration/1",
                                   profile_links_confirmed=True)
        manifest["disclosures"] = [{
            "program": "toss_sharelink",
            "text": "[광고] 토스쇼핑 쉐어링크 활동으로, 링크 구매 시 수수료를 지급받습니다.",
            "reference_url": "https://sharelink-docs.toss.im/disclosure",
            "checked_at": "2026-10-21T12:00:00+09:00", "verified": True,
        }]
        for product in manifest["products"]:
            product.update(program="toss_sharelink", seller="TEST ONLY 판매자", option="TEST ONLY 옵션", quantity=1,
                           price={"amount": 12000, "currency": "KRW", "conditions": "테스트 금액, 쿠폰 없음",
                                  "shipping": "배송비 별도 3000원"},
                           original_url=f"https://shop.vendor.co.kr/product/{product['id']}?color=stone&size=1",
                           affiliate_url=f"https://share.vendor.co.kr/a/{product['id']}?token=a%2Bb%2Fc&x=1&x=2&empty=&Upper=%2f#keep",
                           evidence={"url": f"https://shop.vendor.co.kr/evidence/{product['id']}",
                                     "checked_at": "2026-10-21T12:00:00+09:00", "claims_verified": True,
                                     "link_ownership_confirmed": True})
            # Declared actual only inside validation unit tests, not a published sample.
            product["asset"]["kind"] = "actual"
        return manifest
