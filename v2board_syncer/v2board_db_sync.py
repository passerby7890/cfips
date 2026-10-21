#!/usr/bin/env python3
"""
=============================================================================
V2Board / Xboard (Xiao 版) MySQL 資料庫直接極速替換引擎 (v3.0.0)
=============================================================================
特色：
  1. 【0 延遲直連資料庫】：免 Token、免登入、無 Session 過期、無 Web 路由問題！
  2. 【自動讀取 .env】：自動抓取面板 .env 中的 MySQL 與 Redis 連線資訊，零設定！
  3. 【原子級安全更新】：只更新 v2_server_vless 中的 host 欄位為純 IP，嚴格保留所有其他欄位！
  4. 【自動刷新面板快取】：更新後自動調用 php artisan cache:clear，前台與客戶端立即生效！
  5. 【支援隨機輪替】：可選固定榜首 (top) 或前 3~5 名隨機 (random_top3) 分散負載！
  6. 【支援 --dry-run 模擬】與 【--daemon --interval 常駐守護】模式！
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CURRENT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = CURRENT_DIR / "config.json"
DEFAULT_CACHE_DIR = CURRENT_DIR / "cache"
DEFAULT_BACKUP_DIR = CURRENT_DIR / "backups"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("v2board_db_sync")

DEFAULT_TABLES = {
    "ct": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/ct.txt",
    "cu": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cu.txt",
    "cm": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cm.txt",
}

IP_REGEX = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})")


def find_env_file() -> Optional[Path]:
    """自動尋找 V2Board 面板根目錄的 .env 檔案"""
    candidates = [
        CURRENT_DIR.parent / ".env",
        CURRENT_DIR / ".env",
        Path("/www/wwwroot/0n21.com/.env")
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def parse_env_file(env_path: Path) -> Dict[str, str]:
    """解析 .env 檔案獲取 DB 配置"""
    res = {}
    if not env_path or not env_path.is_file():
        return res
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        res[k.strip()] = v.strip().strip("'\"")
    return res


def fetch_ip_table(source: str, cache_dir: Optional[Path] = None) -> List[Tuple[str, str]]:
    """自本地檔案或 HTTP URL 讀取 IP 清單，具備本機快取容災能力"""
    content = ""
    # 1. 嘗試讀取本地檔案
    p = Path(source)
    if not p.is_absolute():
        if (CURRENT_DIR / source).is_file():
            p = CURRENT_DIR / source
        elif (CURRENT_DIR.parent / source).is_file():
            p = CURRENT_DIR.parent / source

    if p.is_file():
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.warning("讀取本地表 %s 失敗: %s", p, e)

    # 2. 嘗試網路下載 (HTTP/HTTPS)
    cache_file = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", source.split("/")[-1])
        cache_file = cache_dir / f"cached_{safe_name}"

    if not content and (source.startswith("http://") or source.startswith("https://")):
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(source, headers={"User-Agent": "curl/7.88.1"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
                if cache_file and content:
                    cache_file.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.warning("下載遠端表失敗: %s (%s)", source, e)
            if cache_file and cache_file.is_file():
                logger.info("自動啟用本地快取備援: %s", cache_file)
                content = cache_file.read_text(encoding="utf-8", errors="ignore")

    results = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tag = ""
        if "#" in line:
            parts = line.split("#", 1)
            line = parts[0].strip()
            tag = parts[1].strip().upper()
        m = IP_REGEX.match(line)
        if m:
            results.append((m.group(1), tag or "CF"))
    return results


def select_best_ip(
    ip_list: List[Tuple[str, str]],
    preferred_country: str = "",
    rank: int = 1,
    strategy: str = "top"
) -> Optional[Tuple[str, str]]:
    """依落地國與策略挑選極品 IP"""
    if not ip_list:
        return None

    candidates = ip_list
    if preferred_country:
        filtered = [item for item in ip_list if item[1].upper() == preferred_country.upper()]
        if filtered:
            candidates = filtered

    strat = str(strategy).lower().strip()
    if "random" in strat:
        m = re.search(r"\d+", strat)
        limit = int(m.group(0)) if m else 3
        pool = candidates[:max(1, limit)]
        return random.choice(pool)

    idx = max(0, rank - 1)
    idx = min(idx, len(candidates) - 1)
    return candidates[idx]


class MySQLManager:
    """直接透過 mysql 命令列或連線管理資料庫"""

    def __init__(self, host: str, port: int, user: str, password: str, database: str):
        self.host = host or "127.0.0.1"
        self.port = str(port or 3306)
        self.user = user
        self.password = password
        self.database = database

    def run_query(self, sql: str) -> str:
        cmd = [
            "mysql",
            f"-h{self.host}",
            f"-P{self.port}",
            f"-u{self.user}",
            f"-p{self.password}",
            self.database,
            "-N",
            "-e",
            sql
        ]
        res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        return res.decode("utf-8", errors="replace")

    def get_vless_nodes(self, node_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        if not node_ids:
            return {}
        ids_str = ",".join(str(i) for i in node_ids)
        sql = f"SELECT id, name, host, port FROM v2_server_vless WHERE id IN ({ids_str});"
        raw = self.run_query(sql)
        nodes = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                nid = int(parts[0])
                nodes[nid] = {
                    "id": nid,
                    "name": parts[1],
                    "host": parts[2],
                    "port": parts[3]
                }
        return nodes

    def update_vless_host(self, node_id: int, new_host: str) -> bool:
        safe_host = re.sub(r"[^0-9a-zA-Z.:_-]", "", new_host)
        now_ts = int(time.time())
        sql = f"UPDATE v2_server_vless SET host = '{safe_host}', updated_at = {now_ts} WHERE id = {node_id};"
        self.run_query(sql)
        return True


def clear_v2board_cache(panel_root: Path):
    """清除 V2Board 快取使節點立即生效"""
    try:
        artisan = panel_root / "artisan"
        if artisan.is_file():
            subprocess.run(["php", str(artisan), "cache:clear"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("已成功清除 Laravel 應用快取 (php artisan cache:clear)")
    except Exception as e:
        logger.warning("執行 cache:clear 失敗: %s", e)

    try:
        subprocess.run(["redis-cli", "del", "SERVER_VLESS"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def perform_db_sync(
    db_mgr: MySQLManager,
    panel_root: Path,
    tables: Dict[str, str],
    nodes_to_update: List[Dict[str, Any]],
    cache_dir: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
    dry_run: bool = False
) -> bool:
    print("======================================================================")
    print("⚡ V2Board (Xiao 版) MySQL 直接極速入口純 IP 替換引擎")
    print(f"[*] 連線資料庫: {db_mgr.database} ({db_mgr.host}:{db_mgr.port})")
    print("======================================================================")

    # 1. 載入三張表
    table_cache: Dict[str, List[Tuple[str, str]]] = {}
    for t_name, t_url in tables.items():
        ips = fetch_ip_table(t_url, cache_dir=cache_dir)
        table_cache[t_name] = ips
        src_label = t_url.split("/")[-1]
        print(f"[*] 載入 [{t_name.upper()}] 表 ({src_label}): 取得 {len(ips)} 個極品 IP")

    # 2. 查詢資料庫節點
    target_ids = [int(item["id"]) for item in nodes_to_update]
    try:
        nodes = db_mgr.get_vless_nodes(target_ids)
    except Exception as e:
        print(f"❌ 資料庫查詢失敗: {e}")
        return False

    print("----------------------------------------------------------------------")
    success_count = 0
    has_modified = False

    for item in nodes_to_update:
        node_id = int(item["id"])
        t_name = str(item.get("table", "ct")).lower()
        pref_country = str(item.get("country", "")).upper()
        rank = int(item.get("rank", 1))
        strategy = str(item.get("strategy", "top"))

        node = nodes.get(node_id)
        if not node:
            print(f"❌ 資料庫 v2_server_vless 中找不到 ID 為 [{node_id}] 的節點！")
            continue

        name = node["name"]
        curr_host = node["host"]
        port = node["port"]

        ip_pool = table_cache.get(t_name, [])
        best_choice = select_best_ip(ip_pool, preferred_country=pref_country, rank=rank, strategy=strategy)
        if not best_choice:
            print(f"❌ 表 [{t_name}] 中無可用 IP！(節點 ID: {node_id})")
            continue

        new_ip, tag = best_choice
        strat_desc = "隨機輪替" if "random" in strategy else f"第 {rank} 名"
        print(f"[*] 節點 ID: {node_id} 【{name}】")
        print(f"    - 原入口地址 (host)  : {curr_host}:{port}")
        print(f"    - 綁定表與策略       : {t_name.upper()} 表 (#{tag}) | 模式: {strat_desc}")
        print(f"    - 選定純 IP          : {new_ip} (#{tag})")

        if curr_host == new_ip:
            print(f"    - 執行結果           : ⚡ 目前已是選定 IP ({new_ip})，無需變更！")
            success_count += 1
            print("----------------------------------------------------------------------")
            continue

        if dry_run:
            print(f"    - 執行結果           : 🔍 [DRY-RUN 模擬] 預計將 host 由 {curr_host} 改為: {new_ip}")
            success_count += 1
            print("----------------------------------------------------------------------")
            continue

        # 備份
        if backup_dir:
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            b_path = backup_dir / f"db_node_{node_id}_{ts}.json"
            b_path.write_text(json.dumps(node, indent=2, ensure_ascii=False), encoding="utf-8")

        # 寫入 MySQL
        try:
            db_mgr.update_vless_host(node_id, new_ip)
            print(f"    - 執行結果           : ✅ MySQL 更新成功！{curr_host} ➡️ {new_ip}")
            success_count += 1
            has_modified = True
        except Exception as e:
            print(f"    - 執行結果           : ❌ MySQL 更新出錯: {e}")
        print("----------------------------------------------------------------------")

    if has_modified and not dry_run:
        clear_v2board_cache(panel_root)

    print(f"🎉 執行完畢！成功處理 {success_count}/{len(nodes_to_update)} 個節點。")
    print("======================================================================")
    return True


def main():
    parser = argparse.ArgumentParser(description="V2Board (Xiao 版) MySQL 直接極速入口純 IP 替換工具")
    parser.add_argument("--config", "-c", default=str(DEFAULT_CONFIG), help="設定檔路徑")
    parser.add_argument("--node", "-n", type=int, help="指定替換的單一節點 ID (例如: 7)")
    parser.add_argument("--table", "-t", choices=["ct", "cu", "cm"], help="指定使用的表 (ct/cu/cm)")
    parser.add_argument("--country", help="指定偏好落地國 (JP/HK/SG/US)")
    parser.add_argument("--strategy", choices=["top", "random", "random_top3", "random_top5"], default="top", help="IP 選取策略")
    parser.add_argument("--daemon", action="store_true", help="啟用常駐守護模式 (背景持續循環執行)")
    parser.add_argument("--interval", type=int, default=1800, help="常駐循環時間間隔，單位秒 (預設: 1800 秒 / 30 分鐘)")
    parser.add_argument("--dry-run", action="store_true", help="模擬執行 (不實際寫入資料庫)")

    args = parser.parse_args()

    cfg_file = Path(args.config).resolve()
    conf: Dict[str, Any] = {}
    if cfg_file.is_file():
        try:
            conf = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("讀取設定檔失敗: %s", e)

    # 自動讀取 .env 資訊
    env_file = find_env_file()
    env_vars = parse_env_file(env_file) if env_file else {}
    panel_root = env_file.parent if env_file else CURRENT_DIR.parent

    db_conf = conf.get("database", {})
    db_mgr = MySQLManager(
        host=db_conf.get("host") or env_vars.get("DB_HOST", "127.0.0.1"),
        port=int(db_conf.get("port") or env_vars.get("DB_PORT", 3306)),
        user=db_conf.get("username") or env_vars.get("DB_USERNAME", "0n21_com"),
        password=db_conf.get("password") or env_vars.get("DB_PASSWORD", "MStrTX7EpG3wtlXM"),
        database=db_conf.get("database") or env_vars.get("DB_DATABASE", "0n21_com")
    )

    if args.node is not None:
        nodes = [{
            "id": args.node,
            "table": args.table or "ct",
            "country": args.country or "",
            "strategy": args.strategy or "top",
            "rank": 1
        }]
    else:
        nodes = conf.get("nodes", [])

    if not nodes:
        logger.error("未指定任何要替換的節點 ID！")
        sys.exit(1)

    tables = conf.get("tables", DEFAULT_TABLES)
    cache_dir = DEFAULT_CACHE_DIR
    backup_dir = DEFAULT_BACKUP_DIR

    if args.daemon:
        print(f"🕒 啟動常駐守護模式，每隔 {args.interval} 秒 (約 {args.interval // 60} 分鐘) 自動執行一次同步...")
        while True:
            perform_db_sync(
                db_mgr=db_mgr,
                panel_root=panel_root,
                tables=tables,
                nodes_to_update=nodes,
                cache_dir=cache_dir,
                backup_dir=backup_dir,
                dry_run=args.dry_run
            )
            print(f"\n💤 進入休眠，將於 {args.interval} 秒後執行下一次替換 (按 Ctrl+C 結束)...")
            time.sleep(args.interval)
    else:
        success = perform_db_sync(
            db_mgr=db_mgr,
            panel_root=panel_root,
            tables=tables,
            nodes_to_update=nodes,
            cache_dir=cache_dir,
            backup_dir=backup_dir,
            dry_run=args.dry_run
        )
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
