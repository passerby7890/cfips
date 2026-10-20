#!/usr/bin/env python3
"""CF-Global-QuadNet-Feed 主程式入口。

GitHub Actions 雲端四網與多國優選 IP 自動產出系統。
- 階段一：ECS DoH 偽裝解析 + 境內實測榜單 + 分國源 + 本地種子
- 階段二：同 /24 網段冷門鄰居平移抽樣
- 階段三：端到端 TLS + /cdn-cgi/trace (colo) + WebSocket 101 回源測活
- 階段四：雙維度加權評分、跨 /24 網段容災篩選與檔案匯出
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from src import __version__
from src.collector import collect_all_candidates
from src.exporter import export_all
from src.neighbor_shifter import shift_neighbors
from src.region_mapper import get_country_name
from src.verifier import OriginVerifier

PROJECT_ROOT = Path(__file__).resolve().parent

# 日誌格式設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cf-global-quadnet-feed",
        description="Cloudflare 四網與多國優選 IP 自動化雲端生成系統",
    )
    parser.add_argument(
        "-c", "--config",
        default="config/config.yaml",
        help="設定檔路徑（預設 config/config.yaml）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="覆寫測活併發數（預設 50）",
    )
    parser.add_argument(
        "--no-ecs",
        action="store_true",
        help="停用 ECS DoH 偽裝解析策略",
    )
    parser.add_argument(
        "--no-neighbor",
        action="store_true",
        help="停用同 /24 網段冷門鄰居平移演算法",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模擬執行模式（輸出報表但不覆寫實際檔案）",
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args()


def load_config(cfg_path: Path) -> dict[str, Any]:
    if not cfg_path.is_file():
        sys.exit(f"[-] 設定檔不存在：{cfg_path}")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return data
    except Exception as exc:
        sys.exit(f"[-] 設定檔 YAML 解析失敗：{exc}")


def load_anchors(anchors_path: Path) -> list[dict[str, Any]]:
    if not anchors_path.is_file():
        log.warning("錨點檔案不存在：%s", anchors_path)
        return []
    try:
        data = json.loads(anchors_path.read_text(encoding="utf-8"))
        return data.get("anchors", [])
    except Exception as exc:
        log.error("錨點檔案解析失敗：%s", exc)
        return []


def print_table(title: str, headers: list[str], rows: list[list[str]]) -> None:
    print(f"\n=== {title} ===")
    if not rows:
        print("  (無資料)")
        return

    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            # 支援包含中文寬度計算（粗略兩倍寬）
            w = sum(2 if ord(c) > 127 else 1 for c in str(cell))
            if w > col_widths[idx]:
                col_widths[idx] = w

    header_fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    sep = "  ".join("-" * w for w in col_widths)
    print(header_fmt.format(*headers))
    print(sep)
    for row in rows:
        print(header_fmt.format(*row))


async def run_pipeline(args: argparse.Namespace) -> int:
    started_at = time.time()
    cfg_path = (PROJECT_ROOT / args.config).resolve()
    config = load_config(cfg_path)

    # 參數覆寫
    if args.concurrency:
        config["concurrency"] = args.concurrency
    if args.no_ecs:
        config.setdefault("collector", {}).setdefault("ecs_doh", {})["enabled"] = False
    if args.no_neighbor:
        config.setdefault("neighbor_shifter", {})["enabled"] = False

    anchors_rel = config.get("collector", {}).get("ecs_doh", {}).get("anchors_file", "data/cn_anchors.json")
    anchors = load_anchors((PROJECT_ROOT / anchors_rel).resolve())

    origin_cfg = config.get("origin", {})
    log.info("=" * 65)
    log.info("CF-Global-QuadNet-Feed v%s 啟動", __version__)
    log.info("目標源站：%s:%s (WS: %s, 預期 101)", origin_cfg.get('host'), origin_cfg.get('port'), origin_cfg.get('ws_path'))
    log.info("=" * 65)

    # 1. 階段一：多維度情報收集
    candidates = await collect_all_candidates(config, anchors, PROJECT_ROOT)

    # 2. 階段二：同 /24 網段冷門鄰居平移演算法
    expanded_candidates = shift_neighbors(candidates, config.get("neighbor_shifter", {}))

    # 3. 階段三：端到端真實回源測活與機房國家識別
    verifier = OriginVerifier(origin_cfg, concurrency=config.get("concurrency", 50))
    results = await verifier.verify_all(expanded_candidates)

    # 4. 階段四：雙維度加權排序與匯出
    output_dir = PROJECT_ROOT / config.get("exporter", {}).get("output_dir", "output")
    if args.dry_run:
        output_dir = PROJECT_ROOT / "output_dryrun"
        log.info("模擬模式：輸出將儲存至臨時目錄 %s", output_dir)

    summary = export_all(results, config, output_dir)

    # 5. 終端報表渲染
    carrier_rows = []
    for c_key in ["ct", "cu", "cm", "edu"]:
        items = summary["by_carrier"].get(c_key, [])
        c_name = config.get("carrier_preferences", {}).get(c_key, {}).get("name", c_key.upper())
        if items:
            best_ip = items[0]["ip"]
            colos = ",".join(sorted(set(it["colo"] for it in items)))
            lat_range = f"{min(it['latency_ms'] for it in items):.1f}~{max(it['latency_ms'] for it in items):.1f}ms"
            carrier_rows.append([c_name, f"output/carriers/{c_key}.txt", str(len(items)), best_ip, colos, lat_range])
        else:
            carrier_rows.append([c_name, f"output/carriers/{c_key}.txt", "0", "-", "-", "-"])

    print_table("大陸四網優選結果", ["運營商", "匯出檔案", "挑選數", "最佳 IP", "機房分佈", "握手延遲區間"], carrier_rows)

    country_rows = []
    for c_code, items in summary["by_country"].items():
        c_name = get_country_name(c_code)
        if items:
            best_ip = items[0]["ip"]
            colos = ",".join(sorted(set(it["colo"] for it in items)))
            lat_range = f"{min(it['latency_ms'] for it in items):.1f}~{max(it['latency_ms'] for it in items):.1f}ms"
            country_rows.append([f"{c_name} ({c_code.upper()})", f"output/countries/{c_code}.txt", str(len(items)), best_ip, colos, lat_range])
        else:
            country_rows.append([f"{c_name} ({c_code.upper()})", f"output/countries/{c_code}.txt", "0", "-", "-", "-"])

    print_table("國際與地區優選結果", ["地區", "匯出檔案", "挑選數", "最佳 IP", "機房分佈", "握手延遲區間"], country_rows)

    elapsed = time.time() - started_at
    log.info("全流程執行完畢，總耗時: %.1f 秒，合格回源節點總數: %d 個", elapsed, summary["total_verified_ok"])
    return 0


def main() -> None:
    args = parse_args()
    try:
        code = asyncio.run(run_pipeline(args))
        sys.exit(code)
    except KeyboardInterrupt:
        log.info("使用者中斷執行")
        sys.exit(130)
    except Exception as exc:
        log.exception("執行過程發生未預期例外：%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
