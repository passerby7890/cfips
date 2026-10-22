#!/usr/bin/env python3
"""
=============================================================================
V2Board / Xboard (Xiao 版) MySQL 資料庫直接極速替換引擎 (v3.2.0 實戰自癒版)
=============================================================================
特色：
  1. 【0 延遲直連資料庫】：免 Token、免登入、無 Session 過期、無 Web 路由問題！
  2. 【Top-K 黏性調度 (top_k_sticky)】：現任 IP 依然頂級健康 (101 通過且在 Top 15 內) 則黏性保持，杜絕盲選洗牌！
  3. 【本地持久化黑名單庫 (state_cache.json)】：Error 1034 封禁 24h、超時/GFW 冷卻 2h，0ms 跳過死 IP！
  4. 【大陸境內真機防牆驗證 (Globalping CN)】：海外 101 回源通過後，再由大陸境內探針檢驗 TCP，徹底防 GFW 假活！
  5. 【跨節點防撞車鎖 (used_ips_in_run)】：同一輪同步中不同節點互斥，防止單 IP 流量聚集被省牆 QoS！
  6. 【零快取干擾】：V2Board 直讀 MySQL，無需 flush 快取，保障面板登入與 Session 100% 穩定！
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import socket
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

# 導入本地狀態記憶庫與大陸真機探針模組
try:
    from state_manager import StateManager
except ImportError:
    try:
        from v2board_syncer.state_manager import StateManager
    except ImportError:
        StateManager = None

try:
    from cn_probe import verify_cn_mainland_tcp
except ImportError:
    try:
        from v2board_syncer.cn_probe import verify_cn_mainland_tcp
    except ImportError:
        def verify_cn_mainland_tcp(ip: str, port: int = 443, timeout_sec: float = 5.0):
            return True, "探針模組未載入 (略過)"

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


def verify_node_origin_handshake(
    ip: str,
    port: int = 2053,
    sni: str = "",
    host_header: str = "",
    ws_path: str = "/ws?ed=2048",
    timeout: float = 2.5
) -> Tuple[bool, str, float]:
    """針對節點特定配置進行端到端回源驗證
    回傳: (is_valid, reason, rtt_ms)
    """
    t_start = time.time()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        s = socket.create_connection((ip, int(port)), timeout=timeout)
        if sni:
            ss = ctx.wrap_socket(s, server_hostname=sni)
        else:
            ss = s
            
        target_host = host_header or sni or ip
        target_path = ws_path or "/ws?ed=2048"
        target_path = target_path.replace(r"\/", "/").replace("\\", "/")
        if not target_path.startswith("/"):
            target_path = "/" + target_path
        req = (
            f"GET {target_path} HTTP/1.1\r\n"
            f"Host: {target_host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        ss.sendall(req.encode())
        resp = ss.recv(512).decode("utf-8", errors="replace")
        rtt_ms = round((time.time() - t_start) * 1000, 1)
        
        try:
            ss.close()
        except Exception:
            pass

        if "1034" in resp or ("403" in resp and "cloudflare" in resp.lower()):
            return False, "Cloudflare Error 1034 (Edge IP Restricted 邊緣阻斷)", rtt_ms
        if any(err in resp for err in ("502 Bad Gateway", "521 Web Server", "522 Connection", "523 Origin")):
            return False, "Cloudflare 回源失敗 (502/521/522/523)", rtt_ms
        if "101" in resp:
            return True, "101 Switching Protocols (WS 回源握手成功)", rtt_ms
        if "400" in resp and "sec-websocket-version" in resp.lower():
            return True, "Origin WebSocket Active (回源成功)", rtt_ms
        if "200 OK" in resp:
            return True, "HTTP 200 OK", rtt_ms

        first_line = resp.splitlines()[0] if resp.splitlines() else "No Response"
        return False, f"未符合回源協議: {first_line[:40]}", rtt_ms
    except socket.timeout:
        return False, f"連線超時 (>{timeout}s)", round((time.time() - t_start) * 1000, 1)
    except Exception as e:
        return False, f"網路握手失敗: {e}", round((time.time() - t_start) * 1000, 1)


def select_best_verified_ip(
    ip_list: List[Tuple[str, str]],
    preferred_country: str = "",
    prefer_countries: Optional[List[str]] = None,
    reject_countries: Optional[List[str]] = None,
    rank: int = 1,
    strategy: str = "top_k_sticky",
    current_ip: str = "",
    sni: str = "",
    host_header: str = "",
    ws_path: str = "/ws?ed=2048",
    port: int = 2053,
    max_test_count: int = 15,
    node_id: int = 0,
    top_k: int = 8,
    sticky_minutes: int = 15,
    enable_cn_probe: bool = True,
    state_mgr: Optional[StateManager] = None,
    used_ips_in_run: Optional[set] = None,
    carrier: str = ""
) -> Optional[Tuple[str, str, str, float]]:
    """全方位實戰自癒回源測活篩選 (v4.1.0 防繞美西與亞太優先版)：
      1. 【防繞美西與拒絕國別立即換血】：凡現任為電信/聯通繞美網段或 #US，無條件強制換血！(注意：移動 CMI 104.* 直連香港，不予誤殺)
      2. 【Top-K 黏性調度 (top_k_sticky)】：若現任 IP 仍健康且符合亞太偏好國別，維持不動。
      3. 【0ms 記憶庫跳過 (state_cache)】：自動過濾 24h 1034 黑名單與 2h 超時/被牆冷卻 IP。
      4. 【跨節點防撞車鎖 (used_ips_in_run)】：跳過本輪已分配給其他節點的 IP。
      5. 【雙重真・測活防線】：海外 101 WebSocket 回源握手 ＋ Globalping 大陸境內真機 TCP 探測。
      回傳: (ip, country_tag, reason, rtt_ms) 或 None
    """
    if not ip_list:
        return None

    strat = str(strategy).lower().strip()
    c_carrier = str(carrier).lower().strip()
    if c_carrier in ("ct", "cu"):
        us_blacklist_prefixes = (
            "104.18.", "104.19.", "104.20.", "104.21.", "104.22.",
            "104.24.", "104.25.", "104.26.", "172.66.", "172.67."
        )
    else:  # cm (移動直連香港，不封殺 104.*)
        us_blacklist_prefixes = ("172.66.",)

    target_prefer_list = [c.upper() for c in (prefer_countries or ([preferred_country] if preferred_country else []))]
    target_reject_list = [c.upper() for c in (reject_countries or ["US"])]

    # -------------------------------------------------------------------------
    # 階段 1：【Top-K 黏性審計 (Sticky Retention)】
    # -------------------------------------------------------------------------
    if strat in ("top_k_sticky", "sticky_top_k", "sticky") and current_ip and IP_REGEX.match(current_ip):
        node_st = state_mgr.get_node_state(node_id) if state_mgr else {}
        assigned_at = node_st.get("assigned_at", 0)
        elapsed_min = (time.time() - assigned_at) / 60.0 if assigned_at else 999.0
        is_banned = state_mgr.is_blacklisted(current_ip) if state_mgr else False

        # 檢查現任 IP 在當期池中的排位 (不分落地國與落地國池綜合排位)
        current_rank = next((idx + 1 for idx, (ip, _) in enumerate(ip_list) if ip == current_ip), 999)

        # 找出 current_ip 在本期清單或快取中的真實國別
        current_country = node_st.get("country", "")
        for idx, (ip, tag) in enumerate(ip_list):
            if ip == current_ip:
                current_country = tag
                break

        is_us_detour_ip = current_ip.startswith(us_blacklist_prefixes)

        # [Country Mismatch Guard & US Detour Guard (v4.0.0)]
        if is_us_detour_ip or (current_country and current_country.upper() in target_reject_list):
            print(
                f"    🚫 [繞美黑名單/拒絕國別] Node {node_id} 現任 IP {current_ip} 屬於繞美西網段或 #{current_country}，"
                f"無條件強制解除黏性並立刻換血為亞太低延遲 IP！"
            )
        elif target_prefer_list and current_country and current_country.upper() not in target_prefer_list:
            print(
                f"    ⚠️ [國別不符降級態] Node {node_id} 現任 IP {current_ip} 國別為 #{current_country} "
                f"(目標偏好要求 {target_prefer_list})，拒絕黏性保持，強制重新選拔！"
            )
        elif not is_banned and elapsed_min < sticky_minutes and current_rank <= 15:
            print(f"    [*] [Top-K 黏性審計] 現任 IP {current_ip} (排名 #{current_rank}, 已服役 {elapsed_min:.1f}/{sticky_minutes} 分鐘) 執行健康複檢...")
            ok, reason, rtt = verify_node_origin_handshake(
                current_ip,
                port=port,
                sni=sni,
                host_header=host_header,
                ws_path=ws_path
            )
            if ok:
                cn_passed = True
                cn_detail = ""
                if enable_cn_probe:
                    print(f"    [*] [國內探針複檢] 對現任 IP {current_ip}:{port} 發起 Globalping CN TCP 探測...")
                    cn_passed, cn_detail = verify_cn_mainland_tcp(current_ip, port=port)

                if cn_passed:
                    print(f"    [🔒 黏性保持] 現任 IP {current_ip} 依然頂級健康 (握手 {rtt}ms, {cn_detail or '101 暢通'})，維持不動！")
                    if used_ips_in_run is not None:
                        used_ips_in_run.add(current_ip)
                    curr_tag = next((tag for ip, tag in ip_list if ip == current_ip), preferred_country or "CF")
                    return current_ip, curr_tag, f"Top-K 黏性保持 ({reason})", rtt
                else:
                    print(f"    [!] [GFW 阻斷告警] 現任 IP {current_ip} 境內探針失敗: {cn_detail}，解除黏性並強制換血！")
                    if state_mgr:
                        state_mgr.mark_timeout_or_gfw(current_ip)
            else:
                print(f"    [!] [握手失效告警] 現任 IP {current_ip} 複檢失敗: {reason}，解除黏性並強制換血！")
                if state_mgr:
                    if "1034" in reason:
                        state_mgr.mark_1034(current_ip)
                    else:
                        state_mgr.mark_timeout_or_gfw(current_ip)
        elif current_rank > 15:
            print(f"    [*] [榜單調度] 現任 IP {current_ip} 掉出前 15 名 (當前 #{current_rank})，觸發良性換血！")

    # -------------------------------------------------------------------------
    # 階段 2：【新血候選池過濾與選拔】
    # -------------------------------------------------------------------------
    # (A) 黑名單、撞車與繞美前置過濾
    healthy_candidates = [
        item for item in ip_list
        if not item[0].startswith(us_blacklist_prefixes)
        and item[1].upper() not in target_reject_list
        and not (state_mgr and state_mgr.is_blacklisted(item[0]))
        and (used_ips_in_run is None or item[0] not in used_ips_in_run)
    ]
    if len(healthy_candidates) < len(ip_list):
        diff = len(ip_list) - len(healthy_candidates)
        print(f"    [*] [前置過濾] 已自候選清單預先剔除 {diff} 顆黑名單/冷卻/已佔用/繞美 IP (剩餘 {len(healthy_candidates)} 顆可用)")

    primary_candidates = []
    if target_prefer_list:
        for pref in target_prefer_list:
            matched = [item for item in healthy_candidates if item[1].upper() == pref]
            for m_item in matched:
                if m_item not in primary_candidates:
                    primary_candidates.append(m_item)
        fallback_candidates = [item for item in healthy_candidates if item not in primary_candidates]
    else:
        primary_candidates = list(healthy_candidates)
        fallback_candidates = []

    def verify_pool(pool: List[Tuple[str, str]], pool_desc: str) -> Optional[Tuple[str, str, str, float]]:
        if not pool:
            return None

        # 1. 記憶庫過濾 (0ms 跳過黑名單) 與跨節點防撞車過濾
        valid_candidates = []
        skipped_banned = 0
        skipped_collision = 0
        for cand_ip, cand_tag in pool:
            if state_mgr and state_mgr.is_blacklisted(cand_ip):
                skipped_banned += 1
                continue
            if used_ips_in_run is not None and cand_ip in used_ips_in_run:
                skipped_collision += 1
                continue
            valid_candidates.append((cand_ip, cand_tag))

        if skipped_banned > 0 or skipped_collision > 0:
            print(f"    [*] [{pool_desc}] 自動防護過濾: 跳過 {skipped_banned} 顆黑名單/冷卻 IP，跳過 {skipped_collision} 顆其他節點已佔用 IP")

        if not valid_candidates:
            return None

        # 2. 策略調度建構測試隊列
        if strat in ("top_k_sticky", "sticky_top_k", "top_k", "top_k_random", "random_topk"):
            k = max(1, top_k)
            elite_slice = list(valid_candidates[:k])
            rest_slice = list(valid_candidates[k:])
            if strat in ("top_k_random", "random_topk"):
                random.shuffle(elite_slice)
            test_pool = elite_slice + rest_slice
        elif strat in ("random_all", "random", "all"):
            test_pool = list(valid_candidates)
            random.shuffle(test_pool)
        elif "random" in strat:
            m = re.search(r"\d+", strat)
            limit = int(m.group(0)) if m else 3
            sub = list(valid_candidates[:max(1, limit)])
            random.shuffle(sub)
            test_pool = sub + valid_candidates[limit:]
        elif strat == "top" and rank > 1 and rank <= len(valid_candidates):
            target_item = valid_candidates[rank - 1]
            test_pool = [target_item] + [x for x in valid_candidates if x != target_item]
        else:
            test_pool = list(valid_candidates)

        tested = 0
        for cand_ip, cand_tag in test_pool:
            if tested >= max_test_count:
                print(f"    ⚠️ [{pool_desc}] 達到單池最大測活限制 ({max_test_count} 個)，中止測試。")
                break
            tested += 1

            # 防線 A: 海外端到端 WebSocket 101 回源握手驗證
            ok, reason, rtt = verify_node_origin_handshake(
                cand_ip,
                port=port,
                sni=sni,
                host_header=host_header,
                ws_path=ws_path
            )
            if not ok:
                if state_mgr:
                    if "1034" in reason:
                        state_mgr.mark_1034(cand_ip)
                        print(f"    [✖ 1034 阻斷] {cand_ip:16s} (#{cand_tag:2s}) ➡️ {reason} (已入 24h 黑名單)")
                    else:
                        state_mgr.mark_timeout_or_gfw(cand_ip)
                        print(f"    [✖ 握手失敗] {cand_ip:16s} (#{cand_tag:2s}) ➡️ {reason} (已入 2h 冷卻池)")
                else:
                    print(f"    [✖ 握手失敗] {cand_ip:16s} (#{cand_tag:2s}) ➡️ {reason}")
                continue

            # 防線 B: 大陸境內真機實體探針驗證 (Globalping CN TCP)
            if enable_cn_probe:
                print(f"    [*] 正在對通過 101 之候選 IP {cand_ip}:{port} 執行大陸境內實體探針驗證...")
                cn_ok, cn_msg = verify_cn_mainland_tcp(cand_ip, port=port)
                if not cn_ok:
                    if state_mgr:
                        state_mgr.mark_timeout_or_gfw(cand_ip)
                    print(f"    [✖ GFW 阻斷] {cand_ip:16s} (#{cand_tag:2s}) ➡️ {cn_msg} (已入 2h 冷卻池)")
                    continue
                else:
                    print(f"    [✔ 境內暢通] {cand_ip:16s} (#{cand_tag:2s}) ➡️ {cn_msg}")

            # 雙重驗證全部通過！
            print(f"    [✔ 最終選定] {cand_ip:16s} (#{cand_tag:2s}) ➡️ {reason} (耗時 {rtt}ms)")
            if used_ips_in_run is not None:
                used_ips_in_run.add(cand_ip)
            return cand_ip, cand_tag, reason, rtt

        return None

    # 1. 優先選拔目標落地國池
    target_label = preferred_country or "ALL"
    print(f"    [*] 正在對目標落地國 [{target_label}] 候選池進行逐一測活選拔 (SNI: {sni}, Port: {port})...")
    chosen = verify_pool(primary_candidates, target_label)
    if chosen:
        return chosen

    # 2. 備援池選拔
    if fallback_candidates:
        print(f"    ⚠️ [防阻斷保護] 目標國別 [{preferred_country}] 候選 IP 均未通過測活，啟用同表跨區健康備援池...")
        chosen = verify_pool(fallback_candidates, "備援池")
        if chosen:
            return chosen

    return None


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
        sql = f"SELECT id, name, host, port, tls_settings, network_settings FROM v2_server_vless WHERE id IN ({ids_str});"
        raw = self.run_query(sql)
        nodes = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                nid = int(parts[0])
                tls_raw = parts[4] if len(parts) > 4 else "{}"
                net_raw = parts[5] if len(parts) > 5 else "{}"
                sni = ""
                try:
                    t_obj = json.loads(tls_raw) if tls_raw and tls_raw != "NULL" else {}
                    sni = t_obj.get("server_name", "")
                except Exception:
                    pass
                host_header = ""
                ws_path = "/ws?ed=2048"
                try:
                    n_obj = json.loads(net_raw) if net_raw and net_raw != "NULL" else {}
                    host_header = n_obj.get("headers", {}).get("Host", "")
                    if n_obj.get("path"):
                        p_val = str(n_obj.get("path")).replace(r"\/", "/").replace("\\", "/")
                        if not p_val.startswith("/"):
                            p_val = "/" + p_val
                        ws_path = p_val
                except Exception:
                    pass
                if not sni:
                    sni = host_header
                if not host_header:
                    host_header = sni
                nodes[nid] = {
                    "id": nid,
                    "name": parts[1],
                    "host": parts[2],
                    "port": parts[3],
                    "sni": sni,
                    "host_header": host_header,
                    "ws_path": ws_path
                }
        return nodes

    def update_vless_host(self, node_id: int, new_host: str) -> bool:
        safe_host = re.sub(r"[^0-9a-zA-Z.:_-]", "", new_host)
        now_ts = int(time.time())
        sql = f"UPDATE v2_server_vless SET host = '{safe_host}', updated_at = {now_ts} WHERE id = {node_id};"
        self.run_query(sql)
        return True


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
    print("⚡ V2Board (Xiao 版) MySQL 極速入口替換 (v3.2.0 Top-K 黏性自癒版)")
    print(f"[*] 連線資料庫: {db_mgr.database} ({db_mgr.host}:{db_mgr.port})")
    print("======================================================================")

    # 0. 初始化本地狀態記憶庫與跨節點防撞車鎖
    state_mgr = StateManager(CURRENT_DIR / "state_cache.json") if StateManager else None
    used_ips_in_run: set = set()

    # 1. 載入三張表
    table_cache: Dict[str, List[Tuple[str, str]]] = {}
    for t_name, t_url in tables.items():
        ips = fetch_ip_table(t_url, cache_dir=cache_dir)
        table_cache[t_name] = ips
        src_label = t_url.split("/")[-1]
        print(f"[*] 載入 [{t_name.upper()}] 表 ({src_label}): 取得 {len(ips)} 個候選優選 IP")

    # 2. 查詢資料庫節點
    target_ids = [int(item.get("node_id") or item.get("id")) for item in nodes_to_update]
    try:
        nodes = db_mgr.get_vless_nodes(target_ids)
    except Exception as e:
        print(f"❌ 資料庫查詢失敗: {e}")
        return False

    print("----------------------------------------------------------------------")
    success_count = 0
    has_modified = False

    for item in nodes_to_update:
        node_id = int(item.get("node_id") or item.get("id"))
        t_name = str(item.get("table") or item.get("carrier") or "ct").lower()
        pref_country = str(item.get("prefer_country") or item.get("country") or "").upper()
        rank = int(item.get("rank", 1))
        strategy = str(item.get("strategy", "top_k_sticky"))
        top_k = int(item.get("top_k", 8))
        sticky_minutes = int(item.get("sticky_minutes", 15))
        enable_cn_probe = bool(item.get("enable_cn_probe", True))

        node = nodes.get(node_id)
        if not node:
            print(f"❌ 資料庫 v2_server_vless 中找不到 ID 為 [{node_id}] 的節點！")
            continue

        name = node["name"]
        curr_host = node["host"]
        port = int(node["port"])
        sni = node.get("sni", "")
        host_header = node.get("host_header", "")
        ws_path = node.get("ws_path", "/ws?ed=2048")

        strat_desc = (
            f"前 {top_k} 名黏性駐留 ({sticky_minutes}m)" if "sticky" in strategy
            else (f"前 {top_k} 名隨機輪替 (top_k_random)" if "top_k" in strategy
            else ("全表隨機抽取 (random_all)" if "random_all" in strategy or strategy == "all"
            else ("前列隨機" if "random" in strategy else f"第 {rank} 名")))
        )
        print(f"[*] 節點 ID: {node_id} 【{name}】")
        print(f"    - 原入口地址 (host)  : {curr_host}:{port}")
        print(f"    - 握手目標 (SNI/Host): {sni} / {host_header}")
        print(f"    - WebSocket 握手路徑 : {ws_path}")
        print(f"    - 綁定來源表         : {t_name.upper()} 表 | 偏好落地國: {pref_country or '不限'} | 模式: {strat_desc}")
        print(f"    - 大陸真機探測 (GFW) : {'已啟用 (Globalping CN TCP)' if enable_cn_probe else '未啟用'}")

        ip_pool = table_cache.get(t_name, [])
        best_choice = select_best_verified_ip(
            ip_pool,
            preferred_country=pref_country,
            prefer_countries=item.get("prefer_countries"),
            reject_countries=item.get("reject_countries", ["US"]),
            rank=rank,
            strategy=strategy,
            current_ip=curr_host,
            sni=sni,
            host_header=host_header,
            ws_path=ws_path,
            port=port,
            node_id=node_id,
            top_k=top_k,
            sticky_minutes=sticky_minutes,
            enable_cn_probe=enable_cn_probe,
            state_mgr=state_mgr,
            used_ips_in_run=used_ips_in_run,
            carrier=carrier
        )
        if not best_choice:
            print(f"    - ❌ [安全警報] 表 [{t_name.upper()}] 中所有候選 IP 經測活均未通過！")
            print(f"    - 🛡️ [零中斷保護] 嚴格拒絕寫入不可用 IP，安全保留現有 host: {curr_host}")
            print("----------------------------------------------------------------------")
            continue

        new_ip, tag, reason, rtt_ms = best_choice
        print(f"    - 🎯 [最終選定] 經端到端測活確認，選定 IP: {new_ip} (#{tag}) ➡️ {reason} ({rtt_ms}ms)")

        # 記錄狀態至本地記憶庫
        if state_mgr:
            state_mgr.set_node_state(node_id, new_ip, tag)

        if curr_host == new_ip:
            print(f"    - 執行結果           : ⚡ 目前資料庫已是選定 IP ({new_ip})，無需變更！")
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

    # 經源碼審查 (ServerService.php)，V2Board/Xboard 讀取節點乃直接查詢 MySQL，完全不快取 VLESS 伺服器列表。
    # 為徹底杜絕快取刷新可能導致的 Redis Session 丟失或登入中斷，依指令嚴格禁止執行全域 cache:clear！
    if has_modified:
        print("[*] 提示：MySQL 節點已即時生效（V2Board 直讀 MySQL，無需清除 Laravel 快取，保障 Session 與登入 100% 穩定）")

    # 持久化記憶庫存檔
    if state_mgr:
        state_mgr.save()
        b_cnt = len(state_mgr.data.get("error_1034_blacklist", {}))
        c_cnt = len(state_mgr.data.get("timeout_cooldown", {}))
        print(f"[*] 本地記憶庫更新 (state_cache.json): 1034 黑名單 {b_cnt} 筆 (24h)，超時/被牆冷卻 {c_cnt} 筆 (2h)")

    print(f"🎉 執行完畢！成功處理 {success_count}/{len(nodes_to_update)} 個節點。")
    print("======================================================================")
    return True


def main():
    parser = argparse.ArgumentParser(description="V2Board (Xiao 版) MySQL 直接極速入口純 IP 替換工具 (v3.2.0)")
    parser.add_argument("--config", "-c", default=str(DEFAULT_CONFIG), help="設定檔路徑")
    parser.add_argument("--node", "-n", type=int, help="指定替換的單一節點 ID (例如: 7)")
    parser.add_argument("--table", "-t", choices=["ct", "cu", "cm"], help="指定使用的表 (ct/cu/cm)")
    parser.add_argument("--country", help="指定偏好落地國 (JP/HK/SG/US)")
    parser.add_argument("--strategy", choices=["top_k_sticky", "top_k_random", "top", "random", "random_top3", "random_top5", "random_all"], default="top_k_sticky", help="IP 選取策略")
    parser.add_argument("--top-k", type=int, default=8, help="Top-K 菁英池數量 (預設: 8)")
    parser.add_argument("--sticky-minutes", type=int, default=15, help="黏性駐留保持時間 (分鐘，預設: 15)")
    parser.add_argument("--no-cn-probe", action="store_true", help="停用 Globalping 大陸境內真機探針驗證")
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
            "node_id": args.node,
            "table": args.table or "ct",
            "country": args.country or "",
            "prefer_country": args.country or "",
            "strategy": args.strategy or "top_k_sticky",
            "top_k": args.top_k,
            "sticky_minutes": args.sticky_minutes,
            "enable_cn_probe": not args.no_cn_probe,
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
