# 更新紀錄 (Changelog)

## [v1.1.0] - 2026-10-20
### fix: 隔離 GitHub 美國 Runner 機房誤判，保留情報源與境內實測國家標籤
- 解決 Anycast IP 在 GitHub Actions 美東 IAD 機房測試時全部被覆蓋為 #US 的問題。新增 CF 官方 Anycast 網段辨識與 4 層國家標籤優先級 (境內實測 source_colo > 情報源原生 source_country > 非官方反代 trace_colo > 預設兜底)，並確保 /24 鄰居平移完整繼承母體國家標籤。

