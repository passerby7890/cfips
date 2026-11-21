#!/usr/bin/env python3
"""XIU2/CloudflareSpeedTest ip.txt 三網非 1034 / 低延遲亞太子網離線探勘工具。

用途：
1. 從 https://raw.githubusercontent.com/XIU2/CloudflareSpeedTest/master/ip.txt（或 data/cf_ranges.txt）
   載入 Cloudflare 全量 IPv4 CIDR。
2. 抽樣拆分 /24 子網並以併發 WS 101 握手濾除 Error 1034 網段（不消耗 Globalping 配額）。
3. 選配：加上 `--probe-cn` 時，對通過 101 的新 /24 代表 IP 發起大陸三網 Globalping 實測，
   找出可直接加入 data/seeds_{ct,cu,cm}.txt 的低延遲亞太直連子網。
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cn_real_tester import probe_subnet_from_mainland_cn
from src.region_mapper import load_xiu2_cf_cidrs
from src.verifier import batch_verify_with_health_stats


def sample_subnets_from_cidrs(cidrs: list[str], max_per_cidr: int = 8) -> list[dict]:
    candidates: list[dict] = []
    seen_subs: set[str] = set()
    for cidr in cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
            if not isinstance(net, ipaddress.IPv4Network):
                continue
            subs = [net] if net.prefixlen >= 24 else list(net.subnets(new_prefix=24))
            picks = subs if len(subs) <= max_per_cidr else random.sample(subs, max_per_cidr)
            for s in picks:
                sub_str = str(s)
                if sub_str in seen_subs:
                    continue
                seen_subs.add(sub_str)
                ip = str(s[1]) if s.num_addresses > 2 else str(s.network_address)
                candidates.append({"ip": ip, "subnet": sub_str, "domain": "xiu2_scan"})
        except Exception:
            continue
    return candidates


async def run_scan(max_per_cidr: int, probe_cn: bool, carrier: str) -> None:
    cidrs = load_xiu2_cf_cidrs(PROJECT_ROOT / "data" / "cf_ranges.txt")
    cands = sample_subnets_from_cidrs(cidrs, max_per_cidr=max_per_cidr)
    print(f"[*] 載入 {len(cidrs)} 個 XIU2 官方 CIDR，抽樣 {len(cands)} 個 /24 子網代表 IP 執行 2053 WS 101 快篩...")

    alive, _ = await batch_verify_with_health_stats(cands, concurrency=15)
    print(f"[+] 通過 101 握手（無 Error 1034）之 /24 子網共 {len(alive)}/{len(cands)} 個：")
    for item in alive:
        print(f"    PASS {item['subnet']} (rep={item['ip']})")

    if probe_cn and alive:
        print(f"\n[*] 對通過 101 的子網發起 [{carrier.upper()}] 大陸探針實測...")
        for item in alive[:20]:
            cty, ms, loss = probe_subnet_from_mainland_cn(item["ip"], carrier)
            if cty not in ("US", "GB", "DE", "FR", "NL", "UNKNOWN") and loss <= 18.0:
                print(f"    ⭐ 發現優質直連子網: {item['subnet']}#{cty} ({ms}ms, loss={loss}%)")


def main() -> None:
    parser = argparse.ArgumentParser(description="XIU2/CloudflareSpeedTest ip.txt 子網探勘工具")
    parser.add_argument("--max-per-cidr", type=int, default=6, help="每個 CIDR 抽樣的 /24 子網數")
    parser.add_argument("--probe-cn", action="store_true", help="對通過 101 的子網發起 Globalping 大陸探測")
    parser.add_argument("--carrier", choices=["ct", "cu", "cm"], default="ct", help="大陸探測運營商")
    args = parser.parse_args()
    asyncio.run(run_scan(args.max_per_cidr, args.probe_cn, args.carrier))


if __name__ == "__main__":
    main()
