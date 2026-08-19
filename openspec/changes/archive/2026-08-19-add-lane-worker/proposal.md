## Why

MyHarness 的核心論點是「**強制** 而非祈禱」：subagent 回傳給 orchestrator 的東西，
其長度與形狀應該由 API 層與程式碼保證，而不是靠 prompt 拜託。Spike #3 已證明
`output_format` 與 `task_budget` 這些機制**存在且可用**，但還沒有證明它們
**在一個真的會亂寫東西的 worker 身上守得住**。

這是整個專案的分水嶺。如果一個 lane worker 在被要求「寫一份 3000 字報告」時
仍然只能吐出 120 token 的 handle、超預算時仍然能被轉成結構化的失敗值而不是拋錯，
那麼 orchestrator 的 context 上界就是程式碼保證的常數，後面所有元件都成立。
如果守不住，整個架構要重新考慮。

先做這一層而不是先做 orchestrator，是因為它可以用一個假的（純程式碼）driver 驅動，
不需要真的 orchestrator 就能端到端驗證，而 orchestrator 反過來不行。

## What Changes

- 新增 `lane-worker` capability：ephemeral agent + durable lane state 的執行循環。
  每次任務開全新 context，讀 charter 與 lane state，工作，寫 findings 與新的 state，
  回傳受 schema 強制的 handle，然後死亡。
- 新增 `model-backend` capability：per-lane 的後端與模型設定（Anthropic 直連 /
  OpenRouter / 自架 LiteLLM），內建工具裁切，以及 capability 宣告與降級判定。
- 失敗成為值：超預算、工具持續失敗、`max_turns` 用盡皆轉為結構化 handle 而非例外；
  transient 錯誤（429/5xx、網路）由 framework 靜默重試。
- Worker 的完整 transcript 落盤至 `traces/`，並在 handle 中引用。
- 執行過程寫入既有的 event log（`dispatch.start` / `dispatch.end` / `ctx`）。
- 不含 orchestrator、proxy、MCP server、`await_tasks` 排程（後續 change）。

## Capabilities

### New Capabilities
- `lane-worker`: 單一 lane 任務的執行單元。定義 LaneType 與 lane instance、
  ephemeral worker 的生命週期、lane state 的讀寫與上限、handle 契約的強制、
  失敗與降級的結構化表達、以及 transcript 的保存。
- `model-backend`: LLM 後端的可插拔設定。定義 backend profile、模型別名映射、
  per-lane 的 endpoint 與金鑰、內建工具的裁切、以及 backend capability 的宣告
  與缺少時的降級行為。

### Modified Capabilities
（無 —— `artifact-store` 與 `event-log` 的既有 requirement 已足以支撐本層，
worker 只是它們的使用者。這正是地基設計正確的訊號。）

## Impact

- **新增程式碼**：`myharness/lanes/`（LaneType、charter 載入、worker 執行迴圈、
  handle schema、失敗轉換）、`myharness/backends/`（BackendProfile、capability）、
  `tests/lanes/`。
- **相依**：`claude-agent-sdk`（首次成為執行期相依，此前只用於 spike）、
  `jsonschema`（handle 的應用層驗證，供不支援結構化輸出的後端降級使用）。
- **需要網路與金鑰的測試**：handle 契約與預算的端到端驗證必須打真實 API。
  這些測試以 marker 標記並預設跳過，CI 與離線開發只跑可離線的部分。
- **對後續 change 的約束**：`dispatch` 的簽章、handle 的欄位、失敗 handle 的
  `status` 值域，都由本 change 定案；orchestrator 只是它們的呼叫端。
- **會消耗費用**：端到端測試每次執行會產生實際 API 花費（預期每次 < 0.5 USD）。
