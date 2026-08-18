## Context

前一個 change 建好了不含 LLM 的地基（`artifact-store`、`event-log`）。
本 change 是第一個真的呼叫 LLM 的層，也是驗證整個架構核心論點的地方：
**subagent 回傳給 orchestrator 的東西，其長度與形狀由機制保證，不由模型的自制力決定。**

已知的事實基礎（`spikes/RESULTS.md`）：

- `output_format={"type":"json_schema","schema":...}` 會轉成 CLI 的 `--json-schema`，
  結構化結果出現在 `ResultMessage.structured_output`。Anthropic 直連、OpenRouter 的
  Claude / GPT / Gemini **四者皆通過**。
- `task_budget={"total": N}` 超限時 **SDK 拋例外**（`Claude Code returned an error
  result: success`），且 **`ResultMessage` 不會來**。
- `disallowed_tools` 會把工具從請求中移除（省 ~17.7k tokens）；`allowed_tools` 不會。
- `ClaudeAgentOptions.env` 會併入子行程環境，因此 per-lane 的 endpoint / 金鑰 /
  模型別名可以逐次覆寫。
- prompt caching 在直連與 OpenRouter 上都會命中，charter 當穩定前綴成立。
- SDK in-process MCP tool 靜默阻塞 600 秒不會被中止。

## Goals / Non-Goals

**Goals:**
- 讓 handle 的大小上界成為程式碼保證的常數，可用一個「被要求寫長文」的測試證明。
- 讓所有語意層級的失敗都是值，呼叫端永遠不需要 try/except 來處理業務失敗。
- 讓 ephemeral worker 的 context 上界由構造保證：charter + state 上限 + input 額度。
- 讓 lane 能在不同後端與模型上執行，且「這次是強制還是降級」可從事件流查明。
- 用純程式碼的 driver 就能端到端驗證，不需要 orchestrator。

**Non-Goals:**
- 不實作 orchestrator、`dispatch` / `await_tasks` 工具、排程與並行控制。
- 不實作 proxy 與 routing table。
- 不實作 lane state 的壓縮策略（超限先拒絕並記錄降級，壓縮留待有真實資料後）。
- 不做跨 lane 的並行（本層只保證 CAS 能偵測衝突）。
- 不做 charter 的撰寫指南（需要真實 job 的經驗才寫得出有用的版本）。

## Decisions

### D1. `task_budget` 拋例外，所以必須邊串流邊累積

Spike #3c 顯示超預算時 SDK 拋例外且 `ResultMessage` 不會抵達。若實作等到
`ResultMessage` 才組 handle，超預算的情況會**什麼都拿不到** —— 而那正是最需要
部分結果的情況。

**選擇**：worker 迴圈邊消費訊息邊累積（訊息、token 用量、已寫入的 artifact、
最後一次已知的部分產出），例外發生時用累積到的狀態組成失敗 handle。

**替代方案**：不用 `task_budget`，只用本地 token 計數。**否決** —— API 端的硬預算
是唯一能在單一請求內就攔住失控的機制，本地計數只能在請求之間檢查。兩者都要：
API 端當硬牆，本地計數當降級路徑（backend 不支援時）。

### D2. Handle 的大小上界由程式碼保證，不由 schema 保證

JSON Schema 能約束**形狀**，不能約束**長度** —— 模型完全可以吐出一個
符合 schema 但 headline 有 3000 字的物件。

**選擇**：schema 管形狀，`clamp_handle()` 管長度。每個字串欄位有硬上限，
超出即截斷並設 `truncated: true`。呼叫端拿到的 handle 有可證明的大小上界。

這是「強制 vs 祈禱」在實作層的具體樣子：**兩道機制疊起來才是保證，
少任何一道都只是很可能。**

### D3. Lane state 超限時拒絕，不截斷

**選擇**：新 state 超過 token 上限 → 保留舊 state，該次執行標記降級。

