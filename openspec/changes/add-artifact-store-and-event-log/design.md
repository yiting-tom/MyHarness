## Context

MyHarness 是一個以 `claude-agent-sdk` 建構、對外以 MCP server 形式提供的多 agent
資料分析 harness。整體架構與 17 項既有決策記錄於專案根目錄的 `DESIGN.md`，
可行性驗證結果記錄於 `spikes/RESULTS.md`。

本 change 只做其中不含 LLM 的地基層：artifact store 與 event log。專案目前為
greenfield，除設計文件與 spike 腳本外沒有任何實作程式碼。

三項來自既有決策的硬約束：

1. **不變式**：原始資料不可能進入 orchestrator 的 context。本層負責讓「資料進入 context」
   這件事在程式碼層面沒有路徑可走，而非靠 prompt 約束。
2. **後端遷移**：本地 FS + SQLite 為 v1，未來要換 MinIO + MariaDB/Oracle。
   若第一天不抽象，lane 工具會全部綁死在本地路徑上。
3. **可觀測性即產品**：多 agent 系統無法靠讀 log 除錯。event log 是除錯、成本報表、
   回歸測試與未來監控輸出的共同來源，而不是事後附加的功能。

## Goals / Non-Goals

**Goals:**
- 讓「blob 被讀入 context」在程式碼上不可能發生，而非不建議發生。
- 讓 lane worker 的可讀範圍完全由 dispatch 的授權清單決定，且可從呼叫參數完整推導。
- 讓後端替換只需新增一個實作類別，呼叫端零修改；以同一組合約測試驗證所有實作。
- 讓 golden job 的 context 峰值、成本與行為可被自動化斷言。
- 全層可在無網路、無 LLM、無外部服務的情況下完整單元測試。

**Non-Goals:**
- 不實作 orchestrator、proxy、lane worker 或任何 MCP 工具（後續 change）。
- 不實作 MinIO / MariaDB / Oracle 後端，只確保介面足以承載它們。
- 不做跨 job 的 artifact 共享（id 已預留，行為留待日後）。
- 不做即時 TUI、OpenTelemetry 匯出或成本儀表板（皆為事件流的投影，之後再加）。
- 不做事件流的壓縮、輪替或保留策略。

## Decisions

### D1. Blob 與 note 是兩個型別，不是同一抽象的兩種大小

**選擇**：`read_note` 遇到 blob 時直接拒絕，並回傳 schema 與建議的工具型存取方式。

**替代方案**：統一抽象，過大時自動呼叫 summarizer 壓縮後回傳。**否決**，因為它會
默默燒掉大量 token、失真程度不可控，而且把一個應該在編譯期就不可能的錯誤，
變成一個執行期的昂貴意外。拒絕會讓錯誤立刻顯性化。

### D2. 授權以 dispatch 的 `inputs` 為唯一來源

**選擇**：worker 的 grant set = 自己的 namespace + dispatch 明確列出的 id。
Store 在每次讀取時檢查。

**替代方案**：任何 lane 可讀任何 note。**否決**，因為它讓資料流無法從呼叫參數推導 ——
而在多 agent 系統中，「這條 lane 到底看過什麼」是唯一可行的除錯起點。
額外好處：失控的 lane 撈不到不該碰的東西，synthesizer 也不需要特權，
它只是一個被授權讀 N 個 note 的普通 lane。

### D3. `est_tokens` 存在 index，預檢發生在讀取之前

**選擇**：寫入時計算並存入 index；`read_note` 先查 index 再決定要不要讀。

**理由**：讀了才發現太大，token 已經花掉了。預檢讓「超額」變成一個零成本的錯誤。
估算方式先用簡單的字元數比例，介面上保留替換成精確 tokenizer 的空間 ——
精確度在此不重要，重要的是「事前」。

### D4. `localize()` 是 context manager，不是回傳路徑的函式

**選擇**：`async with store.localize(blob_id) as path:`；本地後端回傳真實路徑且零複製，
物件儲存後端下載到 scratch 並在離開時清理。

