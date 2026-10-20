#!/usr/bin/env bash
# ==============================================================================
# release_version.sh - 自動更新 CHANGELOG.md, DEV_NOTES.md 並發布版本
# ==============================================================================
set -euo pipefail

VERSION="${1:-v1.1.0}"
TITLE="${2:-fix: 隔離 GitHub 美國 Runner 機房誤判，保留情報源與境內實測國家標籤}"
DESC="${3:-解決 Anycast IP 在 GitHub Actions 美東 IAD 機房測試時全部被覆蓋為 #US 的問題。新增 CF 官方 Anycast 網段辨識與 4 層國家標籤優先級 (境內實測 source_colo > 情報源原生 source_country > 非官方反代 trace_colo > 預設兜底)，並確保 /24 鄰居平移完整繼承母體國家標籤。}"

DATE_STR=$(date +'%Y-%m-%d')

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
### 核心問題
- **問題現象：** GitHub Actions (ubuntu-latest) 位於美東維吉尼亞 Azure 數據中心，連線 Anycast IP 發起 /cdn-cgi/trace 時 Cloudflare 一律就近分配 IAD / SJC 機房。原有的 trace_colo 判定邏輯導致所有 IP 被強制洗成 #US。
- **解決方案：** 
  1. \`src/region_mapper.py\`：導入 \`CF_OFFICIAL_CIDRS\` 官方 Anycast 網段辨識器 \`is_cf_official_anycast()\` 與 4 層優先級 \`resolve_country_tag()\`。
  2. \`src/collector.py\`：支援解析種子檔案與情報源行末 \`#COUNTRY\` 標籤，並萃取 HostMonit / WeTest 探針的境內 \`source_colo\`。
  3. \`src/neighbor_shifter.py\`：確保同 /24 鄰居平移 100% 繼承母體 \`source_country\` 與 \`source_colo\`。
  4. \`src/verifier.py\`：隔離海外 Runner 的 \`trace_colo\`，僅作為通道存活依據，禁止覆蓋原生國家標籤。

EOF

if [ -f "$DEV_NOTES_FILE" ]; then
    tail -n +2 "$DEV_NOTES_FILE" >> "$TMP_DN"
fi
mv "$TMP_DN" "$DEV_NOTES_FILE"
echo "✅ 已更新 $DEV_NOTES_FILE"

echo "🎉 版本 $VERSION 筆記更新完成！"
