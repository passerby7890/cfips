#!/usr/bin/env python3
"""
=============================================================================
V2Board / Xboard (Xiao 版) 節點入口純 IP 獨立自動替換工具 (v2.5.0)
=============================================================================
目錄獨立封裝版：
  本資料夾為獨立工具包，可整包複製到任何 V2Board 伺服器上直接執行，
  無需依賴外部龐大專案與額外 pip 套件 (僅使用 Python 3 原生標準庫)。

核心特色：
  1. 輸入 node ID 後自動換！(支援命令列參數直接傳入，或互動式輸入)
  2. 專注 VLESS 協議：只換入口地址 (server)，100% 嚴格原樣保留 port、UUID、TLS SNI、Host、Path
  3. 支援兩種選取策略：
     - 固定榜首 ("top"): 取該線路評分最高的最優 IP
     - 隨機輪替 ("random_top3" / "random_top5"): 從前 3~5 名極品中隨機挑選，分散節點負載
  4. 支援兩種定時模式：
     - Linux Crontab 排程 (單次執行後退出)
     - 內建守護進程模式 (--daemon --interval 秒數，本機自動常駐循環)
  5. 支援斷網容災快取 (自動備援本機歷史表，GitHub 斷線也能繼續運作)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import ssl
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
logger = logging.getLogger("v2board_sync")

DEFAULT_TABLES = {
    "ct": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/ct.txt",
    "cu": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cu.txt",
    "cm": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cm.txt",
}

IP_REGEX = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})")


def fetch_ip_table(source: str, cache_dir: Optional[Path] = None) -> List[Tuple[str, str]]:
    """自本地檔案或 HTTP URL 讀取 IP 清單，具備本機快取容災能力"""
    content = ""
    # 1. 嘗試讀取本地檔案 (相對路徑優先解析為同目錄或專案目錄)
    p = Path(source)
    if not p.is_absolute():
        if (CURRENT_DIR / source).is_file():
            p = CURRENT_DIR / source
        elif (CURRENT_DIR.parent / source).is_file():
            p = CURRENT_DIR.parent / source

    if p.is_file():
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
            logger.debug("從本地檔案載入: %s", p)
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
            # 斷網時自動自快取備援載入
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


class V2BoardAPI:
    """V2Board / Xiao 版面板管理員 API Client"""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def _headers(self) -> Dict[str, str]:
        h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) V2BoardSync/2.5.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.token:
            h["Authorization"] = self.token
            h["auth-data"] = self.token
        return h

    def login(self, email: str, password: str) -> bool:
        """使用管理員信箱與密碼自動登入，取得 Authorization Token (免手動找 Token)"""
        url = f"{self.base_url}/api/v1/passport/auth/login"
        payload = {"email": email, "password": password}
        encoded = json.dumps(payload).encode("utf-8")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) V2BoardSync/2.5.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15, context=self.ctx) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                data = res.get("data", {})
                token = data.get("token") or data.get("auth_data")
                if token:
                    self.token = str(token)
                    logger.info("管理員帳號登入成功，已自動取得 Token！")
                    return True
                logger.warning("登入回應中未包含明確 token: %s", res)
                return False
        except Exception as e:
            logger.error("登入面板失敗: %s", e)
            return False

    def get_nodes(self, protocol: str = "vless") -> List[Dict[str, Any]]:
        """取得指定協議的全部節點清單"""
        url = f"{self.base_url}/api/v1/admin/server/{protocol}/fetch"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15, context=self.ctx) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                data = res.get("data", [])
                if isinstance(data, list):
                    return data
                return []
        except urllib.error.HTTPError as e:
            alt_url = f"{self.base_url}/api/v1/admin/server/manage/getNodes"
            req2 = urllib.request.Request(alt_url, headers=self._headers(), method="GET")
            with urllib.request.urlopen(req2, timeout=15, context=self.ctx) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                data = res.get("data", [])
                return [n for n in data if n.get("type") == protocol or protocol in str(n.get("name", "")).lower()]

    def update_node_server(self, protocol: str, node: Dict[str, Any], new_ip: str) -> bool:
        """只替換 node 的 server 欄位，原樣保留其他所有欄位並保存"""
        url = f"{self.base_url}/api/v1/admin/server/{protocol}/save"
        payload = dict(node)
        payload["server"] = new_ip

        encoded = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=encoded, headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=15, context=self.ctx) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return res.get("data") is True or res.get("data") == 1 or "success" in str(res).lower()


def select_best_ip(
    ip_list: List[Tuple[str, str]],
    preferred_country: str = "",
    rank: int = 1,
    strategy: str = "top"
) -> Optional[Tuple[str, str]]:
    """篩選 IP:
      - strategy='top': 固定選擇第 rank 名最優 IP (預設榜首)
      - strategy='random' / 'random_top3' / 'random_top5': 從前 3~5 名中隨機挑選
    """
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


def backup_node_config(node: Dict[str, Any], backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    node_id = node.get("id", "unknown")
    backup_path = backup_dir / f"node_{node_id}_{ts}.json"
    backup_path.write_text(json.dumps(node, indent=2, ensure_ascii=False), encoding="utf-8")
    return backup_path


def perform_sync(
    v2board_url: str,
    v2board_token: str = "",
    admin_email: str = "",
    admin_password: str = "",
    tables: Dict[str, str] = None,
    nodes_to_update: List[Dict[str, Any]] = None,
    cache_dir: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
    dry_run: bool = False
) -> bool:
    print("======================================================================")
    print("🚀 V2Board (Xiao 版) 節點入口純 IP 獨立自動替換引擎")
    print(f"[*] 面板目標: {v2board_url}")
    print("======================================================================")

    # 1. 載入三張表 (支援本地快取容災)
    table_cache: Dict[str, List[Tuple[str, str]]] = {}
    for t_name, t_url in (tables or {}).items():
        ips = fetch_ip_table(t_url, cache_dir=cache_dir)
        table_cache[t_name] = ips
        src_label = t_url.split("/")[-1]
        print(f"[*] 載入 [{t_name.upper()}] 表 ({src_label}): 成功取得 {len(ips)} 個極品 IP")

    # 2. 獲取節點清單
    api = V2BoardAPI(base_url=v2board_url, token=v2board_token)

    # 自動登入
    if not dry_run and not api.token:
        if admin_email and admin_password:
            print(f"[*] 檢測到配置了管理員帳號 ({admin_email})，正在自動登入面板...")
            ok = api.login(admin_email, admin_password)
            if not ok:
                print("❌ 自動登入失敗，請檢查管理員帳號與密碼！")
                return False
        else:
            print("❌ 未提供 Token 且未提供管理員信箱密碼，無法連接面板！")
            return False

    nodes_by_proto: Dict[str, Dict[int, Dict[str, Any]]] = {}

    protocols = list({item.get("protocol", "vless").lower() for item in (nodes_to_update or [])})
    for proto in protocols:
        if not dry_run:
            try:
                print(f"[*] 正在從面板獲取 [{proto}] 節點清單...")
                node_list = api.get_nodes(proto)
                nodes_by_proto[proto] = {int(n["id"]): n for n in node_list if "id" in n}
                print(f"    -> 獲取到 {len(node_list)} 個節點")
            except Exception as e:
                logger.error("無法獲取 [%s] 節點: %s", proto, e)
                print(f"❌ 無法連接面板或 Token 無效: {e}")
                return False
        else:
            # Dry-run 模擬節點
            nodes_by_proto[proto] = {
                7: {"id": 7, "name": "JP 电信专用-日本(3倍)", "server": "www.090902.xyz", "port": 2053},
                6: {"id": 6, "name": "HK 移动专用-香港(3倍)", "server": "cf.godns.cc", "port": 2053}
            }

    # 3. 執行替換
    print("----------------------------------------------------------------------")
    success_count = 0
    for item in nodes_to_update:
        node_id = int(item["id"])
        proto = str(item.get("protocol", "vless")).lower()
        t_name = str(item.get("table", "ct")).lower()
        pref_country = str(item.get("country", "")).upper()
        rank = int(item.get("rank", 1))
        strategy = str(item.get("strategy", "top"))

        node_dict = nodes_by_proto.get(proto, {}).get(node_id)
        if not node_dict:
            print(f"❌ 找不到節點 ID [{node_id}] (協議: {proto})，略過！")
            continue

        name = node_dict.get("name", f"Node-{node_id}")
        curr_server = str(node_dict.get("server", "")).strip()
        port = node_dict.get("port", 443)

        # 取對應表的 IP
        ip_pool = table_cache.get(t_name, [])
        best_choice = select_best_ip(ip_pool, preferred_country=pref_country, rank=rank, strategy=strategy)
        if not best_choice:
            print(f"❌ 表 [{t_name}] 中無可用 IP！(節點 ID: {node_id})")
            continue

        new_ip, tag = best_choice
        strat_desc = "隨機輪替" if "random" in strategy else f"第 {rank} 名"
        print(f"[*] 節點 ID: {node_id} 【{name}】")
        print(f"    - 原入口地址 (server) : {curr_server}:{port}")
        print(f"    - 綁定表與策略       : {t_name.upper()} 表 (#{tag}) | 模式: {strat_desc}")
        print(f"    - 選定純 IP          : {new_ip} (#{tag})")

        if curr_server == new_ip:
            print(f"    - 執行結果           : ⚡ 目前已是選定 IP ({new_ip})，無需變更！")
            success_count += 1
            print("----------------------------------------------------------------------")
            continue

        if dry_run:
            print(f"    - 執行結果           : 🔍 [DRY-RUN 模擬] 預計將入口改為: {new_ip}")
            success_count += 1
            print("----------------------------------------------------------------------")
            continue

        # 自動備份
        if backup_dir:
            b_file = backup_node_config(node_dict, backup_dir)

        try:
            ok = api.update_node_server(proto, node_dict, new_ip)
            if ok:
                print(f"    - 執行結果           : ✅ 成功替換！{curr_server} ➡️ {new_ip}")
                success_count += 1
            else:
                print(f"    - 執行結果           : ❌ 面板拒絕保存，請檢查 Token 權限！")
        except Exception as e:
            print(f"    - 執行結果           : ❌ 更新例外錯誤: {e}")
        print("----------------------------------------------------------------------")

    print(f"🎉 執行完畢！成功處理 {success_count}/{len(nodes_to_update)} 個節點。")
    print("======================================================================")
    return True


def main():
    parser = argparse.ArgumentParser(description="V2Board (Xiao 版) 節點入口純 IP 獨立自動替換工具")
    parser.add_argument("--config", "-c", default=str(DEFAULT_CONFIG), help="設定檔路徑 (預設: 同目錄 config.json)")
    parser.add_argument("--url", help="覆蓋 V2Board 地址或 IP")
    parser.add_argument("--token", help="覆蓋 V2Board 管理員 Token")
    parser.add_argument("--node", "-n", type=int, help="指定替換的單一節點 ID (例如: 7)")
    parser.add_argument("--protocol", "-p", default="vless", help="指定節點協議 (預設: vless)")
    parser.add_argument("--table", "-t", choices=["ct", "cu", "cm"], help="指定使用的表 (ct/cu/cm)")
    parser.add_argument("--country", help="指定偏好落地國 (JP/HK/SG/US)")
    parser.add_argument("--strategy", choices=["top", "random", "random_top3", "random_top5"], default="top", help="IP 選取策略 (top: 固定最優, random_top3: 前三隨機)")
    parser.add_argument("--daemon", action="store_true", help="啟用常駐守護模式 (背景持續循環執行)")
    parser.add_argument("--interval", type=int, default=1800, help="常駐循環時間間隔，單位秒 (預設: 1800 秒 / 30 分鐘)")
    parser.add_argument("--dry-run", action="store_true", help="模擬執行 (不實際寫入面板)")

    args = parser.parse_args()

    cfg_file = Path(args.config).resolve()
    conf: Dict[str, Any] = {}

    if cfg_file.is_file():
        try:
            conf = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("讀取設定檔失敗: %s", e)

    if args.node is not None:
        nodes = [{
            "id": args.node,
            "protocol": args.protocol,
            "table": args.table or "ct",
            "country": args.country or "",
            "strategy": args.strategy or "top",
            "rank": 1
        }]
    else:
        nodes = conf.get("nodes", [])

    v2_conf = conf.get("v2board", {}) or conf.get("panel", {})
    url = args.url or v2_conf.get("url", "")
    token = args.token or v2_conf.get("token", "") or v2_conf.get("api_token", "")
    admin_email = v2_conf.get("admin_email", "")
    admin_password = v2_conf.get("admin_password", "")
    tables = conf.get("tables", DEFAULT_TABLES)

    if not url or (not token and not (admin_email and admin_password)):
        if not args.dry_run:
            logger.error("缺少 V2Board 連線或認證資訊！請在 config.json 中填入 token，或填入 admin_email 與 admin_password！")
            sys.exit(1)

    if not nodes:
        logger.error("未指定任何要替換的節點 ID！")
        sys.exit(1)

    cache_dir = DEFAULT_CACHE_DIR
    backup_dir = DEFAULT_BACKUP_DIR

    if args.daemon:
        print(f"🕒 啟動常駐守護模式，每隔 {args.interval} 秒 (約 {args.interval // 60} 分鐘) 自動執行一次同步...")
        while True:
            perform_sync(
                v2board_url=url or "http://127.0.0.1",
                v2board_token=token,
                admin_email=admin_email,
                admin_password=admin_password,
                tables=tables,
                nodes_to_update=nodes,
                cache_dir=cache_dir,
                backup_dir=backup_dir,
                dry_run=args.dry_run
            )
            print(f"\n💤 進入休眠，將於 {args.interval} 秒後執行下一次替換 (按 Ctrl+C 結束)...")
            time.sleep(args.interval)
    else:
        success = perform_sync(
            v2board_url=url or "http://127.0.0.1",
            v2board_token=token,
            admin_email=admin_email,
            admin_password=admin_password,
            tables=tables,
            nodes_to_update=nodes,
            cache_dir=cache_dir,
            backup_dir=backup_dir,
            dry_run=args.dry_run
        )
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
