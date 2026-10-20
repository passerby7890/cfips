#!/usr/bin/env bash
# ==============================================================================
# CF-Global-QuadNet-Feed 外部機器消費端同步腳本
# 用途：從 GitHub Raw URL 一鍵拉取經過 101 回源驗證的四網或各國優選 IP
# 選配：可配置 Cloudflare API Token 直接自動更新消費端 DNS A 記錄
# ==============================================================================

set -euo pipefail

# 預設倉庫設定（請修改為您的 GitHub 帳號與倉庫名稱，或透過環境變數傳入）
REPO_OWNER="${REPO_OWNER:-"passerby7890"}"
REPO_NAME="${REPO_NAME:-"cfips"}"
BRANCH="${BRANCH:-"main"}"
RAW_BASE="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${BRANCH}/output"

# 參數解析
CATEGORY="${1:-"ct"}"   # 可選：ct, cu, cm, edu, hk, tw, jp, sg, kr, us, eu, all, json
OUTPUT_FILE="${2:-""}"

print_usage() {
  cat <<EOF
使用方式:
  $0 <分類代碼> [輸出檔案路徑]

支援的四網分類:
  ct   (大陸電信優選)
  cu   (大陸聯通優選)
  cm   (大陸移動優選)
  edu  (大陸教育網優選)

支援的國際與地區分類:
  hk   (香港 HKG)
  tw   (台灣 TPE/KHH)
  jp   (日本 NRT/KIX)
  sg   (新加坡 SIN)
  kr   (韓國 ICN)
  us   (美國 SJC/LAX/SEA)
  eu   (歐洲 FRA/AMS/LHR)

特殊選項:
  json (取得包含完整指標的 best_ips.json)
  all  (拉取所有四網與分國文字清單至目前目錄)

環境變數選配 (若提供則自動更新 Cloudflare DNS A 記錄):
  CF_API_TOKEN="your_token"
  CF_ZONE_ID="your_zone_id"
  CF_RECORD_NAME="ct.yourdomain.com"
EOF
}

if [[ "${CATEGORY}" == "-h" || "${CATEGORY}" == "--help" ]]; then
  print_usage
  exit 0
fi

fetch_target() {
  local target="$1"
  local url=""

  case "${target}" in
    ct|cu|cm|edu)
      url="${RAW_BASE}/carriers/${target}.txt"
      ;;
    hk|tw|jp|sg|kr|us|eu)
      url="${RAW_BASE}/countries/${target}.txt"
      ;;
    json)
      url="${RAW_BASE}/best_ips.json"
      ;;
    *)
      echo "[-] 不支援的分類代碼: ${target}" >&2
      print_usage
      exit 1
      ;;
  esac

  echo "[+] 正在從 GitHub 拉取 ${target} 優選清單..."
  echo "    來源 URL: ${url}"

  local content
  content=$(curl -f -sSL "${url}" || true)

  if [[ -z "${content}" ]]; then
    echo "[-] 拉取失敗或內容為空" >&2
    exit 1
  fi

  if [[ -n "${OUTPUT_FILE}" ]]; then
    echo "${content}" > "${OUTPUT_FILE}"
    echo "[✔] 已成功儲存至: ${OUTPUT_FILE}"
  else
    echo "--- 取得的優選 IP 清單 ---"
    echo "${content}"
    echo "--------------------------"
  fi

  # 若設定了 Cloudflare 認證資訊，自動執行 DNS A 記錄輪詢更新
  if [[ -n "${CF_API_TOKEN:-}" && -n "${CF_ZONE_ID:-}" && -n "${CF_RECORD_NAME:-}" ]]; then
    echo "[+] 檢測到 Cloudflare DNS 配置，開始同步至 ${CF_RECORD_NAME}..."
    local ip_list
    ip_list=$(echo "${content}" | grep -E '^([0-9]{1,3}\.){3}[0-9]{1,3}$' | head -n 3)

    if [[ -z "${ip_list}" ]]; then
      echo "[-] 無合格 IP 可供同步，已略過 DNS 更新" >&2
      return 0
    fi

    # 取得現有 A 記錄
    local existing_records
    existing_records=$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records?type=A&name=${CF_RECORD_NAME}" \
      -H "Authorization: Bearer ${CF_API_TOKEN}" \
      -H "Content-Type: application/json")

    # 刪除既有記錄
    echo "${existing_records}" | grep -o '"id":"[^"]*"' | cut -d'"' -f4 | while read -r rec_id; do
      if [[ -n "${rec_id}" ]]; then
        curl -s -X DELETE "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records/${rec_id}" \
          -H "Authorization: Bearer ${CF_API_TOKEN}" > /dev/null
      fi
    done

    # 新增最新優選 IP
    while read -r ip; do
      if [[ -n "${ip}" ]]; then
        echo "  -> 新增 A 記錄: ${CF_RECORD_NAME} -> ${ip} (TTL: 60, Proxied: false)"
        curl -s -X POST "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records" \
          -H "Authorization: Bearer ${CF_API_TOKEN}" \
          -H "Content-Type: application/json" \
          --data "{\"type\":\"A\",\"name\":\"${CF_RECORD_NAME}\",\"content\":\"${ip}\",\"ttl\":60,\"proxied\":false}" > /dev/null
      fi
    done <<< "${ip_list}"

    echo "[✔] Cloudflare DNS A 記錄更新完成！"
  fi
}

if [[ "${CATEGORY}" == "all" ]]; then
  mkdir -p ./output/carriers ./output/countries
  for c in ct cu cm edu; do
    OUTPUT_FILE="./output/carriers/${c}.txt" fetch_target "${c}"
  done
  for c in hk tw jp sg kr us eu; do
    OUTPUT_FILE="./output/countries/${c}.txt" fetch_target "${c}"
  done
  OUTPUT_FILE="./output/best_ips.json" fetch_target "json"
  echo "[✔] 所有四網與各國優選檔案已成功同步至本地 output/ 目錄！"
else
  fetch_target "${CATEGORY}"
fi
