# myharness.monitor / myharness.dataflow

事件流的投影層。`DESIGN.md` D7 從一開始就說報表、TUI、OpenTelemetry 與回歸測試
都是事件流的投影 —— 這裡是第一個。

**唯讀。** 不寫入任何東西，對執行中的 job 零影響。

## 為什麼存在

Golden job 跑五次才通，每次診斷都是臨時腳本翻事件流。而第五次那個**表面上成功**的跑：

```
d4(syn1)  ←授權 3 份 finding  →產出 report
d5(syn1)  ←授權 （無）        →產出 report   ← 覆蓋掉 d4
```

最終交付的是 d5 —— 沒有任何授權、讀不到任何 finding、內容寫著「我什麼都讀不到」。
它在所有紀律指標上都是綠的。逐行讀事件流看不出來；排成資料流是第一眼就看到的東西。

## 用法

```bash
myharness jobs                          # 有哪些 job 可以看
myharness inspect <job>                 # 事後：資料流、異常、成本歸屬
myharness inspect <job> --json          # 同上，機器可讀
myharness inspect <job> --html -o x.html  # 同上 + 逐輪軌跡（給開發者）
myharness report <job> -o x.html        # 來源報告（給交出資料的人）
myharness monitor <job>                 # 即時：現在在做什麼（終端機）
myharness monitor <job> --web           # 即時：同上，在瀏覽器裡
```

## 收回一個理由

`add-flow-viewer/design.md` 裡寫過：

> 不做即時模式。瀏覽器裡的即時模式需要一個 server，那就違反了「零新增相依」。

**那句話是錯的。** `http.server` 不是相依，它跟 `json` 一樣是 Python 的一部分。
`--web` 起的就是它：只綁 loopback、只有 GET、沒有任何寫入路徑。
終端機那份留著 —— ssh 進去的時候它仍然是唯一能用的東西。

輪詢而不是 SSE，理由沿用 `live.py` 自己那一條：事件流是幾 KB 的 append-only
JSONL，重讀的成本人感覺不到，而 SSE 在 stdlib 的 `http.server` 上要每個 client
佔一條執行緒並讓關閉變難。

**這是整個 package 裡唯一會聽 port 的東西**（MCP 走 stdio，終端機 monitor
什麼都不聽），所以 loopback 那條規則抽到 `myharness/loopback.py`，
跟 A2A 端點共用一份 —— 主機名不算證據，位址才算。

### 逐輪紀錄在執行中的派工上「還不存在」

transcript 是派工**結束時**才落檔的。所以點開一個還在跑的派工，回的是一句話，
不是一個轉圈圈：**「還沒有」跟「不會有」對讀者是兩件事，而兩者都不是「正在載入」。**
執行中看得到的是 `artifact.read` —— 那是執行期間就寫下的，也是唯一會在派工
活著時候動的訊號。

## `--web` 是一張圖，而圖上的 agent 會動

由上往下，一列是一層流向深度：資料 → agent → 產出 → 下一個 agent → 報告。
平行的 lane 很少超過四條，流向卻會一直變深，所以深度走捲動的方向。

agent 節點上「它這一刻在做什麼」來自 **`lane.step`** —— worker 在執行**期間**
為每一則模型回應、每一批工具結果各寫一筆：工具名稱、有界的參數摘錄、
結果的字元數、預算比例。**只記大小，不記內容**；推理文字本來就不留存。
在這之前，派工開始之後到它結束之前，事件流裡一個字都沒有 —— golden #25 的
d1 跑了六分鐘、22 次查詢，看著它的人只看得到一個計時器。

`lane.step` 是高頻事件，所以三個為低頻事件設計的讀者都把它濾掉：
MCP 的 long-poll 不被它喚醒（`NOT_NEWS`）、progress 的 recent 不收它、
終端機判斷「在等什麼」的尾端視窗不數它 —— 否則一條 lane 的六次查詢
就能把另一條 lane 的限流等待擠出畫面。

沒有步驟紀錄的 agent 說它為什麼沒有（舊事件流 / 還在等第一輪 /
第一輪前就結束 / 紀錄不完整），不畫成閒著。
`lanes/<lane>/derived/` 的中間表依路徑歸給那條 lane，畫成虛線 ——
事件流沒有記錄它是哪次派工寫的，圖例照說。

## 點開一份產出：內容、版本、差異

圖上的資料與產出都可以點。note 顯示全文；資料表顯示欄位與前幾行
（二進位格式就說是二進位）。被寫過不只一次的標「改寫 ×N」，
側欄可以切版本、看每一版相對前一版**新增與刪除的行**。

存放區只留最新的一份。**版本史是從逐輪紀錄重建的** —— `write_finding` /
`update_state` 的輸入在 transcript 裡不截斷，被拒絕的寫入不算版本
（`content.py`）。紀錄涵蓋不到時照說：還在跑的派工逐輪紀錄還不存在；
存放區的內容與紀錄中最後一版不同。

同一份紀錄也補上了圖上少的邊：`dispatch.end` 只帶一份 artifact，
所以一條 lane 寫了兩份、只回報一份時，另一份以前根本不在圖上
（golden #26 的 d3 寫了 `txn-2024-finance-correction`）。

## 兩個受眾，兩個視圖，刻意不合併

| | `inspect --html` | `report` |
|---|---|---|
| 讀的人 | 在除錯這套 harness 的人 | 把資料交出去、正被要求相信結論的人 |
| 主體 | 逐輪軌跡：推理標記、工具呼叫、回傳結果 | 資料流向：我的資料被誰讀了、報告根據什麼 |
| 詞彙 | `budget_exceeded`、token、estimate | 「預算用完，沒做完」 |
| 長相 | 深色、三欄、等寬 | 淺色、單欄、正文優先 |

