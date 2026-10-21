# V2Board (Xiao 版) 節點入口純 IP 極簡自動替換手冊

專為 **V2Board (Xiao 版) VLESS 節點** 設計，徹底解決節點手動換 IP 的繁瑣流程。
**只替換入口地址 (`server`)，100% 嚴格原樣保留原本的 Port、UUID、TLS SNI、Host、Path**，確保 CDN / 反代握手連線暢通無阻。

---

## 一、 核心輸入要素 (只需這 4 樣)

1. **面板地址與 Token**：
   - 面板地址：`url`（如 `https://panel.example.com` 或伺服器本機 `http://127.0.0.1`）
   - 管理員憑證：`token`（在瀏覽器 F12 審查任一後台 API 請求的 `Authorization` 或 `auth-data` 即可取得）
2. **三張表來源**：
   - 電信表 (`ct`)：`https://raw.githubusercontent.com/passerby7890/cfips/main/output/ct.txt`
   - 聯通表 (`cu`)：`https://raw.githubusercontent.com/passerby7890/cfips/main/output/cu.txt` (100% 來自 `www.090902.xyz`)
   - 移動表 (`cm`)：`https://raw.githubusercontent.com/passerby7890/cfips/main/output/cm.txt`
3. **替換的節點 ID 與協議**：
   - 節點 ID：如您截圖中的 `7`、`6`
   - 協議：`vless`
   - 綁定哪張表：`ct` / `cu` / `cm`
   - 偏好落地國：如 `JP`（日本）、`HK`（香港）

---

## 二、 快速設定檔 (`config/v2board_sync.json`)

在伺服器上建立 `/opt/v2board_sync/config.json`：
```json
{
  "v2board": {
    "url": "http://127.0.0.1",
    "token": "在此貼上您的管理員_Token"
  },
  "tables": {
    "ct": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/ct.txt",
    "cu": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cu.txt",
    "cm": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cm.txt"
  },
  "nodes": [
    {
      "id": 7,
      "protocol": "vless",
      "table": "ct",
      "country": "JP",
      "rank": 1,
      "comment": "JP 电信专用-日本(3倍)"
    },
    {
      "id": 6,
      "protocol": "vless",
      "table": "cm",
      "country": "HK",
      "rank": 1,
      "comment": "HK 移动专用-香港(3倍)"
    }
  ]
}
```

---

## 三、 使用方式

### 方式 1：一鍵全自動執行（依設定檔批次替換）
```bash
python3 scripts/v2board_node_sync.py
```
> 終端機將依序印出：
> - 節點 7：`www.090902.xyz` ➡️ `162.159.44.27` (#JP)
> - 節點 6：`cf.godns.cc` ➡️ `104.17.149.220` (#HK)

### 方式 2：命令列單次即時換特定節點
```bash
# 只換節點 7 為電信日本最優 IP
python3 scripts/v2board_node_sync.py --node 7 --table ct --country JP

# 只換節點 6 為移動香港最優 IP
python3 scripts/v2board_node_sync.py --node 6 --table cm --country HK

# 模擬測試 (不真正寫入面板)
python3 scripts/v2board_node_sync.py --dry-run
```

---

## 四、 Linux Crontab 定時排程 (無人值守)

在面板伺服器輸入：
```bash
crontab -e
```
加入以下排程（每 30 分鐘自動執行一次，自動將最新優選純 IP 寫入節點）：
```bash
*/30 * * * * /usr/bin/python3 /opt/v2board_sync/v2board_node_sync.py --config /opt/v2board_sync/config.json >> /var/log/v2board_ip_sync.log 2>&1
```
