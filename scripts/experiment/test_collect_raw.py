"""Focused tests for write-once RAW response capture helpers."""

from __future__ import annotations

from datetime import timezone
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from collect_raw import (
    forwarding_confirmed,
    parse_time,
    query_url,
    save_native,
    sha256,
    verify_bundle,
)


class _Handler(BaseHTTPRequestHandler):
    body = b'{"status":"success","data":{"result":[]}}\n'

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args: object) -> None:
        pass


class RawCollectorTest(unittest.TestCase):
    def test_time_is_normalized_to_utc(self) -> None:
        value = parse_time("2026-11-16T12:00:00-03:00")
        self.assertEqual(value.tzinfo, timezone.utc)
        self.assertEqual(value.isoformat(), "2026-11-16T15:00:00+00:00")

    def test_query_url_encodes_native_query_parameters(self) -> None:
        value = query_url("http://localhost:9090/", "/api/v1/query_range", {"query": '{__name__=~".+"}', "step": "1s"})
        self.assertIn("query=%7B__name__%3D~%22.%2B%22%7D", value)
        self.assertIn("step=1s", value)

    def test_port_forward_must_confirm_its_expected_local_and_remote_ports(self) -> None:
        self.assertTrue(forwarding_confirmed(
            "Forwarding from 127.0.0.1:45678 -> 16686\n", 45678, 16686
        ))
        self.assertTrue(forwarding_confirmed(
            "Forwarding from [::1]:45678 -> 16686\n", 45678, 16686
        ))
        self.assertFalse(forwarding_confirmed(
            "Forwarding from 127.0.0.1:45678 -> 16686\n", 45679, 16686
        ))
        self.assertFalse(forwarding_confirmed(
            "Forwarding from 127.0.0.1:45678 -> 9411\n", 45678, 16686
        ))

    def test_response_body_is_preserved_byte_for_byte_and_hashed(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                records: list[dict[str, object]] = []
                save_native(root, "datasetA/metrics.json", f"http://127.0.0.1:{server.server_port}/", "prometheus", records)
                output = root / "datasetA/metrics.json"
                self.assertEqual(output.read_bytes(), _Handler.body)
                self.assertEqual(output.stat().st_mode & 0o777, 0o444)
                self.assertEqual(records[0]["sha256"], sha256(_Handler.body))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_verify_bundle_detects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.json").write_text("{}\n", encoding="utf-8")
            (root / "manifest.json").write_text(json.dumps({"sample": True}) + "\n", encoding="utf-8")
            (root / "hashes.sha256").write_text(f"{sha256(b'wrong')}  sample.json\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(verify_bundle(root), 1)

    def test_verify_bundle_accepts_complete_read_only_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "raw"
            root.mkdir()
            sample = root / "sample.json"
            sample.write_text("{}\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"sample": True}) + "\n", encoding="utf-8")
            (root / "hashes.sha256").write_text(
                f"{sha256(sample.read_bytes())}  sample.json\n"
                f"{sha256(manifest.read_bytes())}  manifest.json\n",
                encoding="utf-8",
            )
            sample.chmod(0o444)
            manifest.chmod(0o444)
            (root / "hashes.sha256").chmod(0o444)
            root.chmod(0o555)
            with redirect_stdout(StringIO()):
                self.assertEqual(verify_bundle(root), 0)


if __name__ == "__main__":
    unittest.main()
