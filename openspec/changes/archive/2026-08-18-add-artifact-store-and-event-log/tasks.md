## 1. 專案骨架

- [x] 1.1 建立 `pyproject.toml`（package 名 `myharness`，Python 3.13，dev 相依 `pytest`、`pytest-asyncio`），並以既有的 `.venv` 安裝為 editable
- [x] 1.2 建立套件目錄 `myharness/{__init__.py,artifacts/,events/}` 與 `tests/`
- [x] 1.3 建立 `.gitignore`（`.venv/`、`__pycache__/`、`jobs/`、`.env`）與 `.env.example`（`OPENROUTER_KEY`），並 `git init`
- [x] 1.4 加入 `pytest` 設定與一個 smoke test，確認 `pytest` 可跑

## 2. Artifact store 介面與型別

- [x] 2.1 定義 `ArtifactId` 型別與 `<job_id>/<kind>/<name>` 的解析與組成，含非法 id 的拒絕（規格：全域唯一的 artifact id）
- [x] 2.2 定義 `ArtifactKind`（`blob` / `note`）與 `ArtifactMeta`（`kind`、`bytes`、`est_tokens`、`schema`、`produced_by`、`created_at`）
- [x] 2.3 定義 `GrantSet`（own namespace + 明確授權 id）與其判定方法（規格：Capability-based 讀取授權）
- [x] 2.4 定義結構化錯誤型別：`BlobNotReadable`（含 kind/bytes/schema/建議存取方式）、`TokenBudgetExceeded`（含 est_tokens 與 section 清單）、`NotGranted`
- [x] 2.5 定義 `ArtifactStore` 抽象介面：`put_blob`、`put_note`、`read_note`、`stat`、`list`、`localize`、`compare_and_set_note`

## 3. Artifact store 本地實作

- [x] 3.1 實作 `jobs/<job_id>/{blobs,lanes,traces}` 目錄配置與 job 初始化（規格：後端可替換的儲存介面）
- [x] 3.2 實作 SQLite index：建表、寫入中繼資料、依 id 查詢、依 job 與 kind 列舉（手寫 SQL，無 ORM）
- [x] 3.3 實作 `put_note` / `put_blob`，寫入時計算並記錄 `est_tokens` 與 `bytes`（規格：寫入時記錄索引中繼資料）
- [x] 3.4 實作 `read_note` 的授權檢查，未授權時回傳 `NotGranted` 且不洩漏內容（規格：Capability-based 讀取授權）
- [x] 3.5 實作 `read_note` 對 blob 的拒絕路徑，回傳 schema 與建議存取方式且不讀取任何位元組（規格：Blob 與 Note 的型別二分）
- [x] 3.6 實作讀取前的 `est_tokens` 預檢與 section 分段讀取（規格：讀取前的 token 預檢）
- [x] 3.7 實作 `localize` context manager，本地後端零複製、離開時清理，正常結束與例外路徑皆需清理（規格：Blob 的本地路徑物化）
- [x] 3.8 實作 `compare_and_set_note` 供 lane state 寫入使用（design D6 風險緩解）

## 4. Artifact store 測試

- [x] 4.1 建立可套用於任一實作的合約測試套件 `tests/contract/test_artifact_store.py`
- [x] 4.2 覆蓋規格「Blob 與 Note 的型別二分」的兩個 scenario，含「不讀取任何位元組」的驗證
- [x] 4.3 覆蓋規格「讀取前的 token 預檢」的兩個 scenario，含「不從後端讀取內容」的驗證
- [x] 4.4 覆蓋規格「Capability-based 讀取授權」的三個 scenario
- [x] 4.5 覆蓋規格「Blob 的本地路徑物化」的兩個 scenario，含例外路徑的清理
- [x] 4.6 覆蓋規格「全域唯一的 artifact id」與「寫入時記錄索引中繼資料」的各兩個 scenario
- [x] 4.7 加入靜態檢查測試：非 store 模組不得出現 artifact 路徑拼接（design D6）

## 5. Event log 型別與寫入

- [x] 5.1 定義事件基礎結構與共通欄位 `t` / `seq` / `ts` / `job_id`（規格：事件的共通欄位）
- [x] 5.2 定義涵蓋 job 生命週期的事件型別：`job.start`、`job.finish`、`plan.update`、`ingress`、`proxy.route`、`dispatch.start`、`dispatch.end`、`artifact.read`、`ctx`、`ask.user`、`ask.answer`（規格：涵蓋 job 生命週期的事件型別）
- [x] 5.3 實作 `EventLog.append`，逐行 JSON 落盤並 flush，僅提供附加操作（規格：Append-only 的結構化事件流、寫入的耐久性）
- [x] 5.4 實作序號配置，確保單一 job 內唯一且連續
- [x] 5.5 讀取端對未知事件型別寬容，不因新型別而失敗（design D8 風險緩解）

## 6. Event log 讀取與聚合

- [x] 6.1 實作 `EventLog.read` 逐行解析，並容忍檔案結尾的不完整行（規格：寫入的耐久性）
- [x] 6.2 實作依型別過濾與依 lane 分組加總成本與 token（規格：聚合查詢介面）
- [x] 6.3 實作 context 峰值查詢（規格：Context 用量事件）
- [x] 6.4 實作失敗與降級事件列舉（規格：聚合查詢介面）
- [x] 6.5 實作 `derive_caveats`，從事件流推導超預算 lane、逾時未答的提問、被跳過的 payload、失敗的 dispatch（規格：交付物的 caveats 由事件流推導）

## 7. Event log 測試

- [x] 7.1 覆蓋規格「Append-only 的結構化事件流」與「事件的共通欄位」的各兩個 scenario
- [x] 7.2 覆蓋規格「涵蓋 job 生命週期的事件型別」的兩個 scenario
- [x] 7.3 覆蓋規格「Context 用量事件」與「聚合查詢介面」的各兩個 scenario
- [x] 7.4 覆蓋規格「交付物的 caveats 由事件流推導」的兩個 scenario
- [x] 7.5 覆蓋規格「寫入的耐久性」：寫入後強制中止子行程，驗證可完整讀回且無損毀行
- [x] 7.6 覆蓋規格「事件流可作為回歸斷言的來源」：以合成事件流驗證可判定 context 峰值、重複 dispatch、總成本與最終狀態

## 8. 收尾

- [x] 8.1 全套測試通過，並記錄覆蓋率
- [x] 8.2 撰寫 `myharness/README.md`：兩層的用途、如何新增後端實作、如何新增事件型別
- [x] 8.3 更新根目錄 `DESIGN.md`，將 §7 目錄結構與 §4.7 event log 對齊實際實作
- [x] 8.4 執行 `openspec validate add-artifact-store-and-event-log --strict` 並確認通過
