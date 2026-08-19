# orchestrator Specification

## Purpose
TBD - created by archiving change add-orchestrator. Update Purpose after archive.
## Requirements
### Requirement: Orchestrator 的工具面是固定且極小的
Orchestrator SHALL 只能透過一組固定的工具操作系統，且該組工具中
SHALL NOT 存在任何能讓原始資料進入其 context 的路徑。工具面的大小
SHALL 由程式碼決定，不隨 lane 數量或資料量成長。

#### Scenario: 沒有讀取 blob 的路徑
- **WHEN** 檢視 orchestrator 可用的全部工具
- **THEN** 其中 SHALL NOT 有任何工具能回傳 blob 的內容

#### Scenario: 工具數量不隨規模變化
- **WHEN** 比較一個 3 條 lane 的 job 與一個 20 條 lane 的 job
- **THEN** orchestrator 可用的工具數量相同

### Requirement: Orchestrator 規劃而不彙整
Orchestrator SHALL 負責決定要建立哪些 lane、派什麼任務、以及何時收工。
最終報告 SHALL 由一個被授權讀取相關產出的 synthesis lane 撰寫，
orchestrator SHALL NOT 親自將多份完整分析讀入自己的 context 來寫報告。

#### Scenario: 報告由 synthesis lane 產出
- **WHEN** orchestrator 決定收工並產出報告
- **THEN** 報告 SHALL 由一次 lane 執行寫入 artifact
- **AND** orchestrator 收到的 SHALL 只是指向該報告的 handle

#### Scenario: Synthesis lane 只讀被授權的產出
- **WHEN** synthesis lane 被派工
- **THEN** 其可讀範圍 SHALL 限於 orchestrator 明確列出的 artifact

### Requirement: Peek 有 job 級的總預算
Orchestrator 窺看細節的能力 SHALL 受一個 job 級的 token 總預算限制。
預算耗盡後，窺看請求 SHALL 被拒絕並提示改派 lane 處理。
此預算存在的理由是它是 orchestrator context 中變異最大的一項，
不設限則整層的上界只是估計而非保證。

#### Scenario: 單次窺看扣減預算
- **WHEN** orchestrator 成功窺看一段內容
- **THEN** 該 job 的剩餘窺看預算 SHALL 減少對應的 token 數

#### Scenario: 預算耗盡後拒絕
- **WHEN** 剩餘預算不足以支應一次窺看
- **THEN** 該請求 SHALL 被拒絕，並回傳剩餘預算與替代做法

#### Scenario: 拒絕不影響其他工具
- **WHEN** 窺看預算已耗盡
- **THEN** orchestrator SHALL 仍能派工、收割與收工

### Requirement: 計畫是可續跑的外部狀態
Orchestrator SHALL 將其全局狀態維護於一份外部的計畫產物中，內容涵蓋目標、
已確認的結論、決策與理由、lane 狀態與開放問題。計畫 SHALL 足以讓一個
全新的 orchestrator 接手而不需要原本的對話。

#### Scenario: 計畫足以接手
- **WHEN** 從一份既有計畫啟動一個全新的 orchestrator
- **THEN** 它 SHALL 知道目標、已完成與未完成的工作、以及既有的決策

#### Scenario: 計畫隨進度更新
- **WHEN** 一次派工完成
- **THEN** 計畫中該 lane 的狀態 SHALL 反映其結果

### Requirement: Context 逼近上限時交接重啟
當 orchestrator 的 context 用量達到設定比例時，系統 SHALL 要求它先把計畫更新到
足以讓接手者無縫接續，再以全新 context 從該計畫重啟。重啟 SHALL 是有交接的，
不得是失憶。

#### Scenario: 達到門檻時觸發交接
- **WHEN** orchestrator 的 context 用量達到設定比例
- **THEN** 它 SHALL 收到一則要求更新計畫以便交接的訊息

#### Scenario: 重啟後承接未完成的工作
- **WHEN** 交接完成並以全新 context 重啟
- **THEN** 新的 orchestrator SHALL 看到既有計畫與尚未收割的任務

#### Scenario: 正常規模的 job 不觸發
- **WHEN** 一個 job 的 context 用量始終低於門檻
- **THEN** SHALL NOT 發生重啟

### Requirement: 失敗 handle 由 orchestrator 處置
Lane 回傳的失敗 handle SHALL 交由 orchestrator 決定後續 —— 縮小範圍重派、
改派其他 lane、接受部分結果、或標記為報告的已知限制。系統 SHALL NOT
代替它自動重試語意失敗。

#### Scenario: 超預算的失敗可被縮小範圍重派
- **WHEN** 某 lane 回傳預算耗盡的 handle，orchestrator 以更小範圍重派
- **THEN** 新的派工 SHALL 被執行，且與前一次不視為重複

#### Scenario: 失敗可被接受為已知限制
- **WHEN** orchestrator 選擇不重派而直接收工
- **THEN** 該失敗 SHALL 出現在最終交付的已知限制中

