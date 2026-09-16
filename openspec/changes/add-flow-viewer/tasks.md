## 1. 可行性驗證（決策閘之前，不寫 production code）

- [ ] 1.1 `spikes/spike29_flow_viewer.py`：讀 golden24 的事件流與 transcript，
      產出一份可點的單檔 HTML。**用真實資料，不用 mock** ——
      這個原型要回答的是「既有資料夠不夠」，mock 資料一定夠
- [ ] 1.2 **決策閘**：逐輪軌跡能不能只從 `events.jsonl` + `blobs/traces/dN` 推導出來？
      若不能，缺的是哪一個欄位 —— 缺口補在 `event-log` 或 `_block_to_dict`，
      不在視圖裡用相鄰資訊猜
- [ ] 1.3 在原型上檢查「未留存不得被冒充」：推理、截斷的結果、估計的 token
      三者是否都有可見的標示（規格：未留存與非精確的內容不得被冒充）
- [ ] 1.4 量一下輸出大小。golden24 三份 transcript 共 ~81KB；
      若一個 50 派工的 job 會產出數十 MB，那 transcript 要能按需載入而不是全內嵌
- [ ] 1.5 結論寫進 `spikes/RESULTS.md`

## 2. 規格與模型

- [ ] 2.1 `openspec validate --specs --strict` 通過
- [ ] 2.2 確認 `DataFlow` 不需要改：逐輪軌跡是 transcript 的投影，不是流向的一部分。
      若發現需要把 turn 放進 `DataFlow`，先停下來 —— 那代表這一層的邊界畫錯了

## 3. 實作

- [ ] 3.1 `myharness/monitor/trace.py`：把一份 transcript 解析成
      turn / 工具呼叫 / 結果 的結構。**純函式**，因為要能被測
- [ ] 3.2 解析器對未知的區塊型別寬容（與事件流對未知型別的處理一致）
- [ ] 3.3 `myharness/monitor/viewer.py`：`render_html(flow, events, traces) -> str`
- [ ] 3.4 `myharness inspect <job> --html [-o out.html]`；未給 `-o` 寫到 stdout
      （規格：輸出位置須被指定）
- [ ] 3.5 CJK 不需要 `render.py` 的寬度處理 —— 但字型堆疊要有 CJK 後備，
      不能靠瀏覽器碰運氣

## 4. 測試

- [ ] 4.1 `tests/monitor/test_trace.py`：解析器對 golden24 的 d1 給出
      7 個推理標記、14 次工具呼叫、14 個結果
- [ ] 4.2 推理區塊 `chars: 0` 與 `chars: 800` 渲染成不同的東西
      （規格：後端回傳空的推理區塊）
- [ ] 4.3 被截斷的工具結果帶節錄標示（規格：被截斷的工具結果）
- [ ] 4.4 `test_outputs_agree`：對同一份事件流，ASCII / JSON / HTML
      三者的異常清單相同（規格：三種輸出一致）
- [ ] 4.5 產生 HTML 前後，job 目錄的雜湊不變（規格：產生輸出不改變 job）
- [ ] 4.6 產出的 HTML 不含任何 `http://` / `https://` 的外部資源引用
      （規格：離線開啟）
- [ ] 4.7 用 `tests/dataflow/fixtures/golden5-events.jsonl` 跑一次 ——
      那次的 CRITICAL 異常必須出現在 HTML 裡

## 5. 文件

- [ ] 5.1 `myharness/monitor/README.md`：加一節說明為什麼有第三種 render，
      以及為什麼它仍然不引入相依
- [ ] 5.2 `DESIGN.md` 4.7「已實作的投影」補上這一項
- [ ] 5.3 寫明推理內容未留存這件事 —— 它是使用者第一個會問的問題
