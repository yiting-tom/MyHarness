## 1. 步驟事件
- [x] 1.1 `events/types.py` 新增 `LANE_STEP = "lane.step"` 並放進 `KNOWN_TYPES`
- [x] 1.2 `worker.step_event(message, acc, budget)`：純函式，模型回應 → 工具與參數摘錄、思考與文字的字元數；工具結果 → 每筆的字元數與是否出錯；不含內容
- [x] 1.3 `WorkerToolbox.on_step`，比照 `on_read` 由 worker 注入；`_run_once` 在每則訊息後呼叫
- [x] 1.4 `current_activity` 的尾端視窗排除步驟事件；MCP progress 的 recent 排除步驟事件
- [x] 1.5 確認 `NotifyingEventLog` 不因步驟事件喚醒（`MEANINGFUL` 不含它）
- [x] 1.6 測試：摘錄有界、結果不含內容、限流狀態不被擠掉、long-poll 不被喚醒

## 2. 伺服器
- [x] 2.1 `build_state` 每個派工帶最近的步驟、最後一步、預算比例
- [x] 2.2 `steps_why`：整份事件流沒有步驟事件 vs. 這次派工還沒有第一輪
- [x] 2.3 事件列表排除步驟事件（它們在圖上，不在事件欄裡洗版）

## 3. 頁面（ui-ux-pro-max）
- [x] 3.1 以圖為主：資料 → agent → 產出 → 報告，SVG，由左到右
- [x] 3.2 執行中的 agent 節點：最近一步、輪次、預算條、脈搏
- [x] 3.3 新的讀取與產出沿著邊流過（尊重 prefers-reduced-motion）
- [x] 3.4 選取 agent → 側欄步驟流；已結束 → 完整逐輪紀錄
- [x] 3.5 既有的空白說明、job 不存在、事件流停滯判斷全部保留
- [x] 3.6 每種視覺編碼都有文字等價物（圖例）

## 4. 收尾
- [x] 4.1 pytest、ruff、mypy --strict、openspec validate --strict
- [x] 4.2 用 golden 的事件流實際開一次頁面看
