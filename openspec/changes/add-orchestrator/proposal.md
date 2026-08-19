## Why

三層地基已經就位：artifact store 保證原始資料不可能被讀進 context，
lane worker 保證回傳值的大小由程式碼而非模型的自制力決定，
backend gate 保證多條 lane 不會各自去撞同一個被限流的後端。

缺的是**指揮的那一層**。目前每次 lane 執行都要由一個純程式碼的 driver 手動觸發：
沒有人決定要開哪些 lane、派什麼任務、什麼時候收工，也沒有人在 40 次 dispatch 之後
把散落各處的 finding 收斂成一份報告。

Orchestrator 是唯一有全局視野的角色，因此也是唯一能判斷「這個失敗要不要緊」的角色 ——
lane worker 刻意把所有語意失敗做成值回傳，就是為了讓這個判斷留給它。
在它存在之前，那些失敗值沒有人讀。

這一層同時是 context 紀律的最後一道考驗。前三層保證了**沒有任何一條路徑**能把原始資料
送進 orchestrator，但它自己的規劃推理、handle 累積與細節窺看仍會成長。這個 change
要讓那個成長成為程式碼保證的常數，而不是祈禱。

## What Changes

- 新增 `orchestrator` capability：常駐的規劃與調度角色，持有一份可續跑的計畫，
  以固定的小工具面操作 lane，並在預算耗盡時善終而非被硬砍。
- 新增 `job-runner` capability：job 的生命週期與非阻塞任務調度 ——
  `dispatch` 起背景任務後立刻返回，`await_tasks` 以單一阻塞呼叫收割。
- 防迴圈三道防線：重複 dispatch 偵測、job 級硬上限、無進展偵測。
- `peek` 的 job 級 token 預算，把 orchestrator 最大的變數變成常數。
- 向使用者提問以抽象通道表達，供日後的 MCP 層實作。
- 最終報告由 synthesis lane 產出，orchestrator 不親自彙整。
- 不含 MCP server 對外介面與 proxy（後續 change）。

## Capabilities

### New Capabilities
- `orchestrator`: 全局規劃與調度。定義 orchestrator 的工具面與其上界、
  計畫的維護與續跑、peek 預算、context 逼近上限時的交接重啟、
  失敗 handle 的處置、以及收工條件。
- `job-runner`: 一個 job 的執行框架。定義非阻塞 dispatch 與阻塞收割的語意、
  背景任務的生命週期、三道防迴圈機制、job 級的硬上限與善終、
  以及向使用者提問的抽象通道。

### Modified Capabilities
- `event-log`: 新增 job 級預算與計畫演進的事件型別，使「orchestrator 為什麼這樣決定」
  與「這個 job 為什麼收工」可從事件流重建。

## Impact

- **新增程式碼**：`myharness/orchestrator/`（工具面、計畫狀態、預算、防迴圈）、
  `myharness/jobs/`（job runner、任務登記、使用者通道），以及對應測試。
- **對既有層的使用**：只透過 `run_lane_worker` 與 artifact store／event log 的既有介面，
  不修改它們的行為。`event-log` 的變更是純新增事件型別。
- **需要真實模型的測試**：orchestrator 的規劃品質無法離線驗證，
  需要一個小型的端到端 golden job；同樣以 `live` marker 標記。
- **對後續 change 的約束**：MCP 層的 `analysis_*` 工具將是 job runner 的薄包裝，
  其 job 狀態、提問佇列與報告交付格式由本 change 定案。
- **預期費用**：golden job 每次執行約 $0.1–0.5，視 lane 數量而定。
