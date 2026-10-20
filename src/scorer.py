"""綜合評分加權模組 (Scorer)。

依據規格實作最終排序綜合得分計算公式：
    score = latency_ms + (loss_rate * 25.0) - (min(speed_mbps, 100.0) * 0.4) - bonus

設計理念：
- 將「高頻寬 (Throughput)、零丟包 (0% Loss)」作為核心考量，避免選出延遲低但頻寬被鎖死或嚴重丟包的節點。
- 丟包率懲罰：每增加 1% 丟包率增加 25ms 懲罰。
- 下載頻寬獎勵：每增加 1 Mbps 抵扣 0.4ms（最高採計至 100 Mbps，最多抵扣 40ms）。
- 偏好機房與冷門鄰居獎勵：扣除相應 bonus。
- 最終得分越低越佳。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .region_mapper import colo_to_country

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
            "loss_rate": round(self.loss_rate, 2),
            "download_speed": round(self.download_speed, 2),
            "is_shifted_neighbor": self.is_shifted_neighbor,
            "score": round(self.score, 2),
            "carriers": self.carriers,
            "sources": self.sources,
        }


def calculate_score(
    latency_ms: float,
    loss_rate: float,
    speed_mbps: float,
    bonus: float = 0.0,
) -> float:
    """計算綜合評分。
    
    公式：score = latency_ms + (loss_rate * 25.0) - (min(speed_mbps, 100.0) * 0.4) - bonus
    """
    capped_speed = min(max(float(speed_mbps), 0.0), 100.0)
    loss_penalty = max(float(loss_rate), 0.0) * 25.0
    speed_bonus = capped_speed * 0.4
    return float(latency_ms) + loss_penalty - speed_bonus - float(bonus)


def score_for_carrier(
    res: Any,
    carrier: str,
    carrier_prefs: dict[str, Any],
    neighbor_bonus: float = 15.0,
) -> ScoredItem:
    """為指定四網運營商計算候選分（越低越佳）。"""
    bonus = 0.0

    # 1. 冷門鄰居獎勵
    if getattr(res, "is_shifted_neighbor", False):
        bonus += neighbor_bonus

    # 2. 運營商機房偏好獎勵
    pref = carrier_prefs.get(carrier, {})
    colos = pref.get("preferred_colos", [])
    pref_bonus = float(pref.get("bonus_ms", 30.0))
    if getattr(res, "colo", "") and res.colo.upper() in [c.upper() for c in colos]:
        bonus += pref_bonus

    loss_rate = getattr(res, "loss_rate", 0.0)
    download_speed = getattr(res, "download_speed", 50.0)
    score = calculate_score(
        latency_ms=res.latency_ms,
        loss_rate=loss_rate,
        speed_mbps=download_speed,
        bonus=bonus,
    )

    return ScoredItem(
        ip=res.ip,
        colo=res.colo,
        country=res.country,
        loc=res.loc,
        latency_ms=res.latency_ms,
        loss_rate=loss_rate,
        download_speed=download_speed,
        is_shifted_neighbor=res.is_shifted_neighbor,
        score=score,
        carriers=sorted(res.carriers),
        sources=sorted(res.sources),
    )


def score_for_country(
    res: Any,
    neighbor_bonus: float = 15.0,
) -> ScoredItem:
    """為指定國家/地區計算評分（越低越佳）。"""
    bonus = 0.0
    if getattr(res, "is_shifted_neighbor", False):
        bonus += neighbor_bonus

    loss_rate = getattr(res, "loss_rate", 0.0)
    download_speed = getattr(res, "download_speed", 50.0)
    score = calculate_score(
        latency_ms=res.latency_ms,
        loss_rate=loss_rate,
        speed_mbps=download_speed,
        bonus=bonus,
    )

    return ScoredItem(
        ip=res.ip,
        colo=res.colo,
        country=res.country,
        loc=res.loc,
        latency_ms=res.latency_ms,
        loss_rate=loss_rate,
        download_speed=download_speed,
        is_shifted_neighbor=res.is_shifted_neighbor,
        score=score,
        carriers=sorted(res.carriers),
        sources=sorted(res.sources),
    )
