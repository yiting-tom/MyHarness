## ADDED Requirements

### Requirement: 執行期間逐步寫入事件流
Worker SHALL 在執行**期間**、而非結束時，為每一輪模型回應與每一批工具結果
各寫入一筆步驟事件。步驟事件 SHALL 含：所屬派工、第幾次嘗試、第幾輪、
呼叫的工具名稱與有界長度的參數摘錄、每個工具結果的字元數與是否出錯、
以及當下已花掉的預算與其占上限的比例。

步驟事件 SHALL NOT 含推理文字或工具結果的內容。步驟事件是高頻事件，
SHALL NOT 喚醒等待 job 變化的客戶端，也 SHALL NOT 占用任何為低頻事件設計的
有限視窗（例如進度摘要的最近事件、判斷「在等什麼」的尾端事件）。

#### Scenario: 執行中就看得到工具呼叫
- **WHEN** 一次派工仍在執行，且模型已經呼叫了一個工具
- **THEN** 事件流中 SHALL 已有一筆步驟事件，含該工具的名稱與參數摘錄

#### Scenario: 結果只記大小
- **WHEN** 一個工具回傳了結果
- **THEN** 對應的步驟事件 SHALL 含該結果的字元數與是否出錯，SHALL NOT 含其內容

#### Scenario: 不喚醒 long-poll
- **WHEN** 一筆步驟事件被寫入
- **THEN** 等待 job 變化的客戶端 SHALL NOT 因此被喚醒

#### Scenario: 不擠掉限流狀態
- **WHEN** 一條 lane 正在等限流，而另一條 lane 在此期間寫入多筆步驟事件
- **THEN** monitor SHALL 仍回報「等待限流」
