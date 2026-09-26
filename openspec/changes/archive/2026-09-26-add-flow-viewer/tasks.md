## 1. 可行性驗證（決策閘之前，不寫 production code）

- [x] 1.1 `spikes/spike29_flow_viewer.py`：讀 golden24 的事件流與 transcript，
      產出一份可點的單檔 HTML。**用真實資料，不用 mock** ——
      這個原型要回答的是「既有資料夠不夠」，mock 資料一定夠
- [x] 1.2 **決策閘**：逐輪軌跡能不能只從 `events.jsonl` + `blobs/traces/dN` 推導出來？
      **可以，但有一個真的缺口**：留存的 `tool_use` 沒有 id（只有 result 那側有
      `tool_use_id`），所以呼叫與結果是**照順序配**的。golden24 三份軌跡配不到的
      有 0 個，因為沒有任何一輪同時發兩個呼叫 —— 那是這幾次執行的性質，不是格式的
      保證。要真的可靠，補在 `_block_to_dict`（加一個 `id`），**不在視圖裡猜**。
      `Trace.unpaired_results` 會回報，所以這個假設破掉時看得見
- [x] 1.3 在原型上檢查「未留存不得被冒充」：推理、截斷的結果、估計的 token
      三者是否都有可見的標示（規格：未留存與非精確的內容不得被冒充）
- [x] 1.4 量一下輸出大小。**golden24 實測 134,197 B**（三份 transcript 82,885 B
      + 版面 ~25KB）。外推到 50 派工約 1.4 MB —— 還行。超過那個量級再改成按需載入
- [x] 1.5 結論寫進 `spikes/RESULTS.md`

## 2. 規格與模型

- [x] 2.1 `openspec validate --specs --strict` 通過
- [x] 2.2 確認 `DataFlow` 不需要改：逐輪軌跡是 transcript 的投影，不是流向的一部分。
      **一行都沒改。** transcript 的 artifact id 本來就在 `DispatchInfo.transcript` 上

## 3. 實作

- [x] 3.1 `myharness/monitor/trace.py`：把一份 transcript 解析成
      turn / 工具呼叫 / 結果 的結構。**純函式**，因為要能被測
- [x] 3.2 解析器對未知的區塊型別寬容（與事件流對未知型別的處理一致）
- [x] 3.3 `myharness/monitor/viewer.py`：`render_html(flow, events, traces) -> str`
- [x] 3.4 `myharness inspect <job> --html [-o out.html]`；未給 `-o` 寫到 stdout
      （規格：輸出位置須被指定）。transcript 透過 store 的 `localize()` 讀，
      用 `GrantSet.unrestricted` —— 不自己拼路徑（design.md D6）
- [x] 3.5 CJK 不需要 `render.py` 的寬度處理 —— 但字型堆疊要有 CJK 後備，
      不能靠瀏覽器碰運氣

## 4. 測試

- [x] 4.1 `tests/monitor/test_trace.py`：解析器對 golden24 的 d1 給出
      7 個推理標記、14 次工具呼叫、14 個結果
- [x] 4.2 推理區塊 `chars: 0` 與 `chars: 800` 渲染成不同的東西
      （規格：後端回傳空的推理區塊）
- [x] 4.3 被截斷的工具結果帶節錄標示（規格：被截斷的工具結果）
- [x] 4.4 `test_outputs_agree`：對同一份事件流，ASCII / JSON / HTML
      三者的異常清單相同（規格：三種輸出一致）
- [x] 4.5 產生 HTML 前後，job 目錄的雜湊不變（規格：產生輸出不改變 job）
- [x] 4.6 產出的 HTML 不含任何 `http://` / `https://` 的外部資源引用
      （規格：離線開啟）
- [x] 4.7 用 `tests/dataflow/fixtures/golden5-events.jsonl` 跑一次 ——
      那次的 CRITICAL 異常必須出現在 HTML 裡

## 5. 文件

- [x] 5.1 `myharness/monitor/README.md`：加一節說明為什麼有第三種 render，
      以及為什麼它仍然不引入相依
- [x] 5.2 `DESIGN.md` 4.7「已實作的投影」補上這一項
- [x] 5.3 寫明推理內容未留存這件事 —— 它是使用者第一個會問的問題

## 6. 實作時才發現的

- [x] 6.1 **payload 坐在 `<script>` 裡，而裡面每一個字串都是模型寫的。**
      一個 `</script>` 就把元素關掉，後面變成瀏覽器樂意執行的標記。
      `_as_script_literal` 把 `<` 轉成 `\u003c`（連同 U+2028/2029）。
      這是測試抓到的，不是想到的
- [x] 6.2 第一版的「估計 vs 後端回報」對 `tokens.estimated` 的派工顯示誤差 0.0% ——
      那個「回報值」就是估計值本身。改成明講沒有可比的回報值。
      **規格那條需求的第一個受害者是我自己的面板**
- [x] 6.3 確認 `viewer.html` 進得了 wheel（hatchling `packages = ["myharness"]` 會帶）
