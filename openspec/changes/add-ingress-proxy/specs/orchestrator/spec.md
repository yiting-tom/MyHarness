## ADDED Requirements

### Requirement: Orchestrator 以宣告式資料遙控分類器
Orchestrator SHALL 能宣告一份 routing table，說明每一條 lane 接收哪一類資料以及
是否開放。該 routing table SHALL 是分類器唯一的依據。Orchestrator 與分類器之間
SHALL NOT 共享 context。

#### Scenario: 宣告 routing table
- **WHEN** orchestrator 更新計畫並附上 routing table
- **THEN** 系統 SHALL 記錄該 routing table
- **AND** 後續的分類 SHALL 依據它進行

#### Scenario: 未開放的 lane 不會被指派
- **WHEN** routing table 中某條 lane 標示為未開放
- **THEN** 分類 SHALL NOT 將資料指派給該 lane

#### Scenario: routing table 可被取代
- **WHEN** orchestrator 再次宣告 routing table
- **THEN** 後續的分類 SHALL 依據最新的一份

#### Scenario: 未附 routing table 不影響計畫更新
- **WHEN** orchestrator 更新計畫但未附 routing table
- **THEN** 計畫 SHALL 正常更新
- **AND** 既有的 routing table SHALL 維持不變
