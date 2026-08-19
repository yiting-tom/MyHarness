## Why

這個 harness 存在的理由是分析資料，而它目前**沒有任何工具能處理一份 blob**。

第五次 golden job 通過了全部九項紀律斷言，然後交付了一份說「我讀不到資料」的報告。
Lane 成功拿到那份 138KB CSV 的本地路徑與 schema，接著無事可做 —— 它手上只有
`read_note` / `write_finding` / `update_state` / `localize_blob` 四個儲存工具。

`charters/tabular-analyst.md` 白紙黑字寫著「大型資料一律用 `localize_blob`
取得路徑後**以工具處理**」。那個工具從來沒被建出來。charter 承諾了不存在的能力，
`myharness/artifacts/local.py` 的 `BLOB_ACCESS_HINT` 甚至已經在錯誤訊息裡
向 worker 推薦 `duckdb_query(artifact, sql)` —— 一個呼叫了會說函式不存在的工具。

紀律層全部就緒（授權、handle 兩道閘、失敗為值、資料流投影），缺的是被這些紀律
保護的那個東西本身。

## What Changes

- 新增 `duckdb_query` lane 工具：對被授權的 blob 下 SQL，結果受兩道閘約束
  （列數 + 字元數），大結果以 `into` 寫回成新 blob 而非灌進 context。
- 新增 `inspect_blob` lane 工具：欄位、型別、列數、少量樣本 —— worker 要先看得到
  schema 才寫得出 SQL。
- SQL **不含任何路徑**。worker 指名 artifact，harness 綁定表名，授權檢查一如既往
  發生在 id 上。
- DuckDB 以 spike #10 驗證過的組態封閉：ingest 後 `enable_external_access=false`
  + `lock_configuration=true`，單一 statement 且限 SELECT，牆鐘上限以 interrupt 執行。
- 修掉 `localize_blob` 的 context manager 洩漏：目前在 `async with` 內就 return，
  本地後端僥倖沒事，物件儲存後端會把路徑交出去之後立刻刪檔。
- blob 有 byte 上限（ingest 進記憶體是唯一站得住的沙箱形狀 —— 見 spike #10 的
  `allowed_paths` 負面結果），超過則拒絕並說明。
- `duckdb` 進入必要相依。

## Impact

- Specs: `lane-worker`（新增資料處理能力的需求）
- Code: `myharness/lanes/tools.py`、新增 `myharness/lanes/tabular/`、
  `myharness/artifacts/local.py`（`BLOB_ACCESS_HINT` 與實際工具對齊）、
  `charters/tabular-analyst.md`、`myharness/goldens.py`（lane 工具清單）
- Deps: `duckdb>=1.5`
- 不動：授權模型、handle contract、事件型別、orchestrator 工具面
