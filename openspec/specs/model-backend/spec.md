# model-backend Specification

## Purpose
TBD - created by archiving change add-lane-worker. Update Purpose after archive.
## Requirements
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

### Requirement: 每個後端共享的節流閘
同一個 backend 上的所有 worker SHALL 通過一個共享的節流閘，而非各自獨立重試。
當任一 worker 觀察到速率限制時，該 backend 進入冷卻，**所有**後續要求該 backend 的
worker SHALL 等待冷卻結束，而不是各自去撞一次才發現。節流閘 SHALL 同時限制該
backend 的並行請求數。

#### Scenario: 一個 worker 觸發的冷卻對所有 worker 生效
- **WHEN** 某個 worker 在 backend A 上遇到速率限制而進入冷卻
- **AND** 另一個 worker 隨後要在 backend A 上執行
- **THEN** 後者 SHALL 先等待冷卻結束才送出請求

#### Scenario: 不同後端的冷卻互不影響
- **WHEN** backend A 進入冷卻
- **THEN** 使用 backend B 的 worker SHALL 不受影響

#### Scenario: 並行請求數受限
- **WHEN** 同時有超過設定上限的 worker 要在同一個 backend 上執行
- **THEN** 超出的部分 SHALL 排隊等待，而非同時送出

### Requirement: 重試以時間預算為界，且帶隨機抖動
Transient 重試 SHALL 由一個明確的時間預算界定，而非僅由次數界定 ——
速率限制的恢復時間以分鐘計，固定次數的短退避只會在還沒恢復時就放棄。
退避 SHALL 為指數成長並加入隨機抖動，使同時被拒絕的多個 worker 不會同步重試。

#### Scenario: 短暫限流在時間預算內恢復
- **WHEN** 後端短暫回速率限制，並在時間預算內恢復
- **THEN** worker SHALL 成功完成，且呼叫端不感知該次等待

#### Scenario: 超過時間預算後成為失敗值
- **WHEN** 速率限制持續超過設定的時間預算
- **THEN** worker SHALL 回傳表示後端不可用的 handle，並註明已等待的時間

#### Scenario: 退避帶抖動
- **WHEN** 連續計算多次退避時間
- **THEN** 相同重試次數下的等待時間 SHALL NOT 完全相同

### Requirement: 節流事件寫入事件流
進入冷卻、等待冷卻、以及因限流而放棄，SHALL 各自寫入事件，使「這個 job 有多少時間
花在等待限流」可被事後量化。若沒有這些事件，限流造成的延遲會被誤認為模型很慢。

#### Scenario: 冷卻被記錄
- **WHEN** 某個 backend 因速率限制進入冷卻
- **THEN** 事件流中出現一筆節流事件，含 backend 名稱、觸發原因與冷卻長度

#### Scenario: 等待時間可被加總
- **WHEN** 查詢一個 job 的節流事件
- **THEN** 可得出該 job 因限流而等待的總時間

