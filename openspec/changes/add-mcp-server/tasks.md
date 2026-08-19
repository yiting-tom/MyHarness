## 1. Job 生命週期

- [x] 1.1 `myharness/mcp/manager.py`：`JobManager` 以 job_id 定址背景 `asyncio.Task`
      （規格：分析以非阻塞方式啟動）
- [x] 1.2 背景 task 的例外經完成回呼寫進事件流，不靠有人 await
      （design D1；規格：背景失敗不得無聲消失）
- [x] 1.3 未捕捉例外的測試：讓 loop 拋錯，斷言最終狀態查得到
- [x] 1.4 併行上限與其拒絕訊息（規格：併行分析數有上限）
- [x] 1.5 job 結束後從 running 集合移除，但識別碼仍可查（design D4）
- [x] 1.6 關閉 manager 時取消所有背景 task 並等待它們收尾

## 2. 狀態變更通知

- [x] 2.1 每個 job 一個 `asyncio.Event`；定義「實質改變」為 dispatch 起訖、
      新問題、job 結束 —— **不含 `ctx`**（design D2）
- [x] 2.2 `wait_for_change(timeout, since=)`：等到改變或逾時。**加了 revision cursor**
      —— 沒有它，兩次 poll 之間發生的改變會遺失（測試抓到）
- [x] 2.3 測試：改變發生時立即返回（不等滿 timeout）
- [x] 2.4 測試：無事發生時逾時返回，且不是錯誤
- [x] 2.5 測試：`ctx` 事件不會喚醒等待者（否則等於每 turn 空 poll 一次）
- [x] 2.6 測試：已結束的 job 立即返回

## 3. 回應上限

- [x] 3.1 `myharness/mcp/payload.py`：進度摘要的形狀與上限。**整體上限要真的執行** ——
      每項上限相乘會超過總上限（8×120 + 5×400 ≈ 3,000），和查詢結果同一個教訓
- [x] 3.2 測試：事件數成長時，進度回應長度不成比例成長
- [x] 3.3 結果回應 = executive summary + 章節價目表（est_tokens），不含全文
- [x] 3.4 `analysis_drill` 一次一節且該節受 token 上限
- [x] 3.5 待答問題在進度中呈現，且問題本身也受上限

## 4. 六個對外工具

- [ ] 4.1 `analysis_start(task, ...)` → `{job_id, status}`
- [ ] 4.2 `analysis_poll(job_id, wait)` → 進度摘要 + 待答問題（long-poll）
- [ ] 4.3 `analysis_provide(job_id, payload, name)` → `{artifact, routed: false}`
      （規格：客戶端能補充資料且該資料不進入任何 context）
- [ ] 4.4 `analysis_answer(job_id, question_id, text)` → `{ok}`；未知 id 要拒絕
- [ ] 4.5 `analysis_result(job_id)` → 摘要 + 價目表 + caveats + 成本
- [ ] 4.6 `analysis_drill(job_id, section_id)` → 單節全文
- [ ] 4.7 全部失敗回文字結果不拋例外（design D5；規格：工具失敗以可據以行動的訊息回覆）
- [ ] 4.8 「不在執行中」與「查無此 job」分開回報（規格：不在執行中與不存在是兩件事）

## 5. Stdio server

- [ ] 5.1 `myharness/mcp/server.py`：`mcp.server.Server` + `stdio_server()`
      （design D7 —— 與 `create_sdk_mcp_server` 不是同一個東西）
- [ ] 5.2 工具 schema 明確宣告 `required`，不用簡寫（前一個 change 的教訓）
- [ ] 5.3 `myharness-mcp` 進入點與 `--root` / `--backend` 參數
- [ ] 5.4 以真實 MCP client session 走一次 list_tools / call_tool 的測試

## 6. 跨 process 的結果

- [ ] 6.1 `analysis_result` / `analysis_drill` 只讀事件流與 store
      （規格：結果查詢不依賴分析仍在執行）
- [ ] 6.2 測試：對一個「不在 manager 裡」的 job_id 仍能取得結果
- [ ] 6.3 測試：對同一個 job_id 的 poll 回「不在執行中」而非「查無此 job」

## 7. 文件與端到端

- [ ] 7.1 `myharness/mcp/README.md`：六個工具、上限、跨 process 性質、未做的 proxy
- [ ] 7.2 `README.md` 加上 Claude Code 的連接方式（`claude mcp add`）
- [ ] 7.3 `DESIGN.md` §9 移除「對外的 MCP server 未建」
- [ ] 7.4 端到端（離線、假 backend）：start → poll → answer → result → drill
- [ ] 7.5 live：從真實 MCP client 跑一次小 job，記錄到 `spikes/RESULTS.md`
