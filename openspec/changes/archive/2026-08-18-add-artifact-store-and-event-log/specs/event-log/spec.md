## ADDED Requirements

### Requirement: Append-only 的結構化事件流
每個 job SHALL 擁有一份 append-only 的事件流，每筆事件為一行 JSON。
事件 SHALL NOT 被就地修改或刪除。事件流 SHALL 是 job 內發生了什麼的唯一事實來源。

#### Scenario: 事件依序附加
- **WHEN** 依序寫入三筆事件
- **THEN** 讀回時的順序與寫入順序一致

#### Scenario: 不允許就地修改
- **WHEN** 檢視事件流的寫入介面
- **THEN** 該介面 SHALL 只提供附加操作，不提供更新或刪除既有事件的操作

### Requirement: 事件的共通欄位
每一筆事件 SHALL 含有事件型別 `t`、單調遞增的序號、UTC 時間戳、以及所屬 `job_id`。
序號 SHALL 在單一 job 內唯一且連續，使事件可被穩定引用與比對。

#### Scenario: 共通欄位齊備
- **WHEN** 寫入任一型別的事件
- **THEN** 該筆事件含 `t`、`seq`、`ts`、`job_id` 四個欄位

#### Scenario: 序號連續
- **WHEN** 在一個 job 內寫入 N 筆事件
- **THEN** 其 `seq` 為 0 至 N-1 且無重複

### Requirement: 涵蓋 job 生命週期的事件型別
事件流 SHALL 至少涵蓋以下型別：job 開始與結束、計畫更新、資料進入、proxy 路由決策、
dispatch 開始與結束、artifact 讀取、context 用量、以及向使用者提問與其回覆。

#### Scenario: dispatch 結束事件含成本與 token
- **WHEN** 一次 dispatch 結束
- **THEN** 對應事件含 `status`、產出的 artifact id、`tokens`（輸入與輸出）、
  `turns`、`usd`、以及該次執行的 transcript 檔案引用

#### Scenario: proxy 路由事件含決策理由
- **WHEN** proxy 對一筆進入的資料做出路由決策
- **THEN** 對應事件含來源 payload 的 artifact id、選定的 lane、決策理由、
  所用模型與 token 用量

### Requirement: Context 用量事件
每當 orchestrator 或任一 worker 的 context 用量發生變化時，SHALL 寫入一筆 context 事件，
記錄角色、已用 token 數、以及占其上限的比例。此事件是驗證 harness 的 context 紀律
是否真的有效的唯一依據。

#### Scenario: 記錄 orchestrator 用量
- **WHEN** orchestrator 完成一個 turn
- **THEN** 事件流中出現一筆 context 事件，含角色 `orchestrator`、`used`、`pct`

#### Scenario: 可查得整個 job 的 context 峰值
- **WHEN** 對已完成 job 的事件流查詢 orchestrator 的 context 峰值
- **THEN** 回傳該 job 中 `used` 的最大值

### Requirement: 聚合查詢介面
事件流 SHALL 提供讀取與聚合介面，至少支援：依型別過濾、依 lane 分組加總成本與 token、
取得 context 峰值、以及列出所有失敗與降級事件。成本報表、除錯介面與監控輸出
SHALL 皆為此事件流的投影，而非另行記錄的第二份事實。

#### Scenario: 依 lane 聚合成本
- **WHEN** 對事件流查詢各 lane 的成本加總
- **THEN** 回傳以 lane 為鍵、USD 加總為值的結果

#### Scenario: 列出降級與失敗
- **WHEN** 查詢 job 中所有非成功結束的 dispatch
- **THEN** 回傳其 lane、status 與建議處置

### Requirement: 交付物的 caveats 由事件流推導
最終報告的「未做到什麼」SHALL 由 framework 從事件流自動蒐集（超預算的 lane、
逾時未答的提問、被跳過的 payload、失敗的 dispatch），SHALL NOT 依賴 LLM 主動記得申報。

#### Scenario: 超預算的 lane 進入 caveats
- **WHEN** 某 lane 以 `budget_exceeded` 結束，而 job 正常收工
- **THEN** 自動產生的 caveats 清單中含該 lane 及其未完成範圍

#### Scenario: 逾時未答的提問進入 caveats
- **WHEN** 一則 `ask_user` 提問逾時並套用預設值
- **THEN** caveats 中記錄該假設未經使用者確認

### Requirement: 事件流可作為回歸斷言的來源
事件流 SHALL 足以支撐對 golden job 的自動化斷言，至少包含 context 峰值上界、
無重複 dispatch、總成本上界、以及 job 正常收工。

#### Scenario: 對 golden job 下斷言
- **WHEN** 以固定輸入執行一個 golden job 並取得其事件流
- **THEN** 可從事件流判定 context 峰值、重複 dispatch 次數、總成本與最終狀態

### Requirement: 寫入的耐久性
事件寫入 SHALL 以整行為單位落盤，不得出現半行或交錯損毀的紀錄。程序異常結束時，
已寫入的事件 SHALL 仍可完整讀回。

#### Scenario: 中途中止仍可讀回
- **WHEN** 寫入若干事件後程序被強制中止
- **THEN** 事件流可被完整解析，且不含損毀的部分行
