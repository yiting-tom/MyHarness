## MODIFIED Requirements

### Requirement: Job 級硬上限與善終
每個 job SHALL 有派工次數與時間的硬上限，並 SHALL 在金額可被信賴時另有金額上限。
任一觸頂時，系統 SHALL 通知 orchestrator 立即以現有產出收工，而非直接中止。
使用者拿到「基於已完成部分的初步結論加未完成清單」，遠勝於拿到一個空的 job。

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

#### Scenario: 沒有金額上限時仍然有界
- **WHEN** 一個 job 在沒有金額上限的情況下執行
- **THEN** 派工次數與時間上限 SHALL 仍然生效
- **AND** 該 job SHALL 仍會在其中之一觸頂時收工

## ADDED Requirements

### Requirement: 缺席的金額上限必須可見
當一個 job 在沒有金額上限的情況下執行時，交付的已知限制 SHALL 載明這件事，
並 SHALL 載明實際把關的是哪些上限。系統 SHALL NOT 讓「沒有上限」與
「有上限而未觸頂」在交付上無法區分。

#### Scenario: 沒有金額上限時交付會說
- **WHEN** 一個 job 在沒有金額上限的情況下完成
- **THEN** 交付的已知限制 SHALL 含一則說明沒有金額上限的項目
- **AND** 該項目 SHALL 載明仍然生效的上限

#### Scenario: 有金額上限而未觸頂時不加註
- **WHEN** 一個 job 有金額上限且未觸及
- **THEN** 交付 SHALL NOT 因此加註任何已知限制

### Requirement: 金額上限不適用時，呼叫端的指定不使其復活
後端未宣告成本回報時，金額上限 SHALL NOT 生效，且 SHALL NOT 因呼叫端明確指定
而生效。在這種後端上，累計金額是一個不對應實際計費的數字；依它做停止決定
SHALL 被視為錯誤，無論該上限由誰設定。

#### Scenario: 明確指定也不生效
- **WHEN** 呼叫端明確指定了金額上限，而後端未宣告成本回報
- **THEN** 系統 SHALL NOT 依累計金額收工
- **AND** 交付的已知限制 SHALL 載明沒有金額上限
