## Why

MyHarness 的核心不變式是「原始資料不可能進入 orchestrator 的 context」，而這個不變式
必須由**構造**保證，不能靠 prompt 祈禱。保證它的兩個機制都在 LLM 之外：artifact store
（資料放哪、誰能讀、讀多少）與 event log（實際發生了什麼、花了多少 context）。

這兩者是整個 harness 唯一不含 LLM、可以完整單元測試的部分，也是其他所有元件的依賴
（`dispatch` 的授權檢查、lane worker 的 state 讀寫、golden job 的回歸斷言、成本報表、
未來的 TUI 與 OpenTelemetry 都是它們的投影）。先把地基做對，後面的 LLM 元件才有東西可靠。

現在做的另一個理由：未來要從本地 FS+SQLite 換到 MinIO + MariaDB/Oracle。若第一天不把
存取抽象成介面、不把 blob 的本地化包成 context manager，之後要改就得重寫每一個 lane 工具。

## What Changes

- 新增 `artifact-store` capability：blob / note 二分的儲存層，含 capability-based
  授權、`est_tokens` 事前預檢、以及 `localize()` 本地化 context manager。
- 新增 `event-log` capability：append-only 的 JSONL 事件流，涵蓋 job 生命週期、
  dispatch、proxy 路由、context 用量、成本，並提供查詢與聚合的讀取介面。
- 兩者皆定義為抽象介面 + 本地實作（FS + SQLite），使 MinIO / MariaDB / Oracle
  後端日後可替換而不動呼叫端。
- 建立 `myharness/` Python package 骨架與測試基礎設施。
- 不含任何 LLM 呼叫、不含 orchestrator / proxy / lane worker（後續 change）。

## Capabilities

### New Capabilities
- `artifact-store`: job 範圍的資料儲存層。區分不可讀入 context 的 blob 與可讀入的 note；
  以 dispatch 授權清單限制 lane worker 的讀取範圍；以 `est_tokens` 在讀取前拒絕過大的內容；
  提供 blob 的本地路徑物化以相容既有檔案導向工具。
- `event-log`: append-only 的結構化事件流，記錄 job 內每一次 dispatch、proxy 路由決策、
  artifact 存取、context 用量與成本，作為除錯、成本報表與回歸斷言的唯一事實來源。

### Modified Capabilities
（無 —— 這是專案的第一個 change。）

## Impact

- **新增程式碼**：`myharness/artifacts/`（介面、本地實作、index schema）、
  `myharness/events/`（事件型別、writer、reader）、`tests/`。
- **相依**：Python 3.13、標準庫 `sqlite3` / `json` / `pathlib`；測試用 `pytest`。
  刻意不引入 ORM，以保持 SQL 明確、便於日後換 MariaDB/Oracle。
- **磁碟配置**：定義 `jobs/<job_id>/` 目錄結構（`blobs/`、`lanes/`、`traces/`、
  `events.jsonl`、`index.sqlite`），後續所有元件都依賴此配置。
- **對後續 change 的約束**：`dispatch` 的 `inputs` 參數即授權清單，其語意由本 change
  的授權模型決定；lane worker 的 `state.md` 讀寫、`peek` 的預算扣減也都建立在此之上。
- **不影響**：既有的 `DESIGN.md` 與 `spikes/`（前者為設計依據，後者為已完成的可行性驗證）。
