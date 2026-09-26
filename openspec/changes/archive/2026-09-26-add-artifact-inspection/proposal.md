## Why

即時監看頁畫出了資料流，但圖上的每一個產出都只是一個名字。使用者要的是
**點下去看裡面寫了什麼，以及它每一次被改寫時新增、刪除了什麼**。

golden #26 裡這件事正好重要：`analyst1:txn-2024-analysis` 被 d1 寫成 2,531 字，
又被 d3 改寫成 839 字 —— 監看頁標出了 `overwritten_output`，卻沒辦法讓人看到
**被刪掉的那 1,700 字是什麼**。`critique` 與 `report` 也各在同一次派工內被寫了兩次。

存放區只保留每份 note 的最新內容。但每一次 `write_finding` / `update_state`
的完整文字都在那次派工的逐輪紀錄裡（工具呼叫的輸入不截斷）—— 所以版本史
可以**從紀錄重建**，不需要改存放區，也不需要猜。

同時發現一個缺口：一條 lane 寫了但沒放進 handle 的產出（#26 的 d3 寫了
`txn-2024-finance-correction`），不在 `dispatch.end` 裡，所以圖上根本沒有它。
逐輪紀錄裡有這次寫入。

## What Changes

- **`GET /artifact?id=<artifact id>`**：一份 artifact 的內容與版本史。
  - note：目前內容；由逐輪紀錄重建的每一個版本（哪次派工、第幾次寫入、字數）；
    每一版相對前一版新增與刪除的行。
  - blob：大小、欄位、可以當文字讀的前幾行；二進位格式就說是二進位。
  - 版本史不完整時說為什麼：執行中的派工逐輪紀錄還不存在；
    存放區的內容與紀錄中最後一版不同。
- **`/state` 帶上由逐輪紀錄得到的寫入**，圖上補畫 handle 沒回報、但確實寫了的產出。
- **圖上的資料與產出節點可點**；被寫過不只一次的標出「改寫 ×N」。
  側欄顯示內容、版本切換、以及 +/− 的差異。

## Capabilities

### Modified Capabilities
- `monitor`: 產出可檢視，版本與差異由紀錄重建

## Impact

- **Specs**: `monitor` +2
- **Code**: 新增 `myharness/monitor/content.py`；`serve.py` 加一條唯讀路徑；`live.html`
- **成本**：逐輪紀錄在派工結束後不再變動，解析結果依檔案大小與修改時間快取
