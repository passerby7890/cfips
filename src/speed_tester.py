"""限時吞吐測速模組 (Speed Tester)。

- 10MB 數據切片，2.5 秒限時截斷，換算下行 Mbps。
- 配合境內實測情報源（WeTest、HostMonit 等）作為網段基礎分。
- 若源站 endpoint 未開放或回傳非 200，平滑回退至情報源基準頻寬，確保評分公正性。
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)


def measure_ip_bandwidth(
    ip: str,
    port: int = 2053,
    sni_host: str = "node1.snowcat77.com",
    duration_limit: float = 2.5,
    fallback_speed: float = 50.0,
) -> float:
    """10MB 數據切片，2.5 秒限時截斷，換算下行 Mbps。"""
    url = f"https://{ip}:{port}/__down?bytes=10485760"
    headers = {
        "Host": sni_host,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    total_bytes = 0
    start_time = time.time()

    try:
        with requests.get(url, headers=headers, stream=True, timeout=1.8, verify=False) as response:
            if response.status_code != 200:
                return float(fallback_speed) if fallback_speed > 0 else 0.0

            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    break
                total_bytes += len(chunk)
                if (time.time() - start_time) >= duration_limit:
                    break

        elapsed = time.time() - start_time
        if elapsed > 0 and total_bytes > 0:
            speed_mbps = (total_bytes * 8.0) / (elapsed * 1024.0 * 1024.0)
            return round(speed_mbps, 2)
    except Exception:
        pass

    return float(fallback_speed) if fallback_speed > 0 else 0.0


def batch_measure_bandwidth(
    items: List[Dict[str, Any]],
    port: int = 2053,
    sni_host: str = "node1.snowcat77.com",
    duration_limit: float = 2.5,
    max_workers: int = 20,
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[Dict[str, Any]]:
    """以線程池多工併發執行 10MB 限時切片測速。"""
    if not items:
        return []

    total = len(items)
    completed = 0
    log.info("開始執行限時吞吐測速快篩（共 %d 個回源合格 IP，併發: %d）", total, max_workers)

    def _test(item: Dict[str, Any]) -> Dict[str, Any]:
        ip = item.get("ip", "")
        fb = item.get("download_speed", 50.0)
        speed = measure_ip_bandwidth(
            ip=ip,
            port=port,
            sni_host=sni_host,
            duration_limit=duration_limit,
            fallback_speed=fb,
        )
        res = dict(item)
        res["speed_mbps"] = speed
        res["download_speed"] = speed
        return res

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_test, it): it for it in items}
        for future in concurrent.futures.as_completed(future_map):
            try:
                res = future.result()
                results.append(res)
            except Exception as exc:
                original = future_map[future]
                orig_copy = dict(original)
                orig_copy["speed_mbps"] = orig_copy.get("download_speed", 50.0)
                results.append(orig_copy)
                log.debug("測速異常 %s: %s", original.get("ip"), exc)

            completed += 1
            if progress and (completed % 10 == 0 or completed == total):
                progress(completed, total)

    log.info("限時吞吐測速完成，共測得 %d 個 IP", len(results))
    return results
