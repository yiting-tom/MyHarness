## Why

`monitor --web` 能即時看到的只有兩件事：一次派工**開始了**、它**讀了**哪份資料。
在那之後直到它結束，一個 agent 在做什麼 —— 下了幾次查詢、拿回多少資料、
預算燒到哪 —— 事件流裡一個字都沒有。逐輪紀錄存在，但它是派工**結束時**才落檔的。

golden #25 的 d1 跑了 6 分鐘、22 次查詢、用掉 89% 預算，然後什麼都沒寫就被回合上限切掉。
看著它跑的人，在那 6 分鐘裡只看得到一個計時器在走。**那正是 monitor 該回答、
而它答不出來的問題**：這個 agent 是在推進，還是在原地打轉？

使用者要的是一個「可以即時顯示資料流、agent 處理資料的視覺化 monitor」。
畫面可以重做，但畫面只能投影事件流裡有的東西 —— 所以這個 change 有兩半，
而第一半是前提。

## What Changes

- **新增事件 `lane.step`**：worker 在執行**期間**，每一輪模型回應、每一批工具結果
  各寫一筆。內容是：呼叫了哪些工具、參數的有界摘錄、每個結果的大小與是否出錯、
  當下花掉的預算與比例、這是第幾次嘗試的第幾輪。
  - **不含**推理文字（本來就不留存）、**不含**工具結果的內容（只有大小）。
    事件流是給監看與稽核的，不是第二份 transcript。
  - **不喚醒** MCP 的 long-poll、**不進** MCP progress 的 recent、
    **不擠掉**終端機 monitor 判斷「在等什麼」的那幾筆事件 ——
    它是高頻事件，而那三個讀者都是為低頻事件設計的。
- **`monitor --web` 改成以圖為主的即時視圖**：資料 → agent → 產出 → 報告，
  由左到右。執行中的 agent 節點直接顯示它這一刻在做什麼、第幾輪、預算條；
  讀取與產出沿著邊以動畫流過。點一個 agent 看它的步驟流；結束後看完整逐輪紀錄。
- **沒有步驟紀錄的派工說它沒有**：在 `lane.step` 之前跑的 job，
  agent 節點 SHALL NOT 畫成「閒著」—— 它是「這份事件流沒有記錄步驟」。

## Capabilities

### Modified Capabilities
- `lane-worker`: 執行期間逐步寫入事件流
- `monitor`: 即時視圖以圖呈現資料流與 agent 的處理

## Impact

- **Specs**: `lane-worker` +1、`monitor` +2
- **Code**: `events/types.py`（`LANE_STEP`）、`lanes/worker.py`（`step_event`、串流迴圈）、
  `lanes/tools.py`（`on_step`）、`monitor/live.py` 與 `mcp/service.py`（過濾）、
  `monitor/serve.py`（每個派工的步驟）、`monitor/live.html`（重寫）
- **事件量**：每輪約 2 筆，一個 44 輪的派工約 90 筆、每筆 < 1 KB
