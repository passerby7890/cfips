"""階段四：雙維度加權排序、跨網段容災與檔案輸出。

- 依四網偏好 Colo (-30ms) 與冷門鄰居 (-15ms) 計算綜合評分 (score)。
- 同 /24 網段最多挑選 2 個 IP（跨 C 段容災與抗 QoS）。
- 防空覆寫保護：當次合格數為 0 時，保留前次有效檔案。
- 輸出 output/carriers/*.txt、output/countries/*.txt 與 output/best_ips.json。
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .region_mapper import get_supported_countries
from .scorer import ScoredItem, score_for_carrier, score_for_country
from .verifier import VerifyResult

log = logging.getLogger(__name__)


def filter_subnet_diversity(
    items: list[ScoredItem],
    max_per_subnet: int = 2,
    top_n: int = 15,
) -> list[ScoredItem]:
    """跨 /24 網段分散保護：同一個 C 段最多保留 max_per_subnet 個 IP，取前 top_n。"""
    items_sorted = sorted(items, key=lambda x: x.score)
    subnet_counts: dict[str, int] = {}
    selected: list[ScoredItem] = []

    for it in items_sorted:
        try:
            net = str(ipaddress.IPv4Network(f"{it.ip}/24", strict=False))
        except ValueError:
            net = "unknown"

        if subnet_counts.get(net, 0) < max_per_subnet:
            selected.append(it)
            subnet_counts[net] = subnet_counts.get(net, 0) + 1
            if len(selected) >= top_n:
                break

    return selected


def export_all(
    results: dict[str, VerifyResult],
    config: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """雙維度評分、篩選並輸出四網與分國文字檔及 best_ips.json。"""
    exp_cfg = config.get("exporter", {})
    carrier_prefs = config.get("carrier_preferences", {})
    neighbor_bonus = float(config.get("neighbor_shifter", {}).get("bonus_score_ms", 15.0))
    top_n = int(exp_cfg.get("top_n", 15))
    max_per_subnet = int(exp_cfg.get("max_per_subnet", 2))
    anti_empty = bool(exp_cfg.get("anti_empty_protection", True))

    carriers_dir = output_dir / "carriers"
    countries_dir = output_dir / "countries"
    carriers_dir.mkdir(parents=True, exist_ok=True)
    countries_dir.mkdir(parents=True, exist_ok=True)

    passed_results = [r for r in results.values() if r.ok]
    log.info("開始處理合格回源 IP 匯出作業（共 %d 個 IP 通過 101 驗證）", len(passed_results))

    # 1. 四網分類與評分
    carrier_keys = ["ct", "cu", "cm", "edu"]
    by_carrier: dict[str, list[ScoredItem]] = {}
    for c_key in carrier_keys:
        candidates_for_carrier = [r for r in passed_results if c_key in r.carriers]
        # 若某 IP 未被顯式打上該運營商標籤，但在該運營商偏好的機房且表現優異，亦可作為備選池
        if len(candidates_for_carrier) < top_n:
            pref_colos = carrier_prefs.get(c_key, {}).get("preferred_colos", [])
            for r in passed_results:
                if r not in candidates_for_carrier and r.colo in pref_colos:
                    candidates_for_carrier.append(r)

        scored = [score_for_carrier(r, c_key, carrier_prefs, neighbor_bonus) for r in candidates_for_carrier]
        selected = filter_subnet_diversity(scored, max_per_subnet=max_per_subnet, top_n=top_n)
        by_carrier[c_key] = selected

    # 2. 各國/地區分類與評分
    country_keys = get_supported_countries()
    by_country: dict[str, list[ScoredItem]] = {}
    for country in country_keys:
        # 雙重判定：若候選 IP 具備該國家來源標籤，或即時 trace 命中該國家，均視為該國合格候選池
        candidates_for_country = [
            r for r in passed_results
            if country in r.countries or r.country == country
        ]
        scored = [score_for_country(r, country_code=country, neighbor_bonus=neighbor_bonus) for r in candidates_for_country]
        selected = filter_subnet_diversity(scored, max_per_subnet=max_per_subnet, top_n=top_n)
        by_country[country] = selected

    # 3. 輸出文字檔案 (格式：IP#國家二字碼，含防空覆寫保護)
    for c_key, items in by_carrier.items():
        file_path = carriers_dir / f"{c_key}.txt"
        if items:
            lines = [f"{it.ip}#{it.country_code}" for it in items]
            file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info("匯出四網 [%s] -> %s (%d 個 IP，附標註 #%s)", c_key.upper(), file_path, len(items), items[0].country_code)
        else:
            if anti_empty and file_path.exists() and file_path.stat().st_size > 0:
                log.warning("四網 [%s] 當次合格 IP 為 0，已啟動防空保護保留前次檔案", c_key.upper())
            else:
                file_path.write_text("", encoding="utf-8")
                log.warning("四網 [%s] 當次合格 IP 為 0 且無前次檔案", c_key.upper())

    for country, items in by_country.items():
        file_path = countries_dir / f"{country}.txt"
        c_tag = country.upper()
        if items:
            lines = [f"{it.ip}#{c_tag}" for it in items]
            file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info("匯出地區 [%s] -> %s (%d 個 IP，附標註 #%s)", country.upper(), file_path, len(items), c_tag)
        else:
            if anti_empty and file_path.exists() and file_path.stat().st_size > 0:
                log.warning("地區 [%s] 當次合格 IP 為 0，已啟動防空保護保留前次檔案", country.upper())
            else:
                file_path.write_text("", encoding="utf-8")
                log.warning("地區 [%s] 當次合格 IP 為 0 且無前次檔案", country.upper())

    # 4. 輸出結構化 JSON 總表
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    summary_data = {
        "updated_at": now.isoformat(timespec="seconds"),
        "timestamp": int(now.timestamp()),
        "total_verified_ok": len(passed_results),
        "by_carrier": {k: [it.to_dict() for it in v] for k, v in by_carrier.items()},
        "by_country": {k: [it.to_dict() for it in v] for k, v in by_country.items()},
    }
    json_path = output_dir / "best_ips.json"
    json_path.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("結構化總表已成功產出 -> %s", json_path)

    return summary_data
