# lane-worker Specification

## Purpose
TBD - created by archiving change add-lane-worker. Update Purpose after archive.
## Requirements
### Requirement: Lane type 與 lane instance 分離
系統 SHALL 區分 lane type 與 lane instance。Lane type 由開發者靜態宣告，
包含 charter、工具集、模型、預算與回合上限；lane instance 由執行期建立，
持有自己的 id、範圍描述與獨立的 lane state。同一個 lane type SHALL 能被
實例化為多個 instance，各自的 state 互不影響。

#### Scenario: 同型別的兩個 instance 各持有獨立 state
- **WHEN** 由同一個 lane type 建立 instance `txn-2024` 與 `txn-2023`，
  並各自執行一次任務
- **THEN** 兩者的 lane state 分屬不同 artifact，互不覆寫

#### Scenario: 拒絕未註冊的 lane type
- **WHEN** 以一個未註冊的 lane type 名稱建立 instance
- **THEN** 系統 SHALL 拒絕，並回傳已註冊的 type 清單

### Requirement: Ephemeral worker 的執行循環
每次 lane 任務 SHALL 在一個全新的 agent context 中執行，其輸入僅由
charter、該 lane 目前的 state、任務描述、以及被授權的 input artifact 組成。
Worker 結束後其 context SHALL 被丟棄；跨任務的連續性僅透過 lane state 傳遞。

#### Scenario: 前一次任務的對話不影響下一次
- **WHEN** 同一個 lane 連續執行兩次任務，且第一次的對話中出現了某個
  未寫入 lane state 的細節
- **THEN** 第二次執行的輸入中 SHALL NOT 含有該細節

#### Scenario: Lane state 提供跨任務的連續性
- **WHEN** 第一次任務將某個結論寫入 lane state，之後同一 lane 再次執行
- **THEN** 第二次執行的輸入中 SHALL 含有該結論

#### Scenario: 首次執行時沒有既有 state
- **WHEN** 一個 lane instance 第一次執行任務
- **THEN** 執行 SHALL 正常進行，並在結束時建立該 lane 的初始 state

### Requirement: Lane state 的上限與寫入安全
Lane state SHALL 有明確的 token 上限。超過上限時系統 SHALL 拒絕該次 state 寫入
並將此事記錄為降級，而非默默截斷。State 的寫入 SHALL 使用 compare-and-set，
使並行寫入造成的覆寫失敗得可被偵測。

#### Scenario: 超過上限的 state 寫入被拒絕
- **WHEN** worker 產生的新 lane state 超過設定的 token 上限
- **THEN** 系統 SHALL 保留舊的 state，並在該次執行的結果中記錄此降級

#### Scenario: 並行寫入被偵測
- **WHEN** 兩個 worker 以相同的起始 revision 寫入同一個 lane state
- **THEN** 後者 SHALL 失敗，且該次執行回報為降級而非成功

### Requirement: Handle 契約由機制強制
Worker 的回傳值 SHALL 是一個符合固定 schema 的 handle，含 artifact 引用、
一行摘要、信心程度，並可含量化指標與後續建議。此契約 SHALL 由後端的結構化輸出
機制強制；後端不支援時 SHALL 退回應用層驗證。回傳給呼叫端的 handle
SHALL 由程式碼保證其大小上界，不得由模型的自制力決定。

#### Scenario: 被要求寫長文時仍只回傳 handle
- **WHEN** 任務描述明確要求 worker 產出一份長篇報告
- **THEN** 回傳給呼叫端的 handle 仍 SHALL 符合 schema 且不超過大小上界
- **AND** 長篇內容 SHALL 存在於 artifact 中而非 handle 內

#### Scenario: 過長的欄位被截斷而非放行
- **WHEN** worker 產生的一行摘要超過允許長度
- **THEN** 系統 SHALL 截斷該欄位並標記已截斷，而非原樣回傳

#### Scenario: 不合 schema 的輸出觸發重試後失敗
- **WHEN** 後端不支援結構化輸出，且 worker 連續產生不符 schema 的輸出
- **THEN** 系統 SHALL 在達到重試上限後回傳一個結構化的失敗 handle

