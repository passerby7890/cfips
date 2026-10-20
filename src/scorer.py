"""歷史衰減與綜合評分模型 (Scorer - v2.0 終極修訂版)。

- EWMA 指數平滑：結合歷史穩定性，防範短跑選手誤登榜首。
- 吞吐頻寬分 (滿分 40，以 200 Mbps 為基準)。
- 階梯非線性丟包得分 (滿分 30，適應跨境輕微丟包現實)。
- 握手延遲得分 (滿分 15，80ms ~ 320ms 區間映射)。
- 路由與冷門獎勵 (滿分 15，Colo 偏好 + 冷門平移獎勵)。
- 總分最高 100 分，分數越高越優質。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class ScoredItem:
    ip: str
    colo: str
    country: str | None
    loc: str
    latency_ms: float
    loss_rate: float
    download_speed: float
    is_shifted_neighbor: bool
    instant_score: float
    final_score: float
    carriers: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    tier: str = "HEALTHY"

    @property
    def score(self) -> float:
        return self.final_score

    @property
    def country_code(self) -> str:
        if self.country:
            return str(self.country).upper()
        if self.loc:
            return str(self.loc).upper()
        if self.countries:
            return str(self.countries[0]).upper()
        return "US"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "colo": self.colo,
            "country": self.country,
            "country_code": self.country_code,
            "loc": self.loc,
            "latency_ms": round(self.latency_ms, 2),
            "loss_rate": round(self.loss_rate, 2),
            "loss_percent": round(self.loss_rate, 2),
            "download_speed": round(self.download_speed, 2),
            "speed_mbps": round(self.download_speed, 2),
            "is_shifted_neighbor": self.is_shifted_neighbor,
            "instant_score": round(self.instant_score, 2),
            "final_score": round(self.final_score, 2),
            "tier": self.tier,
            "carriers": self.carriers,
            "sources": self.sources,
            "countries": self.countries,
        }


REGIONAL_TYPICAL_RTT = {
    "HK": 25.0,
    "MO": 30.0,
    "TW": 40.0,
    "JP": 50.0,
    "KR": 50.0,
    "SG": 65.0,
    "US": 160.0,
    "DE": 180.0,
    "GB": 190.0,
    "EU": 180.0,
}


def calculate_single_score(ip_data: dict, carrier: str, preferred_colos: dict) -> float:
    """四部分加權綜合評分 (滿分 100，越高越佳)

    修正點：
    - 解決海外 Runner 跨洋回源延遲逆向淘汰亞洲節點的問題
    - 優先採計境內探針 (cdtools/hostmonit/wetest) 實測延遲
    - 分國節點採計大陸直連基準延遲，獎勵低延遲亞太節點 (HK/TW/JP/SG)
    """
    speed = float(ip_data.get("speed_mbps") or ip_data.get("download_speed") or 0.0)
    loss = float(ip_data.get("loss_percent") or ip_data.get("loss_rate") or 0.0)
    colo = str(ip_data.get("colo", "")).upper()
    c_tag = str(ip_data.get("country_code") or ip_data.get("source_country") or "").upper()
    is_neighbor = bool(ip_data.get("is_shifted_neighbor", False))

    # 1. 吞吐頻寬分 (滿分 40，以 200 Mbps 為基準)
    s_speed = min(40.0, (speed / 200.0) * 40.0)

    # 2. 階梯非線性丟包得分 (滿分 30，適應跨境輕微丟包現實)
    if loss <= 1.0:
        s_loss = 30.0 - (loss * 3.0)
    elif loss <= 3.0:
        s_loss = 27.0 - ((loss - 1.0) * 5.0)
    elif loss <= 5.0:
        s_loss = max(0.0, 17.0 - ((loss - 3.0) * 8.5))
    else:
        s_loss = 0.0

    # 3. 延遲得分 (滿分 15，映射 20ms ~ 300ms)
    # 若有境內探針實測數據 (如 cdtools 55ms、hostmonit 50ms)，優先採用
    probe_lat = ip_data.get("source_hint", {}).get("latency_ms")
    if not probe_lat and not is_neighbor:
        probe_lat = ip_data.get("latency_ms")

    if probe_lat and 10.0 <= float(probe_lat) <= 350.0:
        effective_latency = float(probe_lat)
    elif c_tag in REGIONAL_TYPICAL_RTT:
        effective_latency = REGIONAL_TYPICAL_RTT[c_tag]
    else:
        raw_lat = float(ip_data.get("latency_ms", 150.0))
        effective_latency = min(raw_lat, 180.0)

    s_latency = max(0.0, min(15.0, ((300.0 - effective_latency) / (300.0 - 20.0)) * 15.0))

    # 4. 路由與偏好地區獎勵 (滿分 15)
    pref_list = preferred_colos.get(carrier.lower(), [])
    colo_matches = colo in pref_list
    country_matches = (
        (carrier == "ct" and c_tag in ("TW", "HK", "JP", "US")) or
        (carrier == "cu" and c_tag in ("JP", "HK", "DE", "GB", "US")) or
        (carrier == "cm" and c_tag in ("HK", "SG", "JP", "TW", "US"))
    )
    s_colo = 10.0 if (colo_matches or country_matches) else 2.0
    s_neighbor = 5.0 if is_neighbor else 0.0
    s_routing = s_colo + s_neighbor

    return round(s_speed + s_loss + s_latency + s_routing, 2)


def calculate_country_score(ip_data: dict, country_code: str) -> float:
    """分國維度綜合評分 (滿分 100，越高越佳)"""
    speed = float(ip_data.get("speed_mbps") or ip_data.get("download_speed") or 0.0)
    loss = float(ip_data.get("loss_percent") or ip_data.get("loss_rate") or 0.0)
    latency = float(ip_data.get("latency_ms", 999.0))
    is_neighbor = bool(ip_data.get("is_shifted_neighbor", False))

    s_speed = min(40.0, (speed / 200.0) * 40.0)

    if loss <= 1.0:
        s_loss = 30.0 - (loss * 3.0)
    elif loss <= 3.0:
        s_loss = 27.0 - ((loss - 1.0) * 5.0)
    elif loss <= 5.0:
        s_loss = max(0.0, 17.0 - ((loss - 3.0) * 8.5))
    else:
        s_loss = 0.0

    s_latency = max(0.0, min(15.0, ((320.0 - latency) / (320.0 - 80.0)) * 15.0))

    # 若 trace 偵測國家精準匹配目標國家，給予 10 分獎勵
    detected_country = str(ip_data.get("country") or ip_data.get("loc") or "").upper()
    s_geo = 10.0 if detected_country == country_code.upper() else 5.0
    s_neighbor = 5.0 if is_neighbor else 0.0
    s_routing = s_geo + s_neighbor

    return round(s_speed + s_loss + s_latency + s_routing, 2)


def apply_ewma_history(current_ip: str, instant_score: float, history_dict: dict, decay: float = 0.7) -> float:
    """EWMA 指數平滑：結合歷史穩定性，防範短跑選手誤登榜首"""
    prev_score = history_dict.get(current_ip, instant_score)
    final_score = (prev_score * decay) + (instant_score * (1.0 - decay))
    history_dict[current_ip] = round(final_score, 2)
    return round(final_score, 2)


def load_history(history_file: Path | str) -> dict[str, float]:
    p = Path(history_file)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items()}
    except Exception as exc:
        log.warning("讀取歷史加權檔案失敗：%s", exc)
    return {}


def save_history(history_dict: dict[str, float], history_file: Path | str) -> None:
    p = Path(history_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(history_dict, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("成功持久化 %d 筆 IP 歷史加權分數 -> %s", len(history_dict), p)
    except Exception as exc:
        log.error("儲存歷史加權檔案失敗：%s", exc)
