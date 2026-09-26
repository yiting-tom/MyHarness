## 1. 重建
- [x] 1.1 `content.writes_in(rows)`：從逐輪紀錄取出成功的 `write_finding`/`update_state`（依順序配對結果，`ERROR` 不算）
- [x] 1.2 `content.line_diff(old, new)`：新增/刪除行，未變動的長段收合
- [x] 1.3 `content.artifact_view(root, job_id, id)`：目前內容、版本、差異、不完整的原因
- [x] 1.4 逐輪紀錄解析依（路徑、大小、mtime）快取

## 2. 伺服器
- [x] 2.1 `GET /artifact?id=`：`ArtifactId.parse` + 必須屬於本 job，否則拒絕
- [x] 2.2 `/state` 帶 `writes`

## 3. 頁面
- [x] 3.1 資料與產出節點可點、可鍵盤操作
- [x] 3.2 側欄：內容 / 變更切換、版本切換、+/− 差異（符號＋顏色）
- [x] 3.3 「改寫 ×N」標記；handle 沒回報的產出補畫

## 4. 收尾
- [x] 4.1 測試、ruff、mypy --strict、openspec validate
- [x] 4.2 用 golden #26 實際點開看
