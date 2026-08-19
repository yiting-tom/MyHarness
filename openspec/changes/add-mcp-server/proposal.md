## Why

`DESIGN.md` 第一行寫的是「以 **MCP server** 形式對外提供資料分析能力」。
那一層從第一個 change 就被推遲，到現在還沒建。

結果是：六個 capability、44 條需求、428 個測試、資料工具也補齊了，
**但沒有任何客戶端能觸發一次分析**。唯一的進入點是三個 Python 檔案
（`myharness.goldens`、`myharness.lanes.driver`、monitor CLI）。

該接的東西其實都在原地等著：

| 需要的 | 已存在 |
|---|---|
| job 狀態 | `JobRunner.status()` |
| 問答通道 | `QueueChannel.pending()` / `.answer()` |
| 交付與鑽取 | `build_delivery()` / `drill()` |
| 事件與成本 | `summarize()` / `derive_caveats()` |

缺的是**生命週期**：`analysis_start` 必須立刻返回並讓 job 在背景跑，
而目前 `run_golden` 是 `await loop.run()` 一路跑到底。

## What Changes

- 新增 `myharness/mcp/`：一個 **stdio MCP server**（`mcp.server.Server` +
  `stdio_server()`），供 Claude Code / Desktop 直接連線。
- 六個對外工具，即 `DESIGN.md` §4.6 的那一組：
  `analysis_start` / `analysis_poll` / `analysis_provide` /
  `analysis_answer` / `analysis_result` / `analysis_drill`。
- 新增 `JobManager`：job 在背景 task 中執行，以 job_id 定址，有並行上限。
- `analysis_poll` 採 long-poll：等狀態真的改變才回，不空轉
  （in-process tool 阻塞 180s / 600s 實測皆通過，DESIGN §8 Q5）。
- **回給客戶端的每一份 payload 都受上限約束。** 這一層保護的是 host agent 的
  context，和 handle、查詢結果同一個形狀的問題。
- `analysis_result` / `analysis_drill` 對**已結束的 job** 也要能用：
  它們是事件流與 store 的投影，不依賴那個 job 的 process 還活著。
- 新增 `myharness-mcp` 進入點。

## 不在這個 change 裡

**Proxy agent（DESIGN #4）不做。** `analysis_provide` 會落 blob 並通知
orchestrator，但**不會**用 LLM 判斷該路由到哪條 lane —— 那是一次模型呼叫、
一份 routing table 契約、一組自己的失敗語意，塞進來會讓這個 change 不可審。
未匹配的資料交給 orchestrator 自行決定，並在回應中明說「未經路由」。

## Impact

- Specs: `mcp-server`（新 capability）
- Code: 新增 `myharness/mcp/`（server、job manager、payload 上限）；
  `myharness/jobs/runner.py` 可能需要曝露一個狀態變更通知
- Deps: 無新增（`mcp` 已隨 `claude-agent-sdk` 進來）
- 不動：授權模型、handle contract、事件型別、lane 與 orchestrator 的工具面
