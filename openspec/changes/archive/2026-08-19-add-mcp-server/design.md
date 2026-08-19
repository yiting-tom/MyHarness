# Design — 對外的 MCP server

## D1 為什麼 job 必須在背景跑

`analysis_start` 如果阻塞到分析完成，這一層就沒有存在意義了：客戶端會在一個
tool call 裡等三十分鐘，期間拿不到進度、答不了問題、給不了補充資料。
DESIGN #2 早就決定「job-based 非阻塞」。

所以 `JobManager` 持有 `asyncio.Task`，以 job_id 定址。這帶來三件必須處理的事：

1. **背景 task 的例外會被吞掉。** 一個沒有人 await 的 task 拋出例外時，
   asyncio 只在 GC 時印一行警告。job 的最終狀態必須由 task 的完成回呼寫進事件流，
   而不是靠有人記得去 await 它。
2. **並行上限。** 每個 job 會起數條 lane，每條 lane 是一個 SDK 子程序。
   沒有上限就是沒有上限。
3. **process 重啟。** in-memory 的 task 會消失。見 D4。

## D2 long-poll 要等的是「狀態改變」，不是時間

`analysis_poll(wait=30)` 若實作成 sleep 迴圈，就只是把空 poll 搬進 server。
每個 job 帶一個 `asyncio.Event`，狀態有實質變化時 set，poll 等它或等逾時。

「實質變化」的定義要窄：dispatch 開始/結束、有新問題、job 結束。
`ctx` 事件每個 turn 都有，拿它當訊號等於每 30 秒喚醒一次客戶端卻沒有新資訊。

逾時返回**不是錯誤**，是「還在跑，沒有新事」。客戶端據此決定要不要再 poll。

阻塞時長：`MCP_TOOL_TIMEOUT` 預設 ≈27.8h，且 in-process tool 靜默阻塞
180s 與 600s 實測皆通過（DESIGN §8 Q5）。上限訂 300s，遠在已驗證範圍內。

## D3 回給客戶端的東西同樣要有上限

這一層保護的是 host agent 的 context —— 那是「外部化狀態 + 短命執行者」
遞迴套用的第一層（DESIGN §1 的表格第一列）。前面每一層都上了兩道閘，
這一層沒理由例外：

- `analysis_poll` 回**有界的進度摘要**，不回事件流。進行中的 dispatch 數、
  完成數、最近一件事的一行描述、待答問題。
- `analysis_result` 回 executive summary + **章節價目表**（每節的 est_tokens），
  不回全文。客戶端看價目表決定要不要 `analysis_drill`。
- `analysis_drill` 一次一節，且該節仍受 token 上限約束。

價目表這個設計是 DESIGN #14 的核心：**讓客戶端在花掉 context 之前知道要花多少**。

## D4 結果是投影，所以比 process 活得久

`analysis_result` 與 `analysis_drill` 只需要事件流與 artifact store，
兩者都在磁碟上。所以它們對**上一個 process 跑的 job** 也要能回答。

只有 `analysis_poll` 和 `analysis_answer` 需要活著的 job —— 它們操作的是
running task 與 in-memory 的問題佇列。對已消失的 job，這兩者要回
「這個 job 不在執行中」而不是「查無此 job」，因為那是兩件不同的事，
而客戶端對它們的處置不同。

這條性質不是免費得來的，是 DESIGN #13「事件流為地基」一路撐到這裡的結果。
寫成需求，免得日後有人為了方便把狀態塞進記憶體而讓它悄悄失效。

## D5 失敗是值

和 lane 工具、orchestrator 工具一致：MCP 工具回文字結果，不拋例外。
客戶端是一個 agent，一個能讀懂「為什麼被拒絕」的錯誤訊息可以自己修正，
一個 stack trace 只會浪費它一個 turn。

## D6 `analysis_provide` 先落地、不路由

Proxy（DESIGN #4）不在這個 change。`analysis_provide` 會：

1. 把 payload 寫成 blob（零 token 進 context）
2. 發一個 ingress 事件
3. 通知 orchestrator「有新資料，id 是這個，**未經路由**」

回應明說未經路由，是因為沉默的降級最危險 —— 客戶端會以為資料已經被送到
正確的 lane。第四次 golden job 的教訓就是「被接受但其實沒作用」比明確拒絕更難查。

## D7 為什麼是 stdio server 而不是 `create_sdk_mcp_server`

`create_sdk_mcp_server` 造的是**給 SDK 自己用**的 in-process server 物件
（lane 與 orchestrator 的工具面都是這樣建的）。外部客戶端連不上它。
對外要的是 `mcp.server.Server` + `stdio_server()`，兩者是不同的東西，
剛好名字都叫 MCP server。
