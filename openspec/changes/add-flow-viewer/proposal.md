## Why

`myharness inspect` 回答的是**派工層**的問題：哪份資料被授權給誰、誰產出了什麼、
最終報告來自哪一次。那一層已經抓到過真東西 —— golden 第五次的報告來自一次
沒有任何授權的派工，逐行讀事件流看不出來，排成流向第一眼就看到。

但這一年診斷的每一個問題都**不在那一層**：

| 問題 | 診斷方式 | 在 `inspect` 裡看得到嗎 |
|---|---|---|
| 再提示回傳的是 JSON Schema 本身而不是實例（#21/#22，5 次派工中 4 次） | 手動翻 transcript | ✗ 只看到 `status=ok` |
| 再提示後 artifact 變成裸 id（#23） | 手動翻 transcript | ✗ `artifact` 欄位有值 |
| 預算閘門用錯單位：57% 時警告「只夠再 5 次請求」，下一輪就寫完了（#24） | 手動翻 transcript | ✗ |
| analyst 跑了 13 次查詢然後 `artifact: null`（#23 d1/d4） | 手動翻 transcript | ✗ 只看到 `budget_exceeded` |

**共同點：答案全都在 `blobs/traces/dN` 裡，而那份檔案今天沒有任何讀者。**
它每次派工都被完整寫下來，每次診斷都用臨時 Python 腳本翻一遍，翻完就丟。
`monitor` 的 README 說「Golden job 跑五次才通，每次診斷都是臨時腳本翻事件流」——
那句話現在只有上半句被解決了。

這個 change 要補的是**下一層**：一次派工裡，模型到底依序做了什麼 ——
在哪裡思考、呼叫了哪個工具、餵了什麼參數、拿回什麼、預算在哪一輪見底。

## What Changes

- **先做 spike，不寫 production code**（`spikes/spike29_flow_viewer.py`）：
  用**真實的 golden24** 產出一份可點的原型，驗三件事：
  1. 事件流 + transcript 這兩份既有資料，夠不夠撐起逐輪的視圖（**決策閘**）
  2. 「沒留存的東西」能不能在視覺上被誠實標示，而不是被相鄰資訊冒充
  3. 單一自包檔案（無網路、無執行期相依）是否足夠
- **新增 `monitor` 的需求**：逐輪軌跡、未留存內容的標示、流向與軌跡相連、
  預算沿軌跡可見、視覺輸出與 `--json` 必須一致、唯讀且離線。
- **不新增 capability。** 這是 `monitor` 的第三種 render（ASCII / JSON / HTML），
  不是新的邊界 —— 它讀的是同一個 `DataFlow`，偵測的是同一組異常。
  若它們會不一致，那是缺陷不是特性。
- **不改 `dataflow`、`events`、`lanes`。** 有一個例外候選見下方「已知的落差」。

## Capabilities

### New Capabilities
（無。）

### Modified Capabilities
- `monitor`: 新增逐輪軌跡視圖的義務。既有的四項需求（即時模式、事後模式、
  人機雙輸出、不影響被觀察的 job）一律不變 —— 新視圖必須同時滿足它們。

## Impact

- **Specs**: `monitor` 新增 6 條需求
- **Code**: 新增 `myharness/monitor/viewer.py` 與 `myharness inspect --html`。
  `render.py` 不動（它是終端機專用的，不該被 HTML 拖著走）
- **Deps**: **零新增**。輸出是一份自包的 HTML，不引入 web 框架、不引入圖表函式庫、
  不在執行期連網。理由與 `render.py` 的「不用 rich / textual」同源：
  這層的價值在正確地投影事實，相依換來的是視覺不是資訊
- **已知的落差（必須寫明，不是先實作）**：
  1. **推理文字沒有被留存。** `_block_to_dict` 只記 `{"type":"thinking","chars":N}`，
     文字被刻意丟掉。而在自架後端上 `chars` 恆為 0 —— 回傳的是空殼。
     所以這個視圖能顯示的是**推理的形狀**（在哪一輪發生、多長），不是推理的內容。
     視圖必須明說，**絕不能讓工具呼叫冒充推理**，一如今天授權邊不冒充讀取邊
  2. **工具結果是被截斷的。** `_excerpt` 取頭 2/3 尾 1/3。視圖顯示的不是全文，
     必須標示
  3. `artifact.read` 事件現在有了（golden24 有 16 筆），所以「授權了但沒讀」
     這件事終於可以真的畫出來，而不是只能說「不可得」
- **風險**：HTML 內嵌 transcript 全文，而 transcript 是 **blob** ——
  契約上「絕不能被讀進任何 context」。給人看的檔案不違反那條（人不是 context），
  但產出的檔案會含原始資料的片段，**不得預設寫進 job 目錄，也不得自動分享**。
  這點必須在規格裡，不是在 README 裡
