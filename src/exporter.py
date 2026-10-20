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
from .verifier import VerifyResult

log = logging.getLogger(__name__)


@dataclass
class ScoredItem:
    ip: str
    colo: str
    country: str | None
    loc: str
    latency_ms: float
    is_shifted_neighbor: bool
    score: float
    carriers: list[str]
    sources: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "colo": self.colo,
            "country": self.country,
            "loc": self.loc,
            "latency_ms": round(self.latency_ms, 2),
            "is_shifted_neighbor": self.is_shifted_neighbor,
            "score": round(self.score, 2),
            "carriers": self.carriers,
            "sources": self.sources,
        }


def score_for_carrier(
    res: VerifyResult,
    carrier: str,
    carrier_prefs: dict[str, Any],
    neighbor_bonus: float = 15.0,
) -> ScoredItem:
    """為指定四網運營商計算候選分（越低越佳）。"""
    score = float(res.latency_ms)

    # 1. 冷門鄰居獎勵
    if res.is_shifted_neighbor:
        score -= neighbor_bonus

    # 2. 運營商機房偏好獎勵
    pref = carrier_prefs.get(carrier, {})
    colos = pref.get("preferred_colos", [])
    bonus = float(pref.get("bonus_ms", 30.0))
    if res.colo and res.colo.upper() in [c.upper() for c in colos]:
        score -= bonus

    return ScoredItem(
        ip=res.ip,
        colo=res.colo,
        country=res.country,
        loc=res.loc,
        latency_ms=res.latency_ms,
        is_shifted_neighbor=res.is_shifted_neighbor,
        score=score,
        carriers=sorted(res.carriers),
        sources=sorted(res.sources),
    )


def score_for_country(
    res: VerifyResult,
    neighbor_bonus: float = 15.0,
) -> ScoredItem:
    """為指定國家/地區計算評分（越低越佳）。"""
    score = float(res.latency_ms)
    if res.is_shifted_neighbor:
        score -= neighbor_bonus

    return ScoredItem(
        ip=res.ip,
        colo=res.colo,
        country=res.country,
        loc=res.loc,
        latency_ms=res.latency_ms,
        is_shifted_neighbor=res.is_shifted_neighbor,
        score=score,
        carriers=sorted(res.carriers),
        sources=sorted(res.sources),
    )


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
        scored = [score_for_country(r, neighbor_bonus) for r in candidates_for_country]
        selected = filter_subnet_diversity(scored, max_per_subnet=max_per_subnet, top_n=top_n)
        by_country[country] = selected

    # 3. 輸出文字檔案 (含防空覆寫保護)
    for c_key, items in by_carrier.items():
        file_path = carriers_dir / f"{c_key}.txt"
        if items:
            file_path.write_text("\n".join(it.ip for it in items) + "\n", encoding="utf-8")
            log.info("匯出四網 [%s] -> %s (%d 個 IP)", c_key.upper(), file_path, len(items))
        else:
            if anti_empty and file_path.exists() and file_path.stat().st_size > 0:
                log.warning("四網 [%s] 當次合格 IP 為 0，已啟動防空保護保留前次檔案", c_key.upper())
            else:
                file_path.write_text("", encoding="utf-8")
                log.warning("四網 [%s] 當次合格 IP 為 0 且無前次檔案", c_key.upper())

    for country, items in by_country.items():
        file_path = countries_dir / f"{country}.txt"
        if items:
            file_path.write_text("\n".join(it.ip for it in items) + "\n", encoding="utf-8")
            log.info("匯出地區 [%s] -> %s (%d 個 IP)", country.upper(), file_path, len(items))
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