合成一個的結果是兩邊都不合適：使用者不需要看 transcript，而開發者不需要
把 `orphan_output` 翻成一句話再讀回去。

**`report.py` 的翻譯表是那個模組的內容，不是措辭。** `tests/monitor/test_report.py`
會對每一個 `AnomalyKind`、每一個 caveat kind、每一個 dispatch status 檢查有沒有人話，
並且掃過整頁確認沒有任何內部代碼漏到使用者面前。新增一種異常而忘了翻譯，測試會紅。

`--root` 預設 `jobs-scratch`，會往下找兩層 —— **猜目錄不該是使用 monitor 的第一道門檻。**

`inspect` 在偵測到 CRITICAL 異常時以 exit code 2 結束，所以 CI 不需要解析輸出就能擋。

## 授權邊與讀取邊是分開的

它們一致時沒有資訊，不一致時全是資訊。

目前 worker 還沒有寫 `artifact.read` 事件，所以讀取邊是空的 ——
**輸出會明說「實際讀取資訊不可得」，不會讓授權冒充讀取。**
補齊那個事件是 `event-log` 的工作，不是在這裡假裝。

## 四種異常

| 異常 | 嚴重度 | 意思 |
|---|---|---|
| `ungranted_production` | CRITICAL | 沒被授權任何輸入卻產出了分析 —— 它不可能是根據 job 裡的任何東西寫的 |
| `overwritten_output` | CRITICAL（報告）/ WARNING | 同一個 artifact 被多次寫入，最終版本來自誰 |
| `unused_input` | WARNING | 原始資料進了 job 但沒授權給任何人 |
| `orphan_output` | WARNING | 產出了但沒人讀，也不是最終報告 |

Lane state 與 transcript 被排除 —— 沒有讀者是它們的正常狀態，不是症狀。

**這些是純函式，因為 golden job 要能斷言它們。** 現在 golden job 有四條資料流健康的斷言，
拿第五次的事件流去跑會失敗（`tests/dataflow/fixtures/golden5-events.jsonl` 是回歸 fixture）。
一次幸運的人工發現不該是唯一的防線。

## 即時模式辨識「在等什麼」

一次 job 跑幾十分鐘，而「在思考」「在等限流」「已經卡死」從外面看起來一模一樣。
實測第五次跑有 **29% 的時間花在限流等待**，而當下沒有任何東西這麼說。

現在會分辨：等待限流（含後端與已等時間）、等待使用者回答、交接重啟、
無進展、收尾中、N 條 lane 執行中、orchestrator 思考中。

## 為什麼不用 rich / textual

這層的價值在**正確地投影事實**，不在畫面。TUI 框架會帶來版面複雜度與一個新的執行期相依，
換來的是視覺而非資訊。CJK 寬度自己處理（`交易` 是 4 欄不是 2 欄，`len()` 對每一個
會出現的標籤都是錯的）。

## 第三種 render，以及為什麼它仍然不引入相依

`inspect` 回答的是**派工層**的問題。而過去四次 golden 診斷的問題一個都不在那一層：
再提示回傳 schema 本身、再提示回傳裸 id、analyst 跑 13 次查詢後 `artifact: null`、
預算閘門用錯單位。答案全在 `blobs/traces/dN` 裡，而那份檔案**過去沒有任何讀者** ——
每次都是開一支臨時腳本翻完就丟。

`--html` 把流向與逐輪軌跡放進同一個畫面：左邊流向、中間軌跡（推理標記／工具呼叫／
回傳結果）、右邊帳。點一次派工，右邊兩欄跟著換；網址帶著選取，可以貼進 issue。

**單一自包檔案，零新增相依。** 不引圖表函式庫、不連字型 CDN、打開時不發任何請求
（`tests/monitor/test_viewer.py` 擋這一條）。理由跟不用 rich / textual 同源 ——
這裡唯一的「圖」是一條長條與一棵縮排的樹，D3 換到的是視覺不是資訊。

調色盤是精煉過的 ANSI 16，`inspect` 的 cyan／green／yellow／red 對應**一個沒改**。
每天看那份終端機輸出的人，不該為了看這個而在兩套符號之間翻譯。

**輸出預設寫到 stdout。** transcript 是 blob，契約上不外流；產出的頁面內含它的節錄，
所以要落檔就得明說寫去哪。

## 三件這個視圖說不出來的事

跟「授權邊不冒充讀取邊」是同一條規則：沒留存的東西，不能用相鄰的資訊補。

| 沒有的東西 | 為什麼 | 怎麼處理 |
|---|---|---|
| **推理的內容** | `_block_to_dict` 只留 `chars`，文字被刻意丟掉。而自架後端連 `chars` 都是 0（golden #24 的 21 個推理區塊**全部**是空殼） | 標推理**發生的位置**，寫明「內容未留存」；`chars: 0` 再另外寫明是空殼。把工具呼叫排成一列標成「推理過程」，就是讓一份不存在的紀錄看起來存在 |
| **呼叫與結果的配對** | `tool_use` 沒留 id，只有 result 那側有 `tool_use_id` | 照順序配，並回報配不到的數量。目前 0 個，因為沒有任何一輪同時發兩個呼叫 —— 那是這幾次執行的性質，不是格式的保證 |
| **兩個量測點之間的預算** | transcript 不帶任何 token 計數 | 左緣是**軌加量測點**，不是進度條。只有 harness 實際發出警告的那幾輪有真數字，中間不畫 |

## 新增一種異常

在 `dataflow/anomalies.py` 加一個 `_detector` 函式並掛進 `detect()`。
如果它代表交付可能不可信，嚴重度給 CRITICAL，並考慮加進 golden job 的斷言 ——
否則它只是一個給人看的提示。
