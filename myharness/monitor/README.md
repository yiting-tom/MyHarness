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
myharness jobs                 # 有哪些 job 可以看
myharness inspect <job>        # 事後：資料流、異常、成本歸屬
myharness inspect <job> --json # 同上，機器可讀
myharness monitor <job>        # 即時：現在在做什麼
```

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

## 新增一種異常

在 `dataflow/anomalies.py` 加一個 `_detector` 函式並掛進 `detect()`。
如果它代表交付可能不可信，嚴重度給 CRITICAL，並考慮加進 golden job 的斷言 ——
否則它只是一個給人看的提示。
