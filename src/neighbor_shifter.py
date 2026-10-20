"""階段二：同 /24 網段冷門鄰居平移演算法。

針對公開榜單上熱門但容易在高峰期被 QoS 限速或擁塞的單一 /32 IP，
在同一個 /24 網段（A.B.C.1 ~ A.B.C.254）內隨機抽取冷門鄰居 IP，
繼承原 IP 的運營商標籤並標記 `is_shifted_neighbor = True`。
"""

from __future__ import annotations

import ipaddress
import logging
import random
from typing import Any

from .collector import Candidate

log = logging.getLogger(__name__)


def shift_neighbors(
    candidates: dict[str, Candidate],
    cfg: dict[str, Any],
    rng: random.Random | None = None,
) -> dict[str, Candidate]:
    """為收集到的候選 IP 平移抽樣冷門鄰居。"""
    if not cfg.get("enabled", True):
        log.info("冷門鄰居平移已停用，保持原始候選池")
        return candidates

    if rng is None:
        rng = random.Random()

    samples_per_ip = int(cfg.get("samples_per_ip", 3))
    expanded: dict[str, Candidate] = dict(candidates)
    original_keys = list(candidates.keys())
    shifted_count = 0

    for ip in original_keys:
        cand = candidates[ip]
        cand.is_shifted_neighbor = False  # 確保原 IP 標記為公開熱門

        try:
            net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
            first = int(net.network_address) + 1
            last = int(net.broadcast_address) - 1
            orig_int = int(ipaddress.IPv4Address(ip))

            # 排除網路號、廣播號與原 IP
            possible = [i for i in range(first, last + 1) if i != orig_int and (i & 0xFF not in (0, 255))]
            if not possible:
                continue

            picked_ints = rng.sample(possible, min(samples_per_ip, len(possible)))
            for n_int in picked_ints:
                neighbor_ip = str(ipaddress.IPv4Address(n_int))
                if neighbor_ip not in expanded:
                    neighbor_cand = Candidate(
                        ip=neighbor_ip,
                        carriers=set(cand.carriers),
                        countries=set(cand.countries),
                        sources={f"neighbor:{ip}"},
                        is_shifted_neighbor=True,
                        loss_rate=cand.loss_rate,
                        download_speed=cand.download_speed,
                    )
                    expanded[neighbor_ip] = neighbor_cand
                    shifted_count += 1
                else:
                    # 若已存在，合併運營商屬性
                    expanded[neighbor_ip].carriers.update(cand.carriers)
        except Exception as exc:
            log.debug("IP %s 鄰居平移異常：%s", ip, exc)

    log.info(
        "同 /24 網段平移演算法執行完成：原始 %d 個 IP -> 擴展至 %d 個 IP（新增 %d 個冷門鄰居）",
        len(candidates), len(expanded), shifted_count
    )
    return expanded


def shift_to_clean_neighbors(base_records: list[dict[str, Any]], count_per_subnet: int = 3) -> list[dict[str, Any]]:
    """針對每個已獲取的公開 IP，抽取同 /24 網段的冷門鄰居 IP，避開熱點限速與萬人擠爆"""
    results: list[dict[str, Any]] = []
    seen_ips = set()

    for record in base_records:
        ip_str = record.get("ip", "").strip()
        try:
            ip_obj = ipaddress.IPv4Address(ip_str)
            network = ipaddress.IPv4Network(f"{ip_obj}/24", strict=False)

            # 保留原 IP（帶有境內情報源數據）
            if str(ip_obj) not in seen_ips:
                item = dict(record)
                item["is_shifted_neighbor"] = False
                results.append(item)
                seen_ips.add(str(ip_obj))

            # 抽取同 /24 冷門鄰居 (排除保留地址與原 IP)
            candidates = [
                str(network[i]) for i in range(2, 254)
                if str(network[i]) != str(ip_obj) and str(network[i]) not in seen_ips
            ]
            sampled = random.sample(candidates, min(count_per_subnet, len(candidates)))
            for neighbor in sampled:
                neighbor_item = dict(record)  # 繼承該網段的前半段實測指標
                neighbor_item["ip"] = neighbor
                neighbor_item["is_shifted_neighbor"] = True
                results.append(neighbor_item)
                seen_ips.add(neighbor)
        except Exception:
            continue

    return results

