## 1. 沙箱

- [x] 1.1 `myharness/lanes/tabular/sandbox.py`：以 spike #10 驗證過的順序建連線
      —— 先 ingest，再 `enable_external_access=false`、關閉三項擴充套件設定，
      最後 `lock_configuration=true`（design D3）
- [x] 1.2 逃逸測試：未授權讀取、glob、ATTACH、COPY 寫出、重開外部存取、
      解鎖組態、INSTALL/LOAD、http（規格：查詢引擎在執行使用者 SQL 期間與外界隔離）
- [x] 1.3 證明圍籬是哪一道：拿掉 `enable_external_access=false` 全破；
      `lock_configuration` 額外釘住 autoload。**修正**：原先寫「兩道各缺一道
      都不成立」是錯的 —— 圍籬只有一道，鎖是縱深防禦（design D3）
- [x] 1.4 `guard_sql()`：`extract_statements` 要求恰好一句、型別 SELECT
      （規格：查詢僅接受單一唯讀敘述）
- [x] 1.5 多敘述、非 SELECT、空字串、無法解析的 SQL 各自的拒絕測試
- [x] 1.6 逾時：查詢跑在 thread、timer 打 `interrupt()`、回逾時而非例外
      （規格：資料量與執行時間有上限且拒絕時說明原因）

## 2. 綁定與 ingest

- [x] 2.1 `bind_name()`：artifact 名稱末段消毒為合法表名，碰撞加尾碼（design D2）
- [x] 2.2 表名綁定的測試：CJK 名稱、含連字號、大小寫、兩個不同 artifact 同末段
- [x] 2.3 `ingest()`：依副檔名/schema 選 `read_csv_auto` / `read_parquet` /
      `read_json_auto`，無法辨識時以明確訊息拒絕
- [x] 2.4 byte 上限檢查在 ingest 之前、由 index 決定，不開檔
      （規格：資料量與執行時間有上限且拒絕時說明原因）
- [x] 2.5 上限訊息說明「這是沙箱的形狀不是效能調校」（design D3）

## 3. 結果整形

- [x] 3.1 `myharness/lanes/tabular/render.py`：結果表以 CJK 寬度對齊
      （沿用 `monitor/render.py` 的寬度函式，不重寫）
- [x] 3.2 列數閘：取 limit+1 列判斷截斷，不為報總數跑完全表（design D4）
- [x] 3.3 字元閘：即使列數在限內仍 clamp，並明示裁切
      （規格：查詢結果受列數與字元數兩道上限約束）
- [x] 3.4 兩道閘各自獨立生效的測試：多列短內容、單列超長欄位
- [x] 3.5 NULL、極長字串、二進位欄位的表述

## 4. Lane 工具

- [x] 4.1 `inspect_blob(artifact)`：欄位、型別、列數、樣本列、綁定表名
      （規格：Worker 能在寫查詢前取得資料結構）
- [x] 4.2 `duckdb_query(artifacts, sql, into?)`：授權檢查在 id 上，回應必帶表名
      （規格：Lane worker 能對被授權的表格資料執行查詢）
- [x] 4.3 `into`：結果寫成 lane namespace 底下的新 blob，回應不含資料列
      （規格：大量結果以新 artifact 交付而非進入 context）
- [x] 4.4 產出的 artifact 可被同一條 lane 再查詢的測試（授權來自 own namespace，
      不是新的授權來源）
- [x] 4.5 失敗一律回 `_err` 文字，並附當次可用表名
      （規格：查詢失敗以可據以行動的訊息回覆）
- [x] 4.6 未授權 artifact 在讀取任何內容前就被拒的測試
- [x] 4.7 兩個工具都標為非唯讀 —— **理由不是寫入而是記憶體**：每次呼叫都會把
      整份 blob ingest 進記憶體，同 turn 併發等於乘上併發數（spike #1）。
      另加 process 層的 ingest semaphore，因為多條 lane 跑在同一個 event loop

## 5. 修 `localize_blob` 的生命週期

- [x] 5.1 `WorkerToolbox` 持有 `AsyncExitStack`，localize 綁在 worker 執行上
      （design D8，規格：本地化路徑在 worker 執行期間保持有效）
- [x] 5.2 worker 結束時關閉 exit stack，包含執行以例外結束的情況
- [x] 5.3 以一個「離開區塊即刪檔」的假後端寫測試，證明舊寫法會失敗、新寫法不會

## 6. 接線與文件

- [x] 6.1 `duckdb>=1.5` 加入 `pyproject.toml` 必要相依
- [x] 6.2 `myharness/artifacts/local.py` 的 `BLOB_ACCESS_HINT` 與實際工具簽名對齊
- [x] 6.3 `myharness/goldens.py` 的 `ANALYST_TOOLS` 與 `myharness/lanes/driver.py`
      加入新工具
- [x] 6.4 `charters/tabular-analyst.md`：把「以工具處理」改成具體的工作流程
      （先 `inspect_blob` 再 `duckdb_query`，大結果用 `into`）
- [x] 6.5 `myharness/lanes/README.md` 與 `myharness/orchestrator/README.md`
      移除「沒有任何工具能處理 blob」的說明
- [x] 6.6 `myharness/lanes/tabular/README.md`：沙箱為何長這樣、兩道閘、逃生口
- [x] 6.7 `DESIGN.md` §9 把「沒有資料處理工具」從開放問題移出

## 7. 端到端

- [x] 7.1 離線測試：一份真實 CSV 走完 inspect → query → into → 再 query
- [x] 7.2 golden job 的**問題**換成需要實際計算才答得出來的（資料不動：
      2,940 列本來就有結構，換掉會作廢第五次真實跑的事件 fixture）
- [x] 7.3 golden 斷言新增：交付內容含實際數字，且該數字與直接查詢的結果相符
- [x] 7.4 live 跑一次 golden job，記錄結果與成本到 `spikes/RESULTS.md`
