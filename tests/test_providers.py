"""Loopback HTTP CONTRACT tests. Responses are fixtures, never model-quality proof."""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import socket
import threading
import time
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from support import FixtureCase
from honeytem.adapters import import_result
from honeytem.providers import (ProviderBudget, ProviderConfig, ProviderError,
                                _addresses, execute_provider, load_provider_config)
from honeytem.schema import ValidationError, canonical, validate_manifest


class ContractHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append({"path": self.path, "body": json.loads(body), "headers": dict(self.headers)})
        if not self.server.responses:
            self.send_error(500)
            return
        spec = self.server.responses.pop(0)
        if spec.get("delay"):
            time.sleep(spec["delay"])
        if spec.get("header_drip"):
            try:
                for byte in b"HTTP/1.1 200 OK\r\nX-Slow: continuing-forever":
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(spec["header_drip"])
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        data = spec.get("body", b"")
        if not isinstance(data, bytes):
            data = canonical(data)
        try:
            self.send_response(spec.get("status", 200))
            self.send_header("Content-Type", spec.get("type", "application/json"))
            self.send_header("Content-Length", str(spec.get("length", len(data))))
            for name, value in spec.get("headers", {}).items():
                self.send_header(name, value)
            self.end_headers()
            if spec.get("drip"):
                for byte in data:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(spec["drip"])
            else:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def chat(output, **updates):
    result = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(output, ensure_ascii=False)}}],
              "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}, "model": "fixture-contract-not-model"}
    result.update(updates)
    return result


