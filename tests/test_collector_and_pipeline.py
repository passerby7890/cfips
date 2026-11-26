import unittest
from unittest.mock import MagicMock, patch

import main as app_main
import scripts.discover_xiu2_subnets as discover_tool
import src.collector as collector


class TestCollectorDualStack(unittest.TestCase):
    def test_sample_xiu2_extra_candidates_ipv4_and_ipv6(self):
        v4 = collector._sample_xiu2_extra_candidates(
            ["172.64.229.0/24", "104.18.38.0/24"],
            existing_ips={"172.64.229.2"},
            max_subnets=2,
            is_v6=False,
        )
        self.assertEqual(len(v4), 1)
        self.assertTrue(v4[0].startswith("104.18.38."))

        v6 = collector._sample_xiu2_extra_candidates(
            ["2606:4700:3033::/48", "2606:4700:3034::/48"],
            existing_ips={"2606:4700:3033::1"},
            max_subnets=2,
            is_v6=True,
        )
        self.assertEqual(len(v6), 1)
        self.assertTrue(v6[0].startswith("2606:4700:3034:"))

    def test_fetch_wetest_v6_ips_parses_carriers(self):
        html = """
        <table>
          <tr><td>移动优选</td><td>2606:4700:3033::ac43:1</td><td>0%</td><td>100MB/s</td><td>55ms</td><td>HKG</td></tr>
          <tr><td>电信优选</td><td>2606:4700:3034::1</td><td>0%</td><td>90MB/s</td><td>72ms</td><td>SIN</td></tr>
          <tr><td>联通优选</td><td>2606:4700:3035::2</td><td>0%</td><td>80MB/s</td><td>190ms</td><td>NRT</td></tr>
        </table>
        """
        resp = MagicMock()
        resp.status_code = 200
        resp.text = html
        with patch("src.collector.requests.get", return_value=resp), patch(
            "src.collector.record_real_probe_observation"
        ) as mock_rec:
            res = collector._fetch_wetest_v6_ips()

        self.assertEqual(len(res["cm"]), 1)
        self.assertEqual(len(res["ct"]), 1)
        self.assertEqual(len(res["cu"]), 1)
        self.assertEqual(res["cm"][0]["ip"], "2606:4700:3033::ac43:1")
        self.assertEqual(mock_rec.call_count, 3)


class TestPipelineHelpers(unittest.TestCase):
    def test_select_target_subnets_reserves_xiu2_exploration_slots(self):
        sorted_subs = [
            (f"172.64.{i}.0/24", [{"domain": "example.com"}]) for i in range(1, 27)
        ] + [
            (f"104.18.{i}.0/24", [{"domain": "xiu2_cf_cidr"}]) for i in range(50, 54)
        ]
        selected, remaining = app_main.select_target_subnets(
            sorted_subs, limit=24, xiu2_reserve=3
        )
        self.assertEqual(len(selected), 24)
        self.assertEqual(len(remaining), 6)
        xiu2_count = sum(
            1 for _, items in selected if all(it.get("domain") == "xiu2_cf_cidr" for it in items)
        )
        self.assertEqual(xiu2_count, 3)

    def test_cu_threshold_228_bandwidth_gate_and_ewma_ranking(self):
        candidates = [
            {
                "ip": "162.159.130.10",
                "country": "HK",
                "real_cn_ms": 225.0,
                "real_cn_loss": 0.0,
                "speed_mbps": 25.0,
                "unprobed": False,
                "hit_ratio": 90.0,
                "score": 90.0,
            },
            {
                "ip": "162.159.131.10",
                "country": "HK",
                "real_cn_ms": 180.0,
                "real_cn_loss": 0.0,
                "speed_mbps": 0.8,
                "unprobed": False,
                "hit_ratio": 90.0,
                "score": 90.0,
            },
        ]
        kept = app_main.filter_usable_nodes(candidates, "cu", min_speed_mbps=2.0)
        self.assertEqual([x["ip"] for x in kept], ["162.159.130.10"])

        # EWMA stability ranking: steady 68ms (ewma 66ms) beats volatile 62ms (ewma 110ms)
        n_steady = {
            "ip": "104.21.82.10",
            "country": "HK",
            "real_cn_ms": 68.0,
            "ewma_ms": 66.0,
            "real_cn_loss": 0.0,
            "hit_ratio": 80.0,
            "score": 90.0,
        }
        n_volatile = {
            "ip": "104.21.85.10",
            "country": "HK",
            "real_cn_ms": 62.0,
            "ewma_ms": 110.0,
            "real_cn_loss": 0.0,
            "hit_ratio": 80.0,
            "score": 90.0,
        }
        ranked = sorted([n_volatile, n_steady], key=app_main.node_rank_key)
        self.assertEqual(ranked[0]["ip"], "104.21.82.10")

        # Loss bucketing: 70.8ms with 10% single-ping jitter beats slower 79.4ms (ewma 34.9) 0% loss
        n_fast_10pct = {
            "ip": "172.66.141.10",
            "country": "HK",
            "real_cn_ms": 70.8,
            "ewma_ms": 48.4,
            "real_cn_loss": 10.0,
            "hit_ratio": 90.0,
            "score": 92.0,
        }
        n_slow_0pct = {
            "ip": "104.21.83.80",
            "country": "HK",
            "real_cn_ms": 79.4,
            "ewma_ms": 34.9,
            "real_cn_loss": 0.0,
            "hit_ratio": 90.0,
            "score": 92.0,
        }
        ranked_loss_tier = sorted([n_slow_0pct, n_fast_10pct], key=app_main.node_rank_key)
        self.assertEqual(ranked_loss_tier[0]["ip"], "172.66.141.10")

    def test_select_target_subnets_reserves_ct_jp_slots(self):
        sorted_subs = [
            (f"104.18.{i}.0/24", [{"domain": "example.com", "prefer_region": "SG"}])
            for i in range(34, 54)
        ] + [
            ("172.64.229.0/24", [{"domain": "seed_ct", "prefer_region": "JP"}]),
            ("104.18.90.0/24", [{"domain": "xiu2_cf_cidr", "prefer_region": ""}]),
            ("104.18.91.0/24", [{"domain": "xiu2_cf_cidr", "prefer_region": ""}]),
        ]
        selected, _remaining = app_main.select_target_subnets(
            sorted_subs, limit=18, xiu2_reserve=2, prefer_regions=("JP",), region_reserve=2
        )
        selected_subs = {sub for sub, _ in selected}
        self.assertIn("172.64.229.0/24", selected_subs)
        self.assertEqual(len(selected), 18)

    def test_discover_xiu2_subnets_ipv6_sampling(self):
        cands = discover_tool.sample_subnets_from_cidrs(
            ["2606:4700:3030::/44"], max_per_cidr=2, is_v6=True
        )
        self.assertEqual(len(cands), 2)
        self.assertTrue(all(":" in c["ip"] and "/48" in c["subnet"] for c in cands))


if __name__ == "__main__":
    unittest.main()
