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
myharness monitor <job>                 # 即時：現在在做什麼
```

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