### Requirement: 失敗是值而非例外
`run_lane_worker` SHALL NOT 因為語意層級的失敗而拋出例外。超出 token 預算、
工具持續失敗、回合數用盡、state 寫入被拒，SHALL 各自轉換為帶有明確 status
的 handle，並盡可能附上部分結果與後續建議，交由呼叫端決定如何處置。

#### Scenario: 超出預算回傳部分結果
- **WHEN** worker 在完成任務前耗盡 token 預算
- **THEN** 回傳的 handle 的 status SHALL 表示預算耗盡
- **AND** SHALL 包含執行過程中已產生的部分結果的引用與後續建議

#### Scenario: 回合數用盡不拋例外
- **WHEN** worker 用盡允許的回合數仍未產出 handle
- **THEN** 呼叫端 SHALL 收到一個 status 表示回合耗盡的 handle，而非例外

#### Scenario: 工具持續失敗被歸因
- **WHEN** worker 使用的工具反覆失敗導致任務無法完成
- **THEN** 回傳的 handle SHALL 說明失敗的工具與原因，並建議替代路徑

### Requirement: Transient 錯誤由 framework 處理
暫時性錯誤（速率限制、伺服器錯誤、網路中斷）SHALL 由 framework 以有上限的
退避重試處理，不得回報給呼叫端。語意層級的失敗 SHALL NOT 被自動重試。

#### Scenario: 速率限制被靜默重試
- **WHEN** 後端回傳速率限制錯誤，且重試後成功
- **THEN** 呼叫端 SHALL 收到成功的 handle，且不感知該次重試

#### Scenario: 語意失敗不被重試
- **WHEN** worker 因超出預算而失敗
- **THEN** framework SHALL NOT 重新執行該次任務

#### Scenario: 重試耗盡後成為失敗值
- **WHEN** transient 錯誤持續超過重試上限
- **THEN** 呼叫端 SHALL 收到一個表示後端不可用的 handle，而非例外

### Requirement: 執行過程可完整重現
每次 worker 執行 SHALL 將其完整的訊息序列保存為 transcript，並在回傳的 handle
與事件流中引用之。Ephemeral worker 結束後其 context 即消失，transcript 是
事後理解「這條 lane 到底看到什麼、做了什麼」的唯一依據。

#### Scenario: 成功執行留下 transcript
- **WHEN** 一次 worker 執行成功結束
- **THEN** 對應的 transcript SHALL 可被讀取，且含該次執行的訊息序列

#### Scenario: 失敗執行同樣留下 transcript
- **WHEN** 一次 worker 執行因超出預算而失敗
- **THEN** transcript SHALL 仍被保存，且含失敗前已產生的訊息

### Requirement: 執行寫入事件流
每次 worker 執行 SHALL 在開始與結束時寫入事件，結束事件含 status、產出的
artifact、token 用量、回合數、成本與 transcript 引用，使成本歸屬與 context
紀律可被事後稽核。

#### Scenario: 成功執行的事件含成本與用量
- **WHEN** 一次 worker 執行成功結束
- **THEN** 事件流中對應的結束事件含 status、artifact、tokens、turns、usd 與 transcript

#### Scenario: 失敗執行同樣寫入結束事件
- **WHEN** 一次 worker 執行以降級狀態結束
- **THEN** 事件流中 SHALL 有對應的結束事件，且其 status 反映該降級

### Requirement: Worker 的可讀範圍受授權限制
Worker 執行期間的 artifact 讀取 SHALL 限於自己的 lane namespace 加上該次任務
明確授權的 input。Worker SHALL NOT 能存取未被授權的其他 lane 的產出。

#### Scenario: 讀取被授權的 input
- **WHEN** 任務的 input 含另一條 lane 的產出，worker 讀取之
- **THEN** 讀取成功

#### Scenario: 讀取未授權的 artifact 失敗
- **WHEN** worker 嘗試讀取未列於 input 且不屬於自己 namespace 的 artifact
- **THEN** 讀取 SHALL 失敗，且該失敗對 worker 可見以便其調整做法