class ProviderContractTests(FixtureCase):
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
        self.server.responses = []
        self.server.requests = []
        self.config = ProviderConfig("chat_completions", f"http://127.0.0.1:{self.server.server_port}/v1", "operator-chosen-test-model")
        self.job = validate_manifest(self.manifest, self.base_dir)
        self.copy = {"title": "차분한 도형", "comment": "형태와 여백을 살펴봅니다.", "evidence_urls": []}

    def execute(self, kind="editorial_copy", **kwargs):
        return execute_provider(kwargs.pop("job", self.job), "01", kind, kwargs.pop("config", self.config), allow_network=kwargs.pop("allow_network", True), **kwargs)

    def respond(self, output=None, **spec):
        self.server.responses.append({"body": chat(self.copy if output is None else output), **spec})

    def image_job(self):
        manifest = deepcopy(self.manifest)
        manifest["fixture"] = False
        for product in manifest["products"]:
            product["asset"]["kind"] = "ai_concept"
        return validate_manifest(manifest, self.base_dir)

    def image_response(self, size=(600, 420), mode="RGB"):
        stream = BytesIO()
        Image.new(mode, size, "white").save(stream, format="PNG")
        data = stream.getvalue()
        self.server.responses.append({"body": {"data": [{"b64_json": base64.b64encode(data).decode()}]}})
        return data

    def test_network_is_off_by_default_and_remote_requires_separate_opt_in(self):
        with self.assertRaisesRegex(ProviderError, "network_opt_in_required"):
            execute_provider(self.job, "01", "editorial_copy", self.config)
        with self.assertRaisesRegex(ProviderError, "remote_opt_in_required"):
            self.execute(config=replace(self.config, base_url="https://api.provider.com/v1"))
        self.assertEqual(self.server.requests, [])

    def test_local_copy_contract_and_unreviewed_import_barrier(self):
        self.respond()
        result = self.execute()
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["body"]["model"], self.config.model)
        self.assertFalse(request["body"]["stream"])
        self.assertEqual(request["body"]["max_tokens"], 2048)
        self.assertEqual(result.adapter_result["output"], self.copy)
        self.assertFalse(result.adapter_result["reviewed"])
        self.assertFalse(result.trace["source_truth_verified"])
        self.assertFalse(result.trace["model_identity_independently_verified"])
        self.assertEqual(result.trace["reported_usage"]["completion_tokens"], 30)
        with self.assertRaises(ValidationError):
            import_result(self.job, result.adapter_result, self.base_dir)
        self.assertNotIn(self.config.base_url, json.dumps(result.trace))
        self.assertNotIn(self.config.model, json.dumps(result.trace))

    def test_source_research_retains_links_and_revokes_claim_approval(self):
        job = validate_manifest(self.affiliate(), self.base_dir)
        original = deepcopy(job.manifest["products"][0])
        proposal = {k: original[k] for k in ("title", "comment", "seller", "option", "quantity", "price")}
        proposal["price"] = {**proposal["price"], "amount": 17000}
        proposal["evidence_urls"] = [original["evidence"]["url"]]
        self.respond(proposal)
        result = self.execute("product_research", job=job, source_material=[{"url": original["evidence"]["url"], "excerpt": "TEST ONLY source excerpt\n가격: 17000원"}])
        output = result.adapter_result["output"]
        self.assertEqual(output["price"]["amount"], 17000)
        self.assertEqual(output["affiliate_url"], original["affiliate_url"])
        self.assertEqual(output["original_url"], original["original_url"])
        self.assertEqual(output["asset"], original["asset"])
        self.assertFalse(output["evidence"]["claims_verified"])
        self.assertEqual(job.manifest["products"][0], original)
        sent = json.dumps(self.server.requests[0]["body"])
        self.assertNotIn(original["affiliate_url"], sent)
        self.assertNotIn("usage_confirmed", sent)
        reviewed_only = {**result.adapter_result, "reviewed": True}
        with self.assertRaises(ValidationError):
            import_result(job, reviewed_only, self.base_dir)

    def test_research_needs_supplied_source_and_matching_retained_evidence(self):
        job = validate_manifest(self.affiliate(), self.base_dir)
        for source in (None, [{"url": "https://shop.vendor.co.kr/other", "excerpt": "fixture"}]):
            with self.subTest(source=source), self.assertRaises(ProviderError):
                self.execute("product_research", job=job, source_material=source)
        with self.assertRaisesRegex(ProviderError, "research_requires_existing_affiliate_product"):
            self.execute("product_research")
        self.assertEqual(self.server.requests, [])

    def test_model_cannot_change_sources_or_return_authority(self):
        for proposal in ({**self.copy, "evidence_urls": ["https://vendor.co.kr/new"]}, {**self.copy, "reviewed": True}):
            self.respond(proposal)
            with self.assertRaises(ProviderError):
                self.execute()

    def test_nonfinite_duplicate_json_and_fenced_outputs_fail_closed(self):
        for content in ('{"title":"a","title":"b","comment":"c","evidence_urls":[]}', 'NaN', '```json\n{}\n```'):
            body = chat(self.copy)
            body["choices"][0]["message"]["content"] = content
            self.server.responses.append({"body": body})
            with self.assertRaises(ProviderError):
                self.execute()

    def test_refusal_tools_truncation_and_multiple_choices_rejected(self):
        variants = []
        for reason in ("length", "tool_calls", "content_filter"):
            body = chat(self.copy)
            body["choices"][0]["finish_reason"] = reason
            variants.append(body)
        body = chat(self.copy)
        body["choices"][0]["message"]["refusal"] = "fixture refusal"
        variants.append(body)
        body = chat(self.copy)
        body["choices"] *= 2
        variants.append(body)
        for body in variants:
            self.server.responses.append({"body": body})
            with self.assertRaises(ProviderError):
                self.execute()

    def test_request_and_response_size_limits(self):
        with self.assertRaisesRegex(ProviderError, "request_size_exceeded"):
            self.execute(config=replace(self.config, max_request_bytes=1024))
        self.assertEqual(self.server.requests, [])
        self.server.responses.append({"body": b" " * 2048})
        with self.assertRaisesRegex(ProviderError, "response_size_exceeded"):
            self.execute(config=replace(self.config, max_response_bytes=1024))

    def test_redirect_and_error_bodies_are_never_followed_or_exposed(self):
        for status in (302, 401, 500):
            marker = "private-test-error-body"
            self.server.responses.append({"status": status, "body": marker.encode(), "headers": {"Location": "http://127.0.0.1:1/secret"}})
            before = len(self.server.requests)
            with self.assertRaises(ProviderError) as caught:
                self.execute(config=replace(self.config, max_attempts=3))
            self.assertEqual(len(self.server.requests), before + 1)
            self.assertNotIn(marker, str(caught.exception))
            self.assertNotIn(marker, json.dumps(caught.exception.trace))
            self.assertEqual(caught.exception.outcome_unknown, status >= 500)

    def test_429_retry_is_explicit_and_consumes_budget(self):
        self.server.responses += [{"status": 429, "headers": {"Retry-After": "0"}}, {"body": chat(self.copy)}]
        budget = ProviderBudget(max_requests=2, max_output_tokens=4096)
        result = self.execute(config=replace(self.config, max_attempts=2), budget=budget)
        self.assertEqual(len(result.trace["attempts"]), 2)
        self.assertEqual(budget.requests_used, 2)
        self.assertEqual(budget.output_tokens_reserved, 4096)
        with self.assertRaisesRegex(ProviderError, "budget_exhausted"):
            self.execute(budget=budget)
        self.assertEqual(len(self.server.requests), 2)

    def test_default_does_not_retry_429_and_small_budget_sends_nothing(self):
        self.server.responses.append({"status": 429})
        with self.assertRaisesRegex(ProviderError, "rate_limited"):
            self.execute()
        with self.assertRaisesRegex(ProviderError, "budget_exhausted"):
            self.execute(budget=ProviderBudget(max_requests=1, max_output_tokens=100))
        self.assertEqual(len(self.server.requests), 1)

    def test_timeout_is_uncertain_and_not_retried(self):
        self.respond(delay=0.2)
        started = time.monotonic()
        with self.assertRaises(ProviderError) as caught:
            self.execute(config=replace(self.config, timeout_seconds=0.05, max_attempts=3))
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(self.server.requests), 1)

    def test_slow_drip_body_obeys_transaction_deadline(self):
        self.server.responses.append({"body": canonical(chat(self.copy)), "drip": 0.02})
        started = time.monotonic()
        with self.assertRaises(ProviderError):
            self.execute(config=replace(self.config, timeout_seconds=0.07))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_credentials_only_runtime_and_secret_echo_rejected(self):
        secret = 'public-test-secret-"-not-real'
        config = replace(self.config, api_key_env="HONEYTEM_PUBLIC_TEST_SECRET")
        with patch.dict("os.environ", {"HONEYTEM_PUBLIC_TEST_SECRET": secret}):
            self.respond({**self.copy, "comment": "Echo " + secret})
            with self.assertRaisesRegex(ProviderError, "credential_echo_rejected") as caught:
                self.execute(config=config)
            self.assertEqual(self.server.requests[0]["headers"]["Authorization"], "Bearer " + secret)
            self.assertNotIn(secret, json.dumps(caught.exception.trace))
            self.assertNotIn(secret, str(caught.exception))
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(ProviderError, "missing_or_invalid_runtime_credential"):
            self.execute(config=config)

    def test_configuration_rejects_inline_keys_and_unsafe_targets(self):
        path = self.output_directory() / "provider.json"
        data = {"protocol": "chat_completions", "base_url": self.config.base_url, "model": "fixture"}
        path.write_bytes(canonical(data))
        self.assertEqual(load_provider_config(path).model, "fixture")
        for extra in ({"api_key": "not-real"}, {"headers": {}}, {"max_attempts": True}, {"timeout_seconds": float("inf")}):
            path.write_text(json.dumps({**data, **extra}))
            with self.assertRaises(ProviderError):
                load_provider_config(path)
        for endpoint in ("http://192.168.0.1/v1", "http://169.254.169.254/v1", "https://10.0.0.1/v1", "http://api.provider.com/v1", "https://user:pass@api.provider.com/v1", "https://api.provider.com/v1?key=secret", "https://api.provider.com/v1#x", "https://api.provider.com/../v1", "file:///tmp/v1", "http://127.0.0.1:0/v1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ProviderError):
                replace(self.config, base_url=endpoint)

    def test_dns_rejects_mixed_private_results_and_pins_public_result(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        with patch("honeytem.providers.socket.getaddrinfo", return_value=[public, private]):
            with self.assertRaisesRegex(ProviderError, "unsafe_resolved_endpoint"):
                _addresses("api.provider.com", 443, False)
        with patch("honeytem.providers.socket.getaddrinfo", return_value=[public]) as resolver:
            self.assertEqual(_addresses("api.provider.com", 443, False), [public])
            self.assertEqual(resolver.call_count, 1)

    def test_usage_bool_or_limit_overrun_rejected(self):
        for usage in ({"completion_tokens": True}, {"completion_tokens": 2049}, {"total_tokens": -1}):
            self.server.responses.append({"body": chat(self.copy, usage=usage)})
            with self.assertRaises(ProviderError):
                self.execute()

    def test_native_concept_image_is_preserved_and_not_rights_approved(self):
        data = self.image_response()
        job = self.image_job()
        result = self.execute("authorized_image", job=job, config=replace(self.config, protocol="images_generations"), source_material={"prompt": "Public fixture test prompt"})
        self.assertEqual(self.server.requests[0]["path"], "/v1/images/generations")
        self.assertEqual(result.image_bytes, data)
        self.assertEqual(result.adapter_result["output"]["path"], result.image_filename)
        self.assertFalse(result.adapter_result["output"]["usage_confirmed"])
        self.assertFalse(result.adapter_result["output"]["layout_permission_confirmed"])
        self.assertTrue(result.trace["image"]["existing_asset_contract_compatible"])
        self.assertFalse(result.adapter_result["reviewed"])

    def test_large_image_is_not_resized_or_misrepresented_as_compatible(self):
        data = self.image_response((1024, 1024))
        result = self.execute("authorized_image", job=self.image_job(), config=replace(self.config, protocol="images_generations"), source_material={"prompt": "Public fixture test prompt"})
        self.assertEqual(result.image_bytes, data)
        self.assertFalse(result.trace["image"]["existing_asset_contract_compatible"])
        self.assertEqual(result.trace["image"]["width"], 1024)

    def test_affiliate_and_fixture_image_generation_blocked_before_network(self):
        config = replace(self.config, protocol="images_generations")
        for job in (self.job, validate_manifest(self.affiliate(), self.base_dir)):
            with self.assertRaisesRegex(ProviderError, "image_generation_requires_nonfixture_concept"):
                self.execute("authorized_image", job=job, config=config, source_material={"prompt": "fixture"})
        self.assertEqual(self.server.requests, [])

    def test_image_response_urls_invalid_base64_and_nonimages_rejected(self):
        for item in ({"url": "http://169.254.169.254/metadata"}, {"b64_json": "%%%"}, {"b64_json": base64.b64encode(b"not an image").decode()}):
            self.server.responses.append({"body": {"data": [item]}})
            before = len(self.server.requests)
            with self.assertRaises(ProviderError):
                self.execute("authorized_image", job=self.image_job(), config=replace(self.config, protocol="images_generations"), source_material={"prompt": "fixture"})
            self.assertEqual(len(self.server.requests), before + 1)

    def test_wrong_content_type_compression_and_truncation_rejected(self):
        for spec in ({"type": "text/html", "body": b"<html>no</html>"}, {"body": chat(self.copy), "headers": {"Content-Encoding": "gzip"}}, {"body": b"{}", "length": 10}):
            self.server.responses.append(spec)
            with self.assertRaises(ProviderError):
                self.execute()

    def test_slow_drip_headers_obey_transaction_deadline(self):
        self.server.responses.append({"header_drip": 0.02})
        started = time.monotonic()
        with self.assertRaises(ProviderError) as caught:
            self.execute(config=replace(self.config, timeout_seconds=0.07))
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_nested_unicode_secret_reflection_is_rejected(self):
        secret = "public-test-only-marker"
        config = replace(self.config, api_key_env="HONEYTEM_PUBLIC_TEST_SECRET")
        encoded = "".join("\\u%04x" % ord(c) for c in secret)
        response = chat(self.copy)
        response["choices"][0]["message"]["content"] = '{"title":"' + encoded + '","comment":"fixture","evidence_urls":[]}'
        self.server.responses.append({"body": response})
        with patch.dict("os.environ", {"HONEYTEM_PUBLIC_TEST_SECRET": secret}), self.assertRaisesRegex(ProviderError, "credential_echo_rejected"):
            self.execute(config=config)

    def test_encoded_image_metadata_secret_reflection_is_rejected(self):
        secret = "public-test-only-marker"
        config = replace(self.config, protocol="images_generations", api_key_env="HONEYTEM_PUBLIC_TEST_SECRET")
        for compressed in (False, True):
            stream = BytesIO()
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("Comment", secret, zip=compressed)
            Image.new("RGB", (20, 20), "white").save(stream, format="PNG", pnginfo=metadata)
            self.server.responses.append({"body": {"data": [{"b64_json": base64.b64encode(stream.getvalue()).decode()}]}})
            with patch.dict("os.environ", {"HONEYTEM_PUBLIC_TEST_SECRET": secret}), self.assertRaisesRegex(ProviderError, "credential_echo_rejected"):
                self.execute("authorized_image", job=self.image_job(), config=config, source_material={"prompt": "fixture"})

    def test_multicast_reserved_and_site_local_targets_are_not_public_unicast(self):
        for address in ("224.0.0.1", "239.255.255.250", "ff02::1", "fec0::1", "::", "::ffff:224.0.0.1"):
            host = "[" + address + "]" if ":" in address else address
            with self.subTest(address=address), self.assertRaises(ProviderError):
                replace(self.config, base_url="https://" + host + "/v1")
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
            resolved = [(family, socket.SOCK_STREAM, 6, "", sockaddr)]
            with patch("honeytem.providers.socket.getaddrinfo", return_value=resolved):
                with self.assertRaisesRegex(ProviderError, "unsafe_resolved_endpoint"):
                    _addresses("api.provider.com", 443, False)
