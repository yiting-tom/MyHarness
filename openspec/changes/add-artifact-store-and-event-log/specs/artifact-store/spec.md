## ADDED Requirements

### Requirement: Blob 與 Note 的型別二分
Artifact store SHALL 將每一筆 artifact 標記為 `blob` 或 `note` 兩種 kind 之一，
並 SHALL 拒絕任何將 blob 內容讀入 LLM context 的請求。Blob 僅能透過以檔案路徑
為介面的工具存取；note 為 LLM 產出的文字，可在額度內讀入 context。

#### Scenario: 讀取 note 成功
- **WHEN** 呼叫端對一筆 kind 為 `note` 的 artifact 呼叫 `read_note`
- **THEN** store 回傳其文字內容

#### Scenario: 拒絕把 blob 讀入 context
- **WHEN** 呼叫端對一筆 kind 為 `blob` 的 artifact 呼叫 `read_note`
- **THEN** store SHALL 拒絕該請求，並回傳一個結構化錯誤，內含該 blob 的
  `kind`、`bytes`、已知 `schema`、以及建議改用的工具型存取方式
- **AND** SHALL NOT 讀取或回傳該 blob 的任何內容位元組

### Requirement: 讀取前的 token 預檢
Index SHALL 為每一筆 note 保存 `est_tokens`。`read_note` SHALL 在實際讀取內容**之前**
先以 index 的 `est_tokens` 與呼叫端給定的 `max_tokens` 比較，超出者直接拒絕。

#### Scenario: 超出額度時在讀取前拒絕
- **WHEN** 呼叫端以 `max_tokens=2000` 讀取一筆 `est_tokens=6200` 的 note
- **THEN** store SHALL 拒絕並回傳該 note 的 `est_tokens` 與可用的 section 清單
- **AND** SHALL NOT 從儲存後端讀取該 note 的內容

#### Scenario: 分段讀取可通過預檢
- **WHEN** 呼叫端指定 `section` 且該 section 的 `est_tokens` 未超過 `max_tokens`
- **THEN** store 僅回傳該 section 的內容

### Requirement: Capability-based 讀取授權
每一次 lane worker 的執行 SHALL 在一個 grant set 之下進行，該 grant set 由
「該 lane 自己的 namespace」與「dispatch 呼叫明確列出的 artifact id」兩者組成。
Store SHALL 拒絕 grant set 以外的任何讀取。

#### Scenario: 讀取自己 namespace 內的 artifact
- **WHEN** lane `txn-2024` 的 worker 讀取 `<job>/lanes/txn-2024/state`
- **THEN** store 允許讀取

#### Scenario: 讀取被明確授權的外部 artifact
- **WHEN** dispatch 的 inputs 含 `<job>/lanes/kyc-docs/findings/001`，且該 lane worker 讀取它
- **THEN** store 允許讀取

#### Scenario: 拒絕未授權的跨 lane 讀取
- **WHEN** lane `txn-2024` 的 worker 讀取未被授權的 `<job>/lanes/kyc-docs/state`
- **THEN** store SHALL 拒絕並回傳授權錯誤
- **AND** SHALL NOT 洩漏該 artifact 是否存在以外的任何內容

### Requirement: Blob 的本地路徑物化
Store SHALL 提供一個 context manager，將任一 blob 物化為本地檔案系統路徑，
供既有的檔案導向工具（DuckDB、pandas、PDF 解析等）直接使用，並在離開時清理
本次物化所建立的暫存資源。此介面 SHALL 在本地後端與物件儲存後端下具有相同語意。

#### Scenario: 本地後端零複製
- **WHEN** 在本地 FS 後端下對一筆 blob 呼叫 `localize`
- **THEN** 回傳該 blob 既有的真實路徑，且不複製檔案

#### Scenario: 離開時清理暫存
- **WHEN** 在需要下載的後端下 `localize` 區塊正常結束或因例外離開
- **THEN** 本次物化建立的暫存檔 SHALL 被移除

### Requirement: 全域唯一的 artifact id
Artifact id SHALL 採 `<job_id>/<kind>/<name>` 形式並在全域唯一，即使 v1 的存取範圍
限定於單一 job。此設計使日後開放跨 job 引用時不需變更定址。

#### Scenario: id 含 job 範圍
- **WHEN** 在 job `j7` 中寫入名為 `raw/txns-2024` 的 blob
- **THEN** 其 id 為 `j7/blob/raw/txns-2024`

#### Scenario: 拒絕跨 job 存取
- **WHEN** job `j8` 的 worker 讀取 id 前綴為 `j7/` 的 artifact
- **THEN** store SHALL 拒絕（v1 為 job-scoped）

### Requirement: 後端可替換的儲存介面
Store SHALL 以抽象介面定義，並提供本地 FS + SQLite 實作。呼叫端 SHALL NOT
依賴任何本地檔案系統的細節（路徑組成、目錄存在與否、SQLite 專有語法）。

#### Scenario: 呼叫端不觸碰檔案系統
- **WHEN** 檢視任何非 store 實作的模組
- **THEN** 該模組 SHALL NOT 直接以檔案路徑存取 artifact，只透過 store 介面

#### Scenario: 同一組測試可驗證多個後端
- **WHEN** 對介面撰寫的合約測試套件套用到任一實作
- **THEN** 該測試套件 SHALL 在不修改的前提下通過

### Requirement: 寫入時記錄索引中繼資料
寫入任一 artifact 時，store SHALL 於 index 中記錄 `kind`、`bytes`、
`est_tokens`（note）、`schema`（blob，可為 null）、`produced_by`、`created_at`。

#### Scenario: 寫入 note 後可查得 est_tokens
- **WHEN** 寫入一筆 note
- **THEN** index 中該 id 的 `est_tokens` 為非負整數，且 `produced_by` 記錄產生者

#### Scenario: 寫入 blob 後記錄大小
- **WHEN** 寫入一筆 blob
- **THEN** index 中該 id 的 `bytes` 等於實際位元組數，且 `kind` 為 `blob`
