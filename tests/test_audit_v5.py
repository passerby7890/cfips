import json
import tempfile
import unittest
from pathlib import Path

import scripts.self_test_audit as audit


class TestAuditV5Rules(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cu_speed_of_light_allows_228ms(self):
        # Verify updated CU HK threshold aligns with main.py (<= 230ms)
        self.assertGreaterEqual(audit.get_max_hk_ms("cu"), 228.0)
        self.assertEqual(audit.get_max_hk_ms("ct"), 130.0)
        self.assertEqual(audit.get_max_hk_ms("cm"), 130.0)

    def test_top_node_thresholds_support_multi_province_probes(self):
        self.assertGreaterEqual(audit.get_max_top_ms("cm"), 85.0)
        self.assertLessEqual(audit.get_max_top_ms("cm"), 105.0)
        self.assertGreaterEqual(audit.get_max_top_ms("ct"), 95.0)
        self.assertEqual(audit.get_max_top_ms("cu"), 230.0)

    def test_audit_ipv6_outputs_valid_and_catches_violations(self):
        good_v6 = (
            "2606:4700:3033::1#HK\n"
            "2606:4700:3033::2#HK\n"
            "2606:4700:3034::1#SG\n"
        )
        for c in ("ct", "cu", "cm"):
            (self.out_dir / f"{c}_v6.txt").write_text(good_v6, encoding="utf-8")

        errs = audit.audit_ipv6_outputs(self.out_dir)
        self.assertEqual(errs, [])

        # Inject >3 in same /48 plus banned #US tag
        bad_v6 = (
            "2606:4700:3033::1#HK\n"
            "2606:4700:3033::2#HK\n"
            "2606:4700:3033::3#HK\n"
            "2606:4700:3033::4#HK\n"
            "2606:4700:3035::1#US\n"
        )
        (self.out_dir / "cm_v6.txt").write_text(bad_v6, encoding="utf-8")
        errs_bad = audit.audit_ipv6_outputs(self.out_dir)
        self.assertTrue(any("/48" in e for e in errs_bad))
        self.assertTrue(any("#US" in e for e in errs_bad))

    def test_audit_ipv6_allows_missing_cu_only_and_rejects_missing_ct_cm(self):
        good_v6 = "2606:4700:3033::1#HK\n2606:4700:3034::1#SG\n"
        (self.out_dir / "ct_v6.txt").write_text(good_v6, encoding="utf-8")
        (self.out_dir / "cm_v6.txt").write_text(good_v6, encoding="utf-8")
        self.assertEqual(audit.audit_ipv6_outputs(self.out_dir), [])

        (self.out_dir / "ct_v6.txt").unlink()
        errs = audit.audit_ipv6_outputs(self.out_dir)
        self.assertTrue(any("ct_v6.txt" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
