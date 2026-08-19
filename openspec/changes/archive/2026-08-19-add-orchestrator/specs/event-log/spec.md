## ADDED Requirements

### Requirement: 計畫與預算的事件
事件流 SHALL 涵蓋計畫更新、窺看預算扣減、job 級上限觸頂、無進展判定、
以及 context 交接重啟，使「orchestrator 為什麼這樣決定」與「這個 job 為什麼收工」
可從事件流重建，而不需要保留 orchestrator 的對話。

#### Scenario: 收工原因可從事件流判定
- **WHEN** 一個 job 因觸及硬上限而收工
- **THEN** 事件流中 SHALL 有對應的觸頂事件，且可據以判定收工原因

#### Scenario: 窺看預算的使用可被追蹤
- **WHEN** orchestrator 多次窺看細節
- **THEN** 事件流 SHALL 足以算出該 job 用掉的窺看預算總量

#### Scenario: 交接重啟被記錄
- **WHEN** orchestrator 因 context 逼近上限而交接重啟
- **THEN** 事件流中 SHALL 有對應事件，含觸發時的用量
