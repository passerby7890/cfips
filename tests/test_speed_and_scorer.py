import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.scorer as scorer
import src.speed_tester as st


class _FakeTLSSocket:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = b""
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, bufsize):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class TestSpeedTester(unittest.TestCase):
    def test_measure_ip_bandwidth_tls_sni_success(self):
        fake_sock = _FakeTLSSocket(
            [
                b"HTTP/1.1 200 OK\r\nContent-Length: 131072\r\n\r\n" + (b"x" * 65536),
                b"y" * 65536,
                b"",
            ]
        )
        fake_ctx = MagicMock()
        fake_ctx.wrap_socket.return_value = fake_sock
        raw_conn = MagicMock()

        ticks = iter([100.0, 100.05, 100.10, 100.20])
        with patch("src.speed_tester.socket.create_connection", return_value=raw_conn) as mock_conn, patch(
            "src.speed_tester.ssl.create_default_context", return_value=fake_ctx
        ), patch("src.speed_tester.time.time", side_effect=lambda: next(ticks)):
            mbps = st.measure_ip_bandwidth("172.64.229.2", fallback_speed=0.0)

        mock_conn.assert_called_once_with(("172.64.229.2", 443), timeout=2.2)
        fake_ctx.wrap_socket.assert_called_once_with(raw_conn, server_hostname="speed.cloudflare.com")
        self.assertIn(b"GET /__down?bytes=2097152 HTTP/1.1", fake_sock.sent)
        self.assertIn(b"Host: speed.cloudflare.com", fake_sock.sent)
        self.assertGreater(mbps, 0.0)

    def test_measure_ip_bandwidth_rejects_non_200_error_1034(self):
        fake_sock = _FakeTLSSocket([b"HTTP/1.1 403 Forbidden\r\n\r\nerror code: 1034", b""])
        fake_ctx = MagicMock()
        fake_ctx.wrap_socket.return_value = fake_sock
        raw_conn = MagicMock()

        with patch("src.speed_tester.socket.create_connection", return_value=raw_conn), patch(
            "src.speed_tester.ssl.create_default_context", return_value=fake_ctx
        ):
            mbps = st.measure_ip_bandwidth("1.2.3.4", fallback_speed=0.0)

        self.assertEqual(mbps, 0.0)

    def test_measure_ip_bandwidth_rejects_connect_error(self):
        with patch("src.speed_tester.socket.create_connection", side_effect=OSError("timed out")):
            mbps = st.measure_ip_bandwidth("1.2.3.4", fallback_speed=0.0)
        self.assertEqual(mbps, 0.0)

    def test_batch_measure_bandwidth_defaults_to_speed_cloudflare_443(self):
        items = [{"ip": "172.64.229.2", "country": "HK"}]
        with patch.object(st, "measure_ip_bandwidth", return_value=42.5) as mock_m:
            res = st.batch_measure_bandwidth(items)
        mock_m.assert_called_once_with(
            ip="172.64.229.2",
            port=443,
            sni_host="speed.cloudflare.com",
            duration_limit=1.8,
            fallback_speed=0.0,
        )
        self.assertEqual(res[0]["speed_mbps"], 42.5)


class TestSubnetEwmaScorer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hist_path = Path(self._tmp.name) / "ip_history.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_history_skips_subnet_ewma_metadata(self):
        self.hist_path.write_text(
            json.dumps({"1.2.3.4": 55.2, "__subnet_ewma__": {"104.21.82.0/24|cm": {"ewma_ms": 60.5}}}),
            encoding="utf-8",
        )
        loaded = scorer.load_history(self.hist_path)
        self.assertEqual(loaded, {"1.2.3.4": 55.2})

    def test_subnet_ewma_update_and_persistence_preserves_ip_entries(self):
        self.hist_path.write_text(json.dumps({"1.2.3.4": 55.2}), encoding="utf-8")
        hist = scorer.load_subnet_history(self.hist_path)
        self.assertEqual(hist, {})

        v1 = scorer.update_subnet_ewma(hist, "104.21.82.0/24", "cm", 60.0)
        self.assertEqual(v1, 60.0)
        self.assertEqual(hist["104.21.82.0/24|cm"]["runs"], 1)

        v2 = scorer.update_subnet_ewma(hist, "104.21.82.0/24", "cm", 100.0, alpha=0.4)
        self.assertEqual(v2, 76.0)
        self.assertEqual(hist["104.21.82.0/24|cm"]["runs"], 2)

        scorer.save_subnet_history(hist, self.hist_path)
        raw = json.loads(self.hist_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["1.2.3.4"], 55.2)
        self.assertEqual(raw["__subnet_ewma__"]["104.21.82.0/24|cm"]["ewma_ms"], 76.0)
        self.assertEqual(raw["__subnet_ewma__"]["104.21.82.0/24|cm"]["runs"], 2)


if __name__ == "__main__":
    unittest.main()
