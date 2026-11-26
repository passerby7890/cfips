import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.cn_real_tester as crt


class TestCnRealTester(unittest.TestCase):
    def setUp(self):
        self._orig_cache_path = crt.PROBE_CACHE_PATH
        self._tmp_dir = tempfile.TemporaryDirectory()
        crt.PROBE_CACHE_PATH = Path(self._tmp_dir.name) / "probe_cache.json"
        crt._DISK_CACHE_LOADED = True
        crt._DISK_CACHE = {}
        crt._SUBNET_PROBE_CACHE = {}
        crt._GLOBALPING_RATE_LIMITED = False

    def tearDown(self):
        crt.PROBE_CACHE_PATH = self._orig_cache_path
        crt._DISK_CACHE = {}
        crt._SUBNET_PROBE_CACHE = {}
        crt._GLOBALPING_RATE_LIMITED = False
        self._tmp_dir.cleanup()

    def test_get_subnet_24_delegates_dual_stack(self):
        self.assertEqual(crt.get_subnet_24("104.21.82.20"), "104.21.82.0/24")
        self.assertEqual(crt.get_subnet_24("2606:4700:3033:1234::1"), "2606:4700:3033::/48")

    def test_extract_colo_and_tcp_from_http_result(self):
        payload = {
            "results": [
                {
                    "result": {
                        "statusCode": 200,
                        "headers": {"cf-ray": "8a1b2c3d-HKG"},
                        "timings": {"tcp": 54.2, "total": 110.5},
                        "rawBody": "colo=HKG\n",
                    }
                }
            ]
        }
        colo, tcp_ms, tcp_ok = crt.extract_http_metrics(payload)
        self.assertEqual(colo, "HKG")
        self.assertEqual(tcp_ms, 54.2)
        self.assertTrue(tcp_ok)

    def test_icmp_ratelimit_calibrated_by_tcp_handshake(self):
        with patch.object(
            crt,
            "_probe_ping_multi_asn",
            return_value=(62.0, 20.0, False, ["Jinhua(AS56041:62ms/20%)"]),
        ), patch.object(
            crt,
            "_probe_http_colo_and_tcp",
            return_value=("HKG", 58.0, True),
        ) as mock_http:
            country, real_ms, real_loss = crt.probe_subnet_from_mainland_cn("104.21.82.20", "cm")
        self.assertEqual(mock_http.call_count, 1)
        self.assertEqual(country, "HK")
        self.assertEqual(real_ms, 62.0)
        self.assertEqual(real_loss, 5.0)

    def test_real_loss_not_calibrated_when_tcp_fails(self):
        with patch.object(
            crt,
            "_probe_ping_multi_asn",
            return_value=(62.0, 40.0, False, ["Guilin(AS9808:62ms/40%)"]),
        ), patch.object(
            crt,
            "_probe_http_colo_and_tcp",
            return_value=("", None, False),
        ):
            country, real_ms, real_loss = crt.probe_subnet_from_mainland_cn("104.21.82.99", "cm")
        self.assertEqual(real_ms, 62.0)
        self.assertEqual(real_loss, 40.0)

    def test_ipv6_ping_falls_back_to_country_cn_on_422(self):
        posts = []

        def fake_post(url, json=None, headers=None, timeout=None):
            posts.append(json)
            resp = MagicMock()
            if len(posts) <= 2:
                resp.status_code = 422
                return resp
            resp.status_code = 202
            resp.json.return_value = {"id": "m-v6"}
            return resp

        fake_get_resp = MagicMock()
        fake_get_resp.status_code = 200
        fake_get_resp.json.return_value = {
            "status": "finished",
            "results": [
                {
                    "probe": {"city": "Shanghai", "asn": 4134},
                    "result": {"stats": {"avg": 64.0, "loss": 0.0}},
                }
            ],
        }

        with patch("src.cn_real_tester.requests.post", side_effect=fake_post), patch(
            "src.cn_real_tester.requests.get", return_value=fake_get_resp
        ), patch("src.cn_real_tester.time.sleep", return_value=None):
            avg_ms, avg_loss, rate_limited, cities = crt._probe_ping_multi_asn(
                "2606:4700:3033::ac43:1", "ct"
            )

        self.assertFalse(rate_limited)
        self.assertEqual(avg_ms, 64.0)
        self.assertEqual(avg_loss, 0.0)
        self.assertEqual(posts[-1]["locations"], [{"country": "CN"}])
        self.assertIn("Shanghai", cities[0])

    def test_cm_ipv6_prefers_dualstack_asns(self):
        v6_asns = crt._select_carrier_asns("cm", "2606:4700:99e3::9f9b:7d93")
        self.assertEqual(v6_asns, [24445, 56040])
        v4_asns = crt._select_carrier_asns("cm", "172.66.132.1")
        self.assertEqual(v4_asns[0], 9808)
        self.assertIn(56040, v4_asns)
        self.assertNotIn(24445, v4_asns)

    def test_ping_timeout_triggers_fresh_cache_fallback_for_ct_jp(self):
        crt._DISK_CACHE["172.64.229.0/24|ct"] = {
            "country": "JP",
            "ms": 63.0,
            "loss": 0.0,
            "ts": 1000000.0 - 7200.0,  # 2h old (> 5400s fresh TTL, < 48h fallback)
            "source": "globalping",
            "colo": "NRT",
        }
        post_resp = MagicMock(status_code=202)
        post_resp.json.return_value = {"id": "m-timeout"}
        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {"status": "in-progress", "results": []}

        ticks = iter([1000000.0, 1000000.0, 1000001.0, 1000010.0])
        with patch("src.cn_real_tester.requests.post", return_value=post_resp), patch(
            "src.cn_real_tester.requests.get", return_value=get_resp
        ), patch("src.cn_real_tester.time.sleep", return_value=None), patch(
            "src.cn_real_tester.time.time", side_effect=lambda: next(ticks, 1000015.0)
        ):
            country, real_ms, real_loss = crt.probe_subnet_from_mainland_cn("172.64.229.1", "ct", timeout_sec=5.0)

        self.assertEqual(country, "JP")
        self.assertEqual(real_ms, 63.0)
        self.assertEqual(real_loss, 0.0)

    def test_cm_rejects_kunming_transpacific_outlier_alongside_apac(self):
        post_resp = MagicMock(status_code=202)
        post_resp.json.return_value = {"id": "m-kunming"}
        get_resp = MagicMock(status_code=200)
        get_resp.json.return_value = {
            "status": "finished",
            "results": [
                {"probe": {"city": "Zhengzhou", "asn": 56040}, "result": {"stats": {"avg": 100.0, "loss": 0.0}}},
                {"probe": {"city": "Kunming", "asn": 9808}, "result": {"stats": {"avg": 190.0, "loss": 0.0}}},
            ],
        }
        with patch("src.cn_real_tester.requests.post", return_value=post_resp), patch(
            "src.cn_real_tester.requests.get", return_value=get_resp
        ), patch("src.cn_real_tester.time.sleep", return_value=None):
            avg_ms, avg_loss, rate_limited, _cities = crt._probe_ping_multi_asn("172.66.132.1", "cm")

        self.assertFalse(rate_limited)
        self.assertEqual(avg_ms, 100.0)
        self.assertEqual(avg_loss, 0.0)


if __name__ == "__main__":
    unittest.main()
