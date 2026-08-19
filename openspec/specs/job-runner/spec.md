# job-runner Specification

## Purpose
TBD - created by archiving change add-orchestrator. Update Purpose after archive.
## Requirements
### Requirement: 派工非阻塞，收割單次阻塞
`dispatch` SHALL 立即返回一個任務識別，實際執行在背景進行；
`await_tasks` SHALL 以單一阻塞呼叫等待指定任務完成並回傳其 handle。
這個切分的理由是 LLM 沒有 await：若派工阻塞，平行度就取決於後端是否併發執行
同一輪的多個工具呼叫；若收割用輪詢，每次空轉都是一整個 orchestrator turn 的代價。

#### Scenario: 派工立即返回
- **WHEN** orchestrator 連續派出三項工作
- **THEN** 三次呼叫 SHALL 都在實際執行完成前返回，各自帶一個任務識別

#### Scenario: 背景任務真正並行
- **WHEN** 三項工作被派出後以單次收割等待
- **THEN** 三者的執行區間 SHALL 重疊

#### Scenario: 收割等到全部完成
- **WHEN** 以「全部」模式收割三個任務
- **THEN** 呼叫 SHALL 在三者皆完成後返回，並回傳三個 handle

#### Scenario: 收割可只等任一完成
- **WHEN** 以「任一」模式收割
- **THEN** 呼叫 SHALL 在第一個任務完成時返回，其餘 SHALL 繼續在背景執行

#### Scenario: 收割逾時回報未完成者
- **WHEN** 收割在指定時限內未能等到全部完成
- **THEN** SHALL 回傳已完成者的 handle 與仍在執行者的識別，而非拋出例外

### Requirement: 重複派工不執行
同一個 job 內，內容相同的派工第二次出現時 SHALL NOT 被實際執行，
而是立即回傳前一次的結果與提示。反覆派同一項工作是 LLM 在失敗時最常見的行為，
而攔下它的成本是零。

#### Scenario: 相同派工回傳前次結果
- **WHEN** 以完全相同的 lane 與任務內容派工第二次
- **THEN** SHALL 立即回傳標示為重複的結果，內含前一次的 handle
- **AND** SHALL NOT 產生新的 lane 執行

#### Scenario: 內容不同即非重複
- **WHEN** 對同一 lane 派出內容不同的任務
- **THEN** 該派工 SHALL 正常執行

### Requirement: Job 級硬上限與善終
每個 job SHALL 有派工次數、金額與時間的硬上限。任一觸頂時，系統 SHALL 通知
orchestrator 立即以現有產出收工，而非直接中止。使用者拿到「基於已完成部分的初步結論
加未完成清單」，遠勝於拿到一個空的 job。

#### Scenario: 觸頂時要求收工
- **WHEN** 任一硬上限被觸及
- **THEN** orchestrator SHALL 收到一則要求立即收工的訊息
- **AND** SHALL 仍能派出產生報告所需的最後一次工作

#### Scenario: 善終後仍有交付
- **WHEN** job 因觸頂而收工
- **THEN** SHALL 仍產出報告，且未完成的部分 SHALL 列於已知限制

#### Scenario: 拒絕收工後才中止
- **WHEN** orchestrator 在被要求收工後仍繼續派工
- **THEN** 系統 SHALL 在寬限額度用盡後中止該 job 並自行產出降級交付

### Requirement: 無進展偵測
連續多次派工均未產生新的分析產出時，系統 SHALL 判定該 job 已無進展，
並升級為向使用者提問或強制收工。

#### Scenario: 連續無產出觸發升級
- **WHEN** 連續數次派工皆未產生新的分析產出
- **THEN** 系統 SHALL 記錄無進展並要求 orchestrator 改變做法或收工

#### Scenario: 有產出即重置
- **WHEN** 其間任一次派工產生了新的分析產出
- **THEN** 無進展計數 SHALL 歸零

### Requirement: 向使用者提問是抽象通道
向使用者提問 SHALL 透過一個抽象通道表達，使 orchestrator 不依賴任何特定的傳輸方式。
提問 SHALL 有逾時與預設值，且 SHALL 有 job 級的次數配額。
逾時或配額耗盡時，SHALL 套用預設值並記錄為未經確認的假設。

#### Scenario: 提問取得回覆後繼續
- **WHEN** orchestrator 提問且使用者在時限內回覆
- **THEN** orchestrator SHALL 收到該回覆並繼續執行

#### Scenario: 逾時套用預設並記錄
- **WHEN** 提問逾時
- **THEN** SHALL 套用預設值，且該假設 SHALL 出現在最終交付的已知限制中

#### Scenario: 配額耗盡後不再提問
- **WHEN** 提問次數已達 job 配額
- **THEN** 後續提問 SHALL 立即以預設值返回，並提示配額已耗盡

### Requirement: Job 狀態可被外部查詢
Job 的狀態 SHALL 可由外部查詢，涵蓋執行階段、進度、待回覆的提問與最終交付，
且查詢結果的大小 SHALL 有上界。此介面是日後對外服務層的唯一依據。

#### Scenario: 查詢回傳有界的狀態
- **WHEN** 查詢一個執行中的 job
- **THEN** SHALL 回傳其階段與進度，且結果大小不隨已完成的工作量成長

#### Scenario: 待回覆的提問出現在狀態中
- **WHEN** orchestrator 正在等待使用者回覆
- **THEN** 狀態中 SHALL 含該提問

