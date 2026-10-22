#!/usr/bin/env python3
"""
=============================================================================
V2Board Syncer - 大陸境內真機 TCP 防牆與防繞美漂移探測模組 (cn_probe.py v4.3.0)
=============================================================================
功能：
  1. 呼叫 Globalping REST API，按節點所屬三網 (CT:4134 / CU:4837 / CM:9808) 指定大陸境內實體探針。
  2. 雙重防護判定：
     - 防 GFW 封鎖：至少一顆境內探針回應成功 (loss < 100%)。
     - 防 Anycast 漂移繞美：境內探針實測最低/平均 RTT 必須 <= max_cn_rtt_ms (預設 115ms)！
       若實測高達 140ms~210ms，代表該 Anycast IP 當下已漂移至美國西岸，立即攔截淘汰！
"""

import json
import ssl
import time
import urllib.request
from typing import Tuple

CARRIER_ASN_MAP = {
    "ct": 4134,  # 中國電信
    "cu": 4837,  # 中國聯通
    "cm": 9808   # 中國移動
}


def verify_cn_mainland_tcp(
    ip: str,
    port: int = 443,
    carrier: str = "ct",
    max_cn_rtt_ms: float = 115.0,
    timeout_sec: float = 5.5
) -> Tuple[bool, str]:
    """
    透過 Globalping 大陸境內實體探針驗證該 IP:Port：
    1. 在中國境內是否未被 GFW 封鎖 (loss < 100%)
    2. 在中國境內是否未發生 Anycast 漂移繞美 (RTT <= max_cn_rtt_ms)
    """
    api_url = "https://api.globalping.io/v1/measurements"
    asn = CARRIER_ASN_MAP.get(str(carrier).lower().strip())

    locations = [{"country": "CN", "asn": asn}] if asn else [{"country": "CN"}]
    payload = {
        "type": "ping",
        "target": ip,
        "locations": locations,
        "limit": 2,
        "measurementOptions": {
            "protocol": "TCP",
            "port": int(port),
            "packets": 3
        }
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "cf-v2board-syncer/4.3"
    }

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers
        )
        with urllib.request.urlopen(req, timeout=3.5, context=ctx) as resp:
            if resp.status != 202:
                return True, f"API 回應 {resp.status} (Fail-open 略過)"
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            meas_id = data.get("id")

        if not meas_id:
            return True, "無 measurement ID (Fail-open 略過)"

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(1.2)
            req_get = urllib.request.Request(f"{api_url}/{meas_id}", headers=headers)
            with urllib.request.urlopen(req_get, timeout=3.0, context=ctx) as r2:
                if r2.status == 200:
                    d2 = json.loads(r2.read().decode("utf-8", errors="ignore"))
                    if d2.get("status") == "finished":
                        results = d2.get("results", [])
                        if not results:
                            return True, "無探針結果 (Fail-open 略過)"

                        probe_summaries = []
                        alive_rtts = []
                        for probe in results:
                            city = probe.get("probe", {}).get("city", "CN")
                            p_asn = probe.get("probe", {}).get("asn", "")
                            stats = probe.get("result", {}).get("stats", {})
                            loss = stats.get("loss", 100)
                            avg_rtt = stats.get("avg")
                            probe_summaries.append(f"{city}(AS{p_asn}):{avg_rtt}ms/丟包{loss}%")
                            if loss is not None and float(loss) < 100.0 and avg_rtt is not None and float(avg_rtt) > 0:
                                alive_rtts.append(float(avg_rtt))

                        summary_str = ", ".join(probe_summaries)
                        if not alive_rtts:
                            return False, f"境內 100% 丟包阻斷 (疑似 GFW 封鎖) [{summary_str}]"

                        best_cn_rtt = min(alive_rtts)
                        if best_cn_rtt > max_cn_rtt_ms:
                            return (
                                False,
                                f"Anycast 路由漂移繞美! 境內實測延遲過高 ({best_cn_rtt:.1f}ms > 上限 {max_cn_rtt_ms}ms) [{summary_str}]"
                            )

                        return True, f"境內亞太直連暢通 (最佳 {best_cn_rtt:.1f}ms <= {max_cn_rtt_ms}ms) [{summary_str}]"
    except Exception as e:
        return True, f"探針 API 調用略過: {e}"

    return True, "探針超時 (Fail-open 略過)"