**理由**：lane 的工具（DuckDB、pandas、PDF 解析）吃的是路徑，不可能為了換後端改寫每一個。
用 context manager 而非裸函式，是因為清理時機必須明確 —— 回傳路徑的函式無法表達
「用完了」，而物件儲存後端一定需要這個訊號。**現在加是二十行，之後補是重寫所有工具。**

### D5. Index 用 SQLite 但不用 ORM

**選擇**：手寫 SQL、以 repository 物件封裝。

**理由**：未來要換 MariaDB/Oracle。ORM 會讓遷移時的方言差異藏在抽象底下難以發現，
明確的 SQL 反而讓需要修改的地方一目了然。查詢種類少（依 id 查、依 job 列舉、
依 kind 過濾），不值得引入相依。

### D6. 目錄配置是介面的一部分，但只有 store 知道

**選擇**：`jobs/<job_id>/{blobs,lanes,traces,events.jsonl,index.sqlite}` 的配置由本地
實作獨佔，任何其他模組不得以路徑存取 artifact。

**理由**：這是 D5/D4 能成立的前提。若其他模組開始拼路徑，後端抽象就只是裝飾。
合約測試中應包含一條靜態檢查：非 store 模組不得出現 artifact 路徑拼接。

### D7. Event log 是 JSONL，且是唯一事實來源

**選擇**：每個 job 一份 append-only 的 `events.jsonl`，逐行 flush。
成本報表、TUI、OpenTelemetry、回歸斷言全部是它的讀取投影。

**替代方案**：直接寫 SQLite 或直接接 OpenTelemetry。**否決**，因為 JSONL 可以直接 `cat`、
可以整包打包給人除錯、不需要跑 collector、且解析成本近乎零。上 OTel 時再寫一個
event → span 的 mapper 即可，那是純加法。

### D8. Caveats 由事件流推導，不由 LLM 申報

**選擇**：報告的「未做到什麼」由 framework 掃事件流自動蒐集。

**理由**：LLM 最會忘記說的就是自己沒做到什麼。這件事必須由程式碼保證。
這也反過來約束了事件型別的設計 —— 每一種降級都必須留下可被機器辨識的事件。

## Risks / Trade-offs

- **`est_tokens` 估算不準** → 用保守係數（寧可高估），並在 index 保留欄位供日後改用
  精確 tokenizer；預檢的價值在「事前」而非「精確」。
- **授權檢查被繞過**：日後某個 lane 工具直接開檔案 → 以合約測試中的靜態檢查（D6）
  阻擋，並在 code review 中把「模組內出現 artifact 路徑拼接」視為缺陷。
- **同一 lane 平行寫 `state.md` 造成覆寫** → 本 change 提供 compare-and-set 語意的寫入，
  由上層以「同一 lane 序列化執行」來使用；平行化留待後續 change。
- **JSONL 在長 job 下變大** → v1 不做輪替。以 job 為單位分檔已能限制單檔規模，
  真的成為問題時再加，不影響介面。
- **抽象過早**：目前只有一個後端，介面可能設計得不合實際需求 → 以「MinIO + MariaDB
  是已知的既定目標」為依據，只抽象已知會變的部分（儲存位置、本地化、index 查詢），
  不抽象尚未有第二個案例的部分。
- **事件型別提前定死** → 事件以 `t` 字串區分並允許附加欄位，讀取端對未知型別寬容，
  新增型別為純加法。

## Migration Plan

專案為 greenfield，無既有資料需要遷移。

未來換到 MinIO + MariaDB/Oracle 的路徑：新增實作類別 → 以同一組合約測試驗證 →
以設定切換。既有 job 目錄不做自動搬遷（job 為短生命週期，舊 job 留在本地即可）。

## Open Questions

- `est_tokens` 的估算係數要用哪個值？先用 4 chars/token 的保守估計，
  待 golden job 累積實際 usage 資料後校準。
- `state.md` 的 stable / working 分區是由 store 理解（結構化欄位）還是純文字慣例
  （由 charter 約束）？傾向後者以保持 store 單純，但若壓縮策略需要程式介入則要改。
- 事件流是否需要 schema 版本欄位？傾向需要，但可在第一次破壞性變更時才加入。
