## MODIFIED Requirements

### Requirement: Backend capability 的宣告與降級
Backend profile SHALL 宣告其支援的能力，至少涵蓋結構化輸出、prompt caching、
API 端預算與**成本回報**。系統 SHALL 依宣告選擇強制路徑或降級路徑，並在事件流中
記錄實際採用的路徑，使「這次執行的契約是被強制還是被祈禱」可被事後查明。

#### Scenario: 支援結構化輸出時走強制路徑
- **WHEN** backend 宣告支援結構化輸出
- **THEN** handle 契約 SHALL 由後端機制強制，且事件記錄採用了強制路徑

#### Scenario: 不支援時退回應用層驗證
- **WHEN** backend 宣告不支援結構化輸出
- **THEN** 系統 SHALL 解析並驗證 worker 輸出，不符時重新提示，
  且事件記錄採用了降級路徑

#### Scenario: 不支援 API 端預算時以本地計數硬斷
- **WHEN** backend 宣告不支援 API 端預算
- **THEN** 系統 SHALL 以本地累計的 token 用量在超出上限時中止該次執行

#### Scenario: 不宣告成本回報時金額不得用於控制決定
- **WHEN** backend 未宣告支援成本回報
- **THEN** 系統 SHALL NOT 以累計金額作為停止或收工的依據

#### Scenario: 宣告成本回報時金額上限照常生效
- **WHEN** backend 宣告支援成本回報
- **THEN** 系統 SHALL 依累計金額執行金額上限