**替代方案**：自動截斷或自動壓縮。**否決** —— 截斷會從中間切斷語意；
自動壓縮是另一次 LLM 呼叫，失真不可控且會默默燒錢。拒絕讓問題立刻顯性化，
而「該壓縮什麼」需要真實 job 的資料才能設計，現在做只是猜。

代價：長 job 的 lane 會撞牆。這是刻意的 —— 撞牆會產生一筆降級事件，
那筆事件就是設計壓縮策略所需要的資料。

### D4. Worker 的 artifact 存取透過 in-process MCP 工具，不直接給檔案系統

**選擇**：worker 拿到的是 `read_note` / `write_finding` / `update_state` /
`localize_blob` 等 SDK in-process 工具，每個都在 harness 這側做授權檢查。
Worker 的 `disallowed_tools` 移除所有內建檔案工具。

**理由**：授權模型（前一個 change 的 `GrantSet`）只有在 worker 沒有旁路時才成立。
給 worker `Read` / `Bash` 等於把授權檢查變成裝飾。

### D5. Charter 是檔案，不是字串常數

**選擇**：LaneType 引用一個 charter 檔案路徑，內容在建構時載入。

**理由**：charter 是 prompt caching 的穩定前綴，也是最需要反覆調整的東西。
放檔案讓它可以被 diff、被 review、被獨立於程式碼修改。

### D6. 需要網路的測試以 marker 隔離，預設跳過

**選擇**：`@pytest.mark.live`，預設 `-m "not live"`。離線測試用一個假的
transport / 錄製回放來覆蓋所有邏輯分支；live 測試只驗證「機制在真實 API 上
確實生效」這件事本身。

**理由**：契約強制這件事**必須**對真實 API 驗證過才算數（這是本 change 的存在理由），
但不能讓每次 `pytest` 都花錢。兩者都要，分開跑。

### D7. Backend capability 是宣告，不是偵測

**選擇**：`BackendProfile.capabilities` 由設定宣告，系統據此選路徑，
並把實際採用的路徑寫進事件流。

**替代方案**：執行期自動偵測。**否決** —— 偵測需要試打，會產生費用與延遲，
且失敗模式模糊（一次失敗不代表不支援）。宣告錯了會在 live 測試中暴露，
而事件流記錄了實際路徑，事後可稽核。

## Risks / Trade-offs

- **模型仍可能在 handle 外洩漏長文**（例如把報告塞進 `metrics` 的鍵名）→
  `clamp_handle()` 對整個序列化結果也設上限，而非只看個別欄位。
- **`task_budget` 的例外訊息不穩定**（目前是 `"...error result: success"`，
  語意含糊）→ 不解析訊息字串來分類，改以「是否收到 `ResultMessage`」
  加上本地 token 累計來判定，例外訊息只放進 handle 供人閱讀。
- **live 測試的不確定性**：模型行為有隨機性，斷言可能偶爾失敗 →
  live 斷言只針對機制（handle 符合 schema、大小有上界、失敗是值），
  不針對內容品質。
- **charter 檔案與程式碼不同步** → LaneType 建構時載入並在事件流記錄
  charter 的雜湊，使「這次跑的是哪一版 charter」可查。
- **in-process 工具讓 worker 無法用 Bash 做臨機處理** → 這是刻意的取捨；
  需要的能力應成為一個明確的工具，而不是給一把萬用鑰匙。

## Migration Plan

無既有資料需要遷移。本 change 首次引入 `claude-agent-sdk` 為執行期相依。

`.env` 需提供至少一組後端金鑰供 live 測試使用；離線測試不需要任何金鑰。

## Open Questions

- Lane state 的 token 上限預設值？暫定 8000，待真實 job 的降級事件累積後校準。
- Handle 各欄位的長度上限？暫定 headline 200 字元、整體序列化 2000 字元，
  同樣待實測校準。
- 應用層降級路徑的重試次數？暫定 2 次，超過即失敗 handle。
- Transient 重試的退避參數是否需要 per-backend 設定？暫定全域，
  待遇到不同後端的速率限制行為差異再說。
