## ADDED Requirements

### Requirement: Per-lane 的後端設定
系統 SHALL 允許每一個 lane type 指定自己的 backend profile，包含 endpoint、
金鑰來源、模型識別與模型別名映射。不同 lane SHALL 能在同一個 job 內
使用不同的後端與模型，且彼此的設定不互相污染。

#### Scenario: 兩條 lane 使用不同後端
- **WHEN** 一個 lane type 設定為 Anthropic 直連，另一個設定為 OpenRouter，
  兩者在同一 job 內各執行一次
- **THEN** 各自的請求 SHALL 送往其設定的 endpoint

#### Scenario: 金鑰不寫在設定中
- **WHEN** 定義一個 backend profile
- **THEN** 其金鑰 SHALL 以環境變數名稱引用，而非明文值

#### Scenario: 缺少金鑰時明確失敗
- **WHEN** 某個 backend profile 引用的環境變數不存在
- **THEN** 系統 SHALL 在執行前以明確訊息失敗，指出缺少哪個變數

### Requirement: 內建工具的裁切
每一個 lane type SHALL 明確宣告其需要的工具，系統 SHALL 將未宣告的內建工具
從請求中移除，而非僅僅阻止其被呼叫。工具定義佔用的 token 是每個 ephemeral
worker 都要重複支付的固定成本，必須由設定控制而非預設承擔。

#### Scenario: 未宣告的工具不出現在請求中
- **WHEN** 一個 lane type 只宣告需要自訂工具
- **THEN** 送出的請求中 SHALL NOT 含未宣告的內建工具定義

#### Scenario: 裁切後的固定成本顯著低於預設
- **WHEN** 比較裁切前後的請求
- **THEN** 裁切後的工具定義 token 數 SHALL 顯著低於預設值

### Requirement: Backend capability 的宣告與降級
Backend profile SHALL 宣告其支援的能力，至少涵蓋結構化輸出、prompt caching
與 API 端預算。系統 SHALL 依宣告選擇強制路徑或降級路徑，並在事件流中記錄
實際採用的路徑，使「這次執行的契約是被強制還是被祈禱」可被事後查明。

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

### Requirement: 模型別名映射
系統 SHALL 支援將邏輯模型別名映射到各後端的實際模型識別，使 lane type
可以用能力層級（如「強」「中」「便宜」）描述需求，而不必綁定特定供應商的
模型名稱。

#### Scenario: 同一別名在不同後端解析為不同模型
- **WHEN** 兩個 backend profile 對同一個別名設定不同的實際模型識別
- **THEN** 各自解析出其設定的模型

#### Scenario: 未映射的別名明確失敗
- **WHEN** lane type 使用一個該 backend 未映射的別名
- **THEN** 系統 SHALL 在執行前失敗，並列出該 backend 可用的別名
