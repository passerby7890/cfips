import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import dns.message
import dns.rdatatype

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestRegionMapperDualStack(unittest.TestCase):
    def test_get_subnet_prefix_ipv4_and_ipv6(self):
        from src.region_mapper import get_subnet_prefix

        self.assertEqual(get_subnet_prefix("172.64.229.173"), "172.64.229.0/24")
        self.assertEqual(get_subnet_prefix("2606:4700:4406::ac40:92ea"), "2606:4700:4406::/48")
        self.assertEqual(get_subnet_prefix("2a06:98c1:310a:0000:0000:0000:ac40:9514"), "2a06:98c1:310a::/48")

    def test_load_xiu2_cf_v6_cidrs_and_anycast_check(self):
        from src.region_mapper import load_xiu2_cf_v6_cidrs, is_cf_official_anycast

        with tempfile.TemporaryDirectory() as tmpdir:
            v6_file = Path(tmpdir) / "cf_ranges_v6.txt"
            v6_file.write_text("# comment\n2606:4700::/32\n2a06:98c0::/29\n", encoding="utf-8")
            with patch("requests.get", side_effect=RuntimeError("offline")):
                cidrs = load_xiu2_cf_v6_cidrs(local_fallback_path=v6_file)

        self.assertIn("2606:4700::/32", cidrs)
        self.assertIn("2a06:98c0::/29", cidrs)
        self.assertTrue(is_cf_official_anycast("2606:4700:4406::ac40:92ea"))
        self.assertTrue(is_cf_official_anycast("2a06:98c1:310a::ac40:9514"))
        self.assertTrue(is_cf_official_anycast("172.64.229.1"))
        self.assertFalse(is_cf_official_anycast("2001:4860:4860::8888"))
        self.assertFalse(is_cf_official_anycast("8.8.8.8"))


class TestDnsExtractorAAAA(unittest.TestCase):
    def test_exhaustive_resolve_aaaa_records(self):
        from src.dns_extractor import exhaustive_resolve_domain_for_carrier

        q = dns.message.make_query("geaerospace.com", dns.rdatatype.AAAA)
        resp = dns.message.make_response(q)
        rrset = dns.rrset.from_text("geaerospace.com.", 300, "IN", "AAAA", "2606:4700:4402::ac40:914a", "fe80::1")
        resp.answer.append(rrset)
        wire = resp.to_wire()

        fake_post_resp = MagicMock(status_code=200, content=wire)
        fake_session = MagicMock()
        fake_session.post.return_value = fake_post_resp

        with patch("src.dns_extractor.discover_target_and_auth_ns", return_value=("geaerospace.com", ())), \
             patch("src.dns_extractor._get_doh_session", return_value=fake_session):
            results = exhaustive_resolve_domain_for_carrier(
                "geaerospace.com",
                [{"region": "GD", "ip": "183.47.126.35"}],
                rounds=1,
                rdtype="AAAA",
            )

        ips = [r["ip"] for r in results]
        self.assertIn("2606:4700:4402::ac40:914a", ips)
        self.assertNotIn("fe80::1", ips)


if __name__ == "__main__":
    unittest.main()
