## Why

對外只有一條路：MCP over stdio。客戶端必須把 `myharness-mcp` 當**子程序 spawn**，
所以只有同一台機器上的 agent 用得到。要讓遠端的 agent 用這套 harness，
今天沒有答案。

A2A 是這件事的現成協定。但它的 artifact 模型預設是「**task 做完，artifact 就是成品**」，
而這個專案對最外層的核心承諾正好相反 —— `analysis_result` 刻意不回報告全文，
只回摘要與各章節的 token 估計，讓呼叫方**看完價錢再決定讀哪節**。實測那次回應
659 字元，報告本體 4 節 219 token。

**這個張力不是 API 品味問題，是要不要保留最外層那道閘的問題。** 所以這個 change
以評估為主：先驗證 A2A 能不能自然表達「這是目錄不是內容」，再決定要不要實作。

## What Changes

- **新增 spike，驗四件事**（`spikes/`，可重跑、結論寫進 `RESULTS.md`）：
  1. A2A 的 artifact 有沒有欄位能標示「這是章節價目表，不是章節內容」
  2. agent card 能不能宣告兩種輸出模式，讓呼叫方顯式選擇
  3. SSE 串流與現有 long-poll 的 `revision` 游標怎麼對應（漏事件是這裡的失敗模式）
  4. 一次 A2A 請求的固定開銷有多少 —— 對照 spike #12 在 proxy 上量到的 93%
- **決策閘**：第 1 項驗不過（協定沒有自然的表達方式）就退回單軌價目表，
  並把「A2A artifact 語意不足」記成否決理由，不硬塞自訂約定。
- **新增 `a2a-server` capability spec**：描述這道邊界的義務 —— 預設回價目表、
  全文須顯式要求、兩種模式都要在 agent card 上宣告。規格不綁傳輸細節。
- **MCP 邊界、orchestrator、lane 一律不動。** A2A 是**第二條**對外的路，不是取代。

## Capabilities

### New Capabilities
- `a2a-server`: 以 A2A 協定對外提供分析能力的邊界。涵蓋輸出模式的宣告與選擇、
  預設不外洩全文、task 生命週期與進度串流、以及不在執行中與不存在的區分。

### Modified Capabilities
（無。A2A 是新增的第二條邊界，`mcp-server` 的既有需求不受影響。）

## Impact

- **Specs**: 新增 `a2a-server`
- **Code**: 新增 `myharness/a2a/`。`AnalysisService` 不改 ——
  `myharness/mcp/server.py` 的 docstring 已言明「這個檔案以上都是 protocol-free」，
  A2A 端點與 `myharness/mcp/server.py` 同層，共用同一個 service
- **Deps**: 需要 HTTP server（目前執行期依賴只有 `claude-agent-sdk`、`duckdb`、
  `jsonschema`，沒有任何 web 框架）。這是這個 change 第一個實質成本
- **已知的牆**：job 只活在單一 process 裡 —— `JobManager` 把 `asyncio.Task` 放在
  記憶體。`analysis_result` / `analysis_drill` 只讀事件流與 store，換 process 照樣答；
  但**進行中的 task 接不回來**。A2A 呼叫方預期能重連，這是必須先寫明而不是先實作的落差
- **風險**：A2A 端點是網路可達的，MCP stdio 不是。認證、多租戶、job id 猜測
  一律**不在這個 change 裡**，但必須在 design 裡寫清楚為什麼可以先不做
