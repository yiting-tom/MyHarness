## Why

Spike #12 量到：每一次分類的 input 有 **8,991 tokens，其中 8,372 不是我寫的**。

| | tokens |
|---|---|
| 分類器的 system prompt | 126 |
| 分類器的 user prompt（最壞情況） | 493 |
| 實際計費 | **8,991** |
| **CLI 固定開銷** | **~8,372（93%）** |

`disallowed_tools` 已經生效（擋掉 31 個內建工具）。剩下的是 **Claude Code CLI
自己的 base system prompt**，那不是工具定義，`disallowed_tools` 碰不到。

對一條 60k 預算的 lane worker 來說，這是用 SDK 的已知代價，換得的是工具面、
多輪、session 管理、prompt caching。**分類器一樣都不用**：
單次、無工具、無 session、輸出一小段 JSON。

DESIGN #4 選了最便宜的 model tier，那個判斷沒錯 —— 但省的是分母裡小的那一項。

## What Changes

- 新增一條**直接呼叫後端 HTTP API** 的路徑，只給 proxy 用。
- `BackendProfile` 已經有 `base_url` 與 `auth_token_env`，這條路徑用同一組設定，
  不新增第二處後端組態。
- `classify()` 的介面不變，換掉的是它底下怎麼送請求。
  現有的 `transport` 注入點保留，離線測試不受影響。
- 兩條路徑都留著：後端沒宣告 base_url（例如 Anthropic 直連走 SDK 預設）時
  仍走 SDK，不強迫每個後端都支援。

## 不在這個 change 裡

- **Lane worker 與 orchestrator 繼續走 SDK。** 它們要工具、要多輪、要 session，
  8.4k 對 60k 預算是划算的。這個 change 只動 proxy。
- 不重做 `spikes/spike08_compare.py`（那是「整個 harness 不用 SDK」的問題，
  範圍大得多）。這裡只處理一個已經量出數字的具體點。

## Impact

- Specs: `proxy`（新增一條關於請求開銷的需求）
- Code: 新增 `myharness/proxy/direct.py`；`myharness/proxy/classify.py` 選路
- Deps: 需要一個 HTTP client。`httpx` 已隨 `claude-agent-sdk` 進來
- 風險：直接呼叫等於自己處理重試與限流。必須接上既有的 `BackendGate`，
  否則會繞過那一層已經驗證過的節流
