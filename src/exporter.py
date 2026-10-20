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

from .region_mapper import get_country_code, resolve_country_tag
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
    tiers: Optional[List[tuple[float, float, str]]] = None,
    anti_empty: bool = True,
) -> Dict[str, Any]:
    """四層自動降級、同 C 段分散限制 (最多 3 個)、前 50 名輸出"""
    if tiers is None:
        tiers = DEFAULT_TIERS

    selected_tier = "SNAPSHOT_LOCK"
    filtered_ips: List[Dict[str, Any]] = []

    # 1. 晚高峰階梯式篩選
    for min_spd, max_los, tier_name in tiers:
        matched = [
            item for item in candidate_pool
            if item.get("ws_handshake_101", False) is True
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

    # 若階梯篩選未能達到足量，但仍有合格回源 IP，回退採計所有 ws 101 合格者
    if not filtered_ips:
        matched = [
            item for item in candidate_pool
            if item.get("ws_handshake_101", False) is True
        ]
        if matched:
            selected_tier = "FALLBACK_101"
            filtered_ips = sorted(matched, key=lambda x: float(x.get("final_score", 0.0)), reverse=True)
            for it in filtered_ips:
                it["tier"] = selected_tier

    # 2. 同 /24 網段限制 (同 C 段最多收錄 max_per_subnet 個)
    final_selection: List[Dict[str, Any]] = []
    c_subnet_tracker: Dict[str, int] = {}
    for item in filtered_ips:
        ip = str(item.get("ip", "")).strip()
        c_subnet = ".".join(ip.split(".")[:3])
        if c_subnet_tracker.get(c_subnet, 0) < max_per_subnet:
            # 確保 country 標籤由 4 層優先級解析，不被海外 Runner 覆蓋
            runner_c = item.get("runner_colo", "")
            country_code = item.get("country_code") or resolve_country_tag(item, runner_trace_colo=runner_c)
            item["country_code"] = country_code

            final_selection.append(item)
            c_subnet_tracker[c_subnet] = c_subnet_tracker.get(c_subnet, 0) + 1
        if len(final_selection) >= max_count:
            break

    # 3. 檔案寫入與防空鎖定
    out_p = Path(output_txt_path)
    if final_selection:
        out_p.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for it in final_selection:
            c_tag = it.get("country_code") or get_country_code(it.get("colo"), it.get("loc"))
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
    decay_ratio = float(tuning.get("history_decay_ratio", 0.7))
    anti_empty = bool(config.get("exporter", {}).get("anti_empty_protection", True))
    history_file = config.get("exporter", {}).get("history_file", "data/ip_history.json")
    preferred_colos = config.get("preferred_colos", {})

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
        # 1. 取得具備該運營商標籤的候選 IP
        cand_for_carrier = [it for it in verified_items if c_key in it.get("carriers", [])]

        # 2. 若該標籤下的數量不足 max_count，以符合該運營商 preferred_colos 的高分節點補充
        pref_colos = preferred_colos.get(c_key, [])
        if len(cand_for_carrier) < max_count:
            for it in verified_items:
                if it not in cand_for_carrier and it.get("colo") in pref_colos:
                    cand_for_carrier.append(it)

        # 3. 若仍不足 max_count，以其餘通過 101 驗證的高分節點備援
        if len(cand_for_carrier) < max_count:
            for it in verified_items:
                if it not in cand_for_carrier and it.get("ws_handshake_101", False):
                    cand_for_carrier.append(it)

        # 4. 為候選池內每個節點計算針對該運營商的綜合評分與 EWMA 歷史平滑
        scored_candidates: List[Dict[str, Any]] = []
        for it in cand_for_carrier:
            it_copy = dict(it)
            instant = calculate_single_score(it_copy, c_key, preferred_colos)
            final_sc = apply_ewma_history(it_copy["ip"], instant, history_dict, decay=decay_ratio)
            it_copy["instant_score"] = instant
            it_copy["final_score"] = final_sc
            # 確保 country_code 優先採用原生標籤與境內 Colo
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
