## ADDED Requirements

### Requirement: Lane worker 能對被授權的表格資料執行查詢
Lane worker SHALL 能對其被授權的 blob 執行 SQL 查詢並取得結果。查詢 SHALL 以
artifact id 指名資料來源，SHALL NOT 接受檔案路徑。系統 SHALL 依既有的授權集合
（`GrantSet`）判定每一個被指名的 artifact，判定 SHALL 與其他讀取路徑使用同一套規則。

#### Scenario: 查詢被授權的 blob
- **WHEN** worker 對一個在其授權集合內的 blob 下 SQL 查詢
- **THEN** 系統 SHALL 回傳查詢結果
- **AND** 回應 SHALL 載明每個 artifact 被綁定的表名

#### Scenario: 查詢未授權的 blob
- **WHEN** worker 指名一個不在其授權集合內的 artifact
- **THEN** 系統 SHALL 拒絕該查詢
- **AND** SHALL NOT 讀取該 artifact 的任何內容

#### Scenario: SQL 不得成為第二條存取路徑
- **WHEN** 查詢的 SQL 內文指涉檔案系統、網路位置或未被指名的資料來源
- **THEN** 該指涉 SHALL 失敗
- **AND** worker SHALL NOT 因此取得任何未授權的資料

### Requirement: 查詢引擎在執行使用者 SQL 期間與外界隔離
系統 SHALL 在執行 worker 提供的 SQL 之前關閉查詢引擎的外部存取，並 SHALL 鎖定
組態使該關閉無法於同一次執行中被還原。缺少其中任一者 SHALL 視為未滿足本需求。

#### Scenario: 外部存取被關閉
- **WHEN** worker 的 SQL 嘗試讀取任何檔案、目錄、網路位置或掛載外部資料庫
- **THEN** 該嘗試 SHALL 失敗

#### Scenario: 組態無法被還原
- **WHEN** worker 的 SQL 嘗試變更查詢引擎組態以重新開啟外部存取
- **THEN** 該嘗試 SHALL 失敗

#### Scenario: 不得載入擴充套件
- **WHEN** worker 的 SQL 嘗試安裝或載入查詢引擎擴充套件
- **THEN** 該嘗試 SHALL 失敗

### Requirement: 查詢僅接受單一唯讀敘述
系統 SHALL 拒絕包含多於一個 SQL 敘述的查詢，並 SHALL 拒絕非唯讀的敘述。
系統 SHALL NOT 依賴查詢引擎執行此限制。

#### Scenario: 多敘述被拒絕
- **WHEN** worker 提交包含兩個以上 SQL 敘述的查詢
- **THEN** 系統 SHALL 拒絕整個查詢
- **AND** SHALL NOT 執行其中任何一個敘述

#### Scenario: 非唯讀敘述被拒絕
- **WHEN** worker 提交的敘述會建立、修改或刪除資料
- **THEN** 系統 SHALL 拒絕該查詢

### Requirement: 查詢結果受列數與字元數兩道上限約束
系統 SHALL 同時以列數上限與字元數上限約束回傳給 worker 的查詢結果。滿足其中一項
SHALL NOT 免除另一項。當結果被任一上限截斷時，回應 SHALL 明示截斷已發生。

#### Scenario: 列數超限
- **WHEN** 查詢結果的列數超過上限
- **THEN** 回應 SHALL 僅含上限內的列
- **AND** SHALL 明示結果已被截斷

#### Scenario: 列數在限內但內容過長
- **WHEN** 查詢結果的列數未超過上限，但其文字表述超過字元上限
- **THEN** 回應 SHALL 被裁切至字元上限內
- **AND** SHALL 明示裁切已發生

### Requirement: 大量結果以新 artifact 交付而非進入 context
系統 SHALL 提供將查詢結果完整寫成新 blob artifact 的方式。使用該方式時，
回應 SHALL 僅含 artifact id 與其摘要性描述，SHALL NOT 含結果資料本身。
新產生的 artifact SHALL 落在該 lane 既有的授權範圍內，SHALL NOT 引入新的授權來源。

#### Scenario: 結果寫成 artifact
- **WHEN** worker 要求將查詢結果寫成新 artifact
- **THEN** 系統 SHALL 建立該 artifact
- **AND** 回應 SHALL NOT 包含結果的資料列

#### Scenario: 產出的 artifact 可被同一條 lane 再次查詢
- **WHEN** worker 對自己稍早以此方式產出的 artifact 下查詢
- **THEN** 該查詢 SHALL 通過授權檢查

### Requirement: Worker 能在寫查詢前取得資料結構
系統 SHALL 提供取得被授權 blob 之欄位名稱、欄位型別與列數的方式，並 SHALL
一併提供少量樣本列。樣本列 SHALL 受與查詢結果相同的上限約束。

#### Scenario: 取得結構
- **WHEN** worker 要求一個被授權 blob 的結構
- **THEN** 回應 SHALL 含欄位名稱、欄位型別與列數
- **AND** SHALL 含該 blob 綁定的表名

### Requirement: 資料量與執行時間有上限且拒絕時說明原因
系統 SHALL 對可載入查詢引擎的 blob 大小設上限，並 SHALL 對單次查詢的執行時間設上限。
超過任一上限時系統 SHALL 拒絕或中止，並 SHALL 回覆可據以改變作法的說明。

#### Scenario: blob 過大
- **WHEN** 被指名的 blob 超過大小上限
- **THEN** 系統 SHALL 拒絕該查詢
- **AND** 回應 SHALL 說明上限值

#### Scenario: 查詢逾時
- **WHEN** 查詢的執行時間超過上限
- **THEN** 系統 SHALL 中止該查詢
- **AND** SHALL 回報逾時而非讓 worker 無限等待

### Requirement: 查詢失敗以可據以行動的訊息回覆
查詢失敗 SHALL 以文字結果回覆 worker 而非以例外中斷執行。回覆 SHALL 含足以讓
worker 在下一回合改正的資訊。

#### Scenario: SQL 有誤
- **WHEN** worker 提交的 SQL 無法執行
- **THEN** 系統 SHALL 回覆錯誤說明與當次可用的表名
- **AND** worker 的執行 SHALL NOT 因此中止

### Requirement: 本地化路徑在 worker 執行期間保持有效
以本地路徑形式交給 worker 的 blob，其路徑 SHALL 在該 worker 執行結束前保持可讀。
系統 SHALL NOT 在交付路徑後、worker 使用它之前釋放該路徑所指的資源。

#### Scenario: 路徑於交付後仍可讀
- **WHEN** worker 取得一個 blob 的本地路徑
- **THEN** 該路徑 SHALL 在此次 worker 執行期間持續可讀
