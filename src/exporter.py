"""三網優選輸出模組 (Exporter - 簡化三網前 50 版)。

- 輸出規範：
  1. 輸出至 output/ct.txt、output/cu.txt、output/cm.txt 三個檔案（取消 output/countries/）。
  2. 每個檔案輸出經綜合評分最高的前 50 個優選 IP。
  3. 每一行格式固定為：<IP>#<國家簡寫>（例如：172.67.140.251#TW 或 104.16.132.229#US）。
     國家簡寫由該 IP 的 colo 透過 region_mapper 取得。
  4. 同 /24 網段最多收錄 3 個 IP 以確保分散度與抗 QoS。
  5. 結構化總表輸出至 output/best_ips.json。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .region_mapper import get_country_code, init_subnet_country_map, resolve_country_tag
from .scorer import apply_ewma_history, calculate_single_score, load_history, save_history

log = logging.getLogger(__name__)

DEFAULT_TIERS = [
    (30.0, 5.0, "HEALTHY"),       # 4K 流暢標準 (speed >= 30, loss <= 5)
    (15.0, 8.0, "DEGRADED_L1"),    # 1080p 流暢標準 (speed >= 15, loss <= 8)
    (5.0, 12.0, "DEGRADED_L2"),    # 基礎通暢 (speed >= 5, loss <= 12)
    (0.0, 100.0, "EMERGENCY"),     # 極限兜底 (speed >= 0, loss <= 100)
]


def export_single_carrier(
    candidate_pool: List[Dict[str, Any]],
    carrier: str,
    output_txt_path: str | Path,
    max_count: int = 50,
    max_per_subnet: int = 3,
    max_per_country: int = 15,
    tiers: Optional[List[tuple[float, float, str]]] = None,
    anti_empty: bool = True,
) -> Dict[str, Any]:
    """四層自動降級、同 C 段分散限制 (最多 3 個)、國家多樣性保障、前 50 名輸出"""
    if tiers is None:
        tiers = DEFAULT_TIERS

    selected_tier = "SNAPSHOT_LOCK"
    filtered_ips: List[Dict[str, Any]] = []

    # 運營商專屬高延遲繞美黑名單
    if carrier == "ct":
        carrier_blacklist = ("104.16.", "104.17.", "104.18.", "104.19.", "104.2", "141.101.", "172.64.", "172.65.", "172.66.", "172.67.", "173.245.")
    elif carrier == "cu":
        carrier_blacklist = ("104.17.", "104.18.", "104.20.", "104.21.", "104.22.", "104.26.", "172.64.", "172.66.", "172.67.")
    else:
        carrier_blacklist = ("104.16.", "104.18.", "104.20.", "104.21.", "172.66.", "172.67.")

    def is_carrier_qualified(it: Dict[str, Any]) -> bool:
        ip_addr = str(it.get("ip", "")).strip()
        # 聯通特批保留 104.16.50 與 104.16.141 東京直連段
        if carrier == "cu" and (ip_addr.startswith("104.16.50.") or ip_addr.startswith("104.16.141.")):
            pass
        elif ip_addr.startswith(carrier_blacklist):
            # 若無境內實體探針驗證 < 90ms，嚴禁入選
            if float(it.get("cn_latency_ms") or 999.0) >= 90.0:
                return False
        # 電信嚴格要求：預估境內延遲必須小於 100ms
        est_lat = float(it.get("est_cn_latency_ms") or it.get("cn_latency_ms") or 150.0)
        if carrier == "ct" and est_lat >= 100.0:
            return False
        return True

    # 1. 晚高峰階梯式篩選
    for min_spd, max_los, tier_name in tiers:
        matched = [
            item for item in candidate_pool
            if item.get("ws_handshake_101", False) is True
            and is_carrier_qualified(item)
            and float(item.get("speed_mbps", 0.0)) >= min_spd
            and float(item.get("loss_percent", 100.0)) <= max_los
        ]
        if matched:
            selected_tier = tier_name
            # 依 final_score 由大到小排序
            filtered_ips = sorted(matched, key=lambda x: float(x.get("final_score", 0.0)), reverse=True)
            for it in filtered_ips:
                it["tier"] = selected_tier
            break

    # 若階梯篩選未能達到足量，但仍有合格回源 IP，回退採計所有合格回源 IP
    if not filtered_ips:
        matched = [
            item for item in candidate_pool
            if item.get("ws_handshake_101", False) is True
            and is_carrier_qualified(item)
        ]
        if matched:
            selected_tier = "FALLBACK_101"
            filtered_ips = sorted(matched, key=lambda x: float(x.get("final_score", 0.0)), reverse=True)
            for it in filtered_ips:
                it["tier"] = selected_tier

    # 2. 國家多樣性保障與同 /24 網段限制 (兩階段篩選)
    final_selection: List[Dict[str, Any]] = []
    selected_ips: set[str] = set()
    c_subnet_tracker: Dict[str, int] = {}
    country_tracker: Dict[str, int] = {}

    # 第一階段：嚴格限制單一國家上限 (防單一地區霸榜) 與同 C 段上限
    for item in filtered_ips:
        ip = str(item.get("ip", "")).strip()
        c_subnet = ".".join(ip.split(".")[:3])
        runner_c = item.get("runner_colo", "")
        c_tag = item.get("country_code") or resolve_country_tag(item, runner_trace_colo=runner_c)
        item["country_code"] = c_tag

        if c_subnet_tracker.get(c_subnet, 0) < max_per_subnet and country_tracker.get(c_tag, 0) < max_per_country:
            final_selection.append(item)
            selected_ips.add(ip)
            c_subnet_tracker[c_subnet] = c_subnet_tracker.get(c_subnet, 0) + 1
            country_tracker[c_tag] = country_tracker.get(c_tag, 0) + 1

        if len(final_selection) >= max_count:
            break

    # 第二階段：若第一階段因國家限額未補滿 max_count，放寬國家限制繼續補齊，同時依然維持同 C 段限制
    if len(final_selection) < max_count:
        for item in filtered_ips:
            ip = str(item.get("ip", "")).strip()
            if ip in selected_ips:
                continue
            c_subnet = ".".join(ip.split(".")[:3])
            runner_c = item.get("runner_colo", "")
            c_tag = item.get("country_code") or resolve_country_tag(item, runner_trace_colo=runner_c)
            item["country_code"] = c_tag

            if c_subnet_tracker.get(c_subnet, 0) < max_per_subnet:
                final_selection.append(item)
                selected_ips.add(ip)
                c_subnet_tracker[c_subnet] = c_subnet_tracker.get(c_subnet, 0) + 1
                country_tracker[c_tag] = country_tracker.get(c_tag, 0) + 1

            if len(final_selection) >= max_count:
                break

    # 3. 檔案寫入與防空鎖定
    out_p = Path(output_txt_path)
    if final_selection:
        out_p.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for it in final_selection:
            c_tag = it.get("country_code") or get_country_code(it.get("colo"), it.get("loc"))
            est_lat = float(it.get("est_cn_latency_ms") or it.get("cn_latency_ms") or 150.0)
            if est_lat >= 100.0 and c_tag in ("HK", "TW", "JP", "SG", "KR"):
                c_tag = "US"
            lines.append(f"{it['ip']}#{c_tag}")
        out_p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("[+] [%s] 成功輸出 %d 筆 IP (狀態: %s) -> %s", carrier.upper(), len(final_selection), selected_tier, out_p.name)
    else:
        if anti_empty and out_p.exists() and out_p.stat().st_size > 0:
            log.warning("[!] [%s] 警告: 當次無合格 IP，已啟動快照保護，保留原檔案！", carrier.upper())
        else:
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text("", encoding="utf-8")
            log.warning("[!] [%s] 警告: 當次無合格 IP 且無快照！", carrier.upper())

    return {
        "carrier": carrier,
        "status": selected_tier,
        "count": len(final_selection),
        "data": final_selection,
    }


def export_all(
    verified_items: List[Dict[str, Any]],
    config: Dict[str, Any],
    output_dir: Path | str,
) -> Dict[str, Any]:
    """輸出三網簡化檔案 (output/ct.txt, output/cu.txt, output/cm.txt) 與 best_ips.json。"""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 清理舊版已廢棄的 output/countries 與 output/carriers 目錄
    for legacy_dir in [out_dir / "countries", out_dir / "carriers"]:
        if legacy_dir.is_dir():
            try:
                shutil.rmtree(legacy_dir)
                log.info("已清理舊版輸出目錄：%s", legacy_dir)
            except Exception as e:
                log.debug("清理目錄失敗 %s: %s", legacy_dir, e)

    tuning = config.get("tuning", {})
    max_count = int(tuning.get("target_output_count", 50))
    max_per_subnet = int(tuning.get("max_ips_per_subnet", 3))
    max_per_country = int(tuning.get("max_ips_per_country", 15))
    decay_ratio = float(tuning.get("history_decay_ratio", 0.7))
    anti_empty = bool(config.get("exporter", {}).get("anti_empty_protection", True))
    history_file = config.get("exporter", {}).get("history_file", "data/ip_history.json")
    preferred_colos = config.get("preferred_colos", {})

    # 初始化網段靜態國家對照表
    init_subnet_country_map(config.get("exporter", {}).get("data_dir", "data"))

    # 載入歷史評分持久化資料
    history_dict = load_history(history_file)

    # 讀取 thresholds
    thresholds = config.get("thresholds", {})
    tiers = []
    if thresholds:
        for t_key in sorted(thresholds.keys()):
            t_cfg = thresholds[t_key]
            tiers.append((
                float(t_cfg.get("speed_mbps", 0.0)),
                float(t_cfg.get("loss_percent", 100.0)),
                str(t_cfg.get("name", t_key.upper())),
            ))
    else:
        tiers = DEFAULT_TIERS

    # 三網分類：ct (電信), cu (聯通), cm (移動)
    target_carriers = ["ct", "cu", "cm"]
    carrier_summary: Dict[str, Any] = {}
    carrier_results: Dict[str, List[Dict[str, Any]]] = {}

    for c_key in target_carriers:
        pref_colos = preferred_colos.get(c_key, [])
        scored_candidates: List[Dict[str, Any]] = []

        # 1. 第一優先：具備該運營商專屬標籤、或具備該運營商境內實測探針的節點
        carrier_specific = [
            it for it in verified_items 
            if c_key in it.get("carriers", [])
            or it.get("source_hint", {}).get("carrier") == c_key
        ]

        # 2. 第二優先：通用亞太優選 (all) 與符合該運營商直連機房 (preferred_colos) 的節點
        supplementary = [
            it for it in verified_items 
            if it not in carrier_specific
            and ("all" in it.get("carriers", []) or it.get("colo") in pref_colos)
        ]

        # 3. 第三優先備援：其餘通過 101 握手的合格節點
        fallback = [
            it for it in verified_items
            if it not in carrier_specific and it not in supplementary
        ]

        cand_pool = carrier_specific + supplementary + fallback

        # 4. 為候選池內每個節點計算針對該運營商的綜合評分與 EWMA 歷史平滑
        for it in cand_pool:
            it_copy = dict(it)
            instant = calculate_single_score(it_copy, c_key, preferred_colos)
            final_sc = apply_ewma_history(it_copy["ip"], instant, history_dict, decay=decay_ratio)
            it_copy["instant_score"] = instant
            it_copy["final_score"] = final_sc
            runner_c = it_copy.get("runner_colo", "")
            it_copy["country_code"] = it_copy.get("country_code") or resolve_country_tag(it_copy, runner_trace_colo=runner_c)
            scored_candidates.append(it_copy)

        txt_path = out_dir / f"{c_key}.txt"
        res = export_single_carrier(
            candidate_pool=scored_candidates,
            carrier=c_key,
            output_txt_path=txt_path,
            max_count=max_count,
            max_per_subnet=max_per_subnet,
            max_per_country=max_per_country,
            tiers=tiers,
            anti_empty=anti_empty,
        )
        carrier_summary[c_key] = res
        carrier_results[c_key] = res["data"]

    # 持久化更新後的歷史評分
    save_history(history_dict, history_file)

    # 輸出結構化 JSON 總表
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    total_ok = sum(1 for it in verified_items if it.get("ws_handshake_101") is True)
    summary_data = {
        "updated_at": now.isoformat(timespec="seconds"),
        "timestamp": int(now.timestamp()),
        "total_verified_ok": total_ok,
        "carriers_status": {k: v["status"] for k, v in carrier_summary.items()},
        "by_carrier": carrier_results,
    }

    json_path = out_dir / "best_ips.json"
    json_path.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("結構化總表已輸出 -> %s", json_path)

    return summary_data


def export_carrier_ips(
    candidate_pool: List[Dict[str, Any]],
    carrier: str,
    output_txt_path: str | Path,
    max_count: int = 50,
    max_per_subnet: int = 3,
    max_per_country: int = 15,
) -> Dict[str, Any]:
    """匯出特定運營商 Top IP 清單至 txt (格式: IP#COUNTRY)。"""
    return export_single_carrier(
        candidate_pool=candidate_pool,
        carrier=carrier,
        output_txt_path=output_txt_path,
        max_count=max_count,
        max_per_subnet=max_per_subnet,
        max_per_country=max_per_country,
    )

