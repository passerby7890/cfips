#!/usr/bin/env python3
"""
=============================================================================
V2Board Syncer - 大陸境內真機 TCP 防牆探測模組 (cn_probe.py)
=============================================================================
功能：
  1. 呼叫 Globalping REST API (https://api.globalping.io/v1/measurements)。
  2. 指定中國大陸境內實體探針 (locations: [{"country": "CN"}])。
  3. 對候選 IP 發起真實 TCP 握手 (protocol: "TCP", port: 節點連接埠)。
  4. 只要有任何一顆境內探針回應成功 (loss < 100%)，即證明未被大陸 GFW 封鎖。
  5. 支援 Fail-open 原則：若 API 限流、網路超時或不可達，預設放行不阻塞主流程。
"""

import json
import ssl
import time
import urllib.request
from typing import Tuple


def verify_cn_mainland_tcp(ip: str, port: int = 443, timeout_sec: float = 5.0) -> Tuple[bool, str]:
    """
    透過 Globalping 大陸境內實體探針驗證該 IP:Port 在中國境內是否未被 GFW 封鎖。
    回傳: (is_reachable, detail_msg)
    若 API 配額滿、網路異常或超時，預設回傳 (True, "API 略過/Fail-open") 以免阻塞主流程。
    """
    api_url = "https://api.globalping.io/v1/measurements"
    payload = {
        "type": "ping",
        "target": ip,
        "locations": [{"country": "CN"}],
        "limit": 2,
        "measurementOptions": {
            "protocol": "TCP",
            "port": int(port),
            "packets": 2
        }
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "cf-v2board-syncer/3.2"
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
                        has_alive = False
                        for probe in results:
                            city = probe.get("probe", {}).get("city", "CN")
                            stats = probe.get("result", {}).get("stats", {})
                            loss = stats.get("loss", 100)
                            avg_rtt = stats.get("avg", 0)
                            probe_summaries.append(f"{city}:{avg_rtt}ms(丟包{loss}%)")
                            if loss is not None and float(loss) < 100.0:
                                has_alive = True
                        summary_str = ", ".join(probe_summaries)
                        if has_alive:
                            return True, f"境內探針暢通 [{summary_str}]"
                        else:
                            return False, f"境內 100% 丟包阻斷 (疑似 GFW 封鎖) [{summary_str}]"
    except Exception as e:
        return True, f"探針 API 調用略過: {e}"

    return True, "探針超時 (Fail-open 略過)"
