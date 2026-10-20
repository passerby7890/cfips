#!/usr/bin/env bash
# ==============================================================================
# release_version.sh - 自動更新 CHANGELOG.md, DEV_NOTES.md 並發布版本
# ==============================================================================
set -euo pipefail

VERSION="${1:-v1.1.0}"
TITLE="${2:-fix: 隔離 GitHub 美國 Runner 機房誤判，保留情報源與境內實測國家標籤}"
DESC="${3:-解決 Anycast IP 在 GitHub Actions 美東 IAD 機房測試時全部被覆蓋為 #US 的問題。新增 CF 官方 Anycast 網段辨識與 4 層國家標籤優先級 (境內實測 source_colo > 情報源原生 source_country > 非官方反代 trace_colo > 預設兜底)，並確保 /24 鄰居平移完整繼承母體國家標籤。}"

DATE_STR=$(date +'%Y-%m-%d')

# 0. 更新 VERSION
echo "$VERSION" > VERSION
echo "✅ 已更新 VERSION -> $VERSION"

# 1. 更新 CHANGELOG.md
CHANGELOG_FILE="CHANGELOG.md"
TMP_CL=$(mktemp)
cat <<EOF > "$TMP_CL"
# 更新紀錄 (Changelog)

## [$VERSION] - $DATE_STR
### $TITLE
- $DESC

EOF

if [ -f "$CHANGELOG_FILE" ]; then
    # 跳過原檔的第一行標題，附加其餘內容
    tail -n +2 "$CHANGELOG_FILE" >> "$TMP_CL"
fi
mv "$TMP_CL" "$CHANGELOG_FILE"
echo "✅ 已更新 $CHANGELOG_FILE"

# 2. 更新 DEV_NOTES.md
DEV_NOTES_FILE="DEV_NOTES.md"
TMP_DN=$(mktemp)
cat <<EOF > "$TMP_DN"
# 開發筆記與架構演化歷程 (Development Notes)

## [$VERSION] 演化實錄 ($DATE_STR)
### 核心決策與架構突破
- **戰略裁決：** 徹底廢除大海撈針盲掃 2,000+ 靜態 IP 與猜測公式，改採大陸境內 24H 實時監測篩選之「三網黃金 CNAME 域名池 (\`data/cname_pool.json\`)」。
- **技術實裝：**
  1. \`data/cname_pool.json\`：收錄電信 (\`ct\`)、聯通 (\`cu\`)、移動 (\`cm\`) 黃金優選 CNAME 網域，並定義北上廣三網 ECS 錨點 IP。
  2. \`src/collector.py\`：多路併發 DoH (Google / AliDNS / DNSPod) 結合三網 ECS 子網解析，配合原生 DNS 解析兜底，精準榨出各運營商分線優選 IP。
  3. \`src/verifier.py\`：實裝源站 \`:2053/ws\` 真實 WebSocket \`101 Switching Protocols\` 握手測活，從 \`CF-RAY\` 單連線直萃機房代碼，拋棄死節點與被牆阻斷 IP。
  4. \`main.py\`：嚴格按照「CNAME 原生 IP 優先 > 境內延遲由低到高 > 評分由高到低」排序，原生不足時由已測活同 /24 網段平移補齊至 50 筆。
  5. \`scripts/self_test_audit.py\`：紅隊 4 大鐵律嚴格驗收，確保 100% 格式合規與 101 握手全通。

EOF

if [ -f "$DEV_NOTES_FILE" ]; then
    tail -n +2 "$DEV_NOTES_FILE" >> "$TMP_DN"
fi
mv "$TMP_DN" "$DEV_NOTES_FILE"
echo "✅ 已更新 $DEV_NOTES_FILE"

echo "🎉 版本 $VERSION 筆記更新完成！"
