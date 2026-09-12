# proxy Specification

## Purpose
資料進入 harness 的那一關：由分類器判斷它可能屬於哪條 lane。
判斷只是建議——分類器不派工也不授權，看得到的只有 routing table 與一段有界樣本。
分類失敗不能影響資料落地，而它自己的花費要單獨記帳。

## Requirements
### Requirement: 進入的資料由分類器判斷歸屬
系統 SHALL 在資料進入 job 時判斷它屬於哪一條 lane。判斷 SHALL 依據 orchestrator
發布的 routing table。判斷結果 SHALL 記錄為事件，並 SHALL 傳達給 orchestrator。

#### Scenario: 比對到一條 lane
- **WHEN** 進入的資料符合 routing table 中某條開放的 lane
- **THEN** 系統 SHALL 記錄該資料歸屬於該 lane
- **AND** 該判斷 SHALL 附上判斷理由

#### Scenario: 比對不到任何 lane
- **WHEN** 進入的資料不符合 routing table 中任何開放的 lane
- **THEN** 系統 SHALL 記錄該資料未被路由
- **AND** SHALL NOT 指派給任意一條 lane

#### Scenario: 沒有 routing table
- **WHEN** orchestrator 尚未發布任何 routing table
- **THEN** 系統 SHALL 記錄該資料未被路由
- **AND** SHALL NOT 因此失敗

### Requirement: 分類器不派工也不授權
分類器 SHALL NOT 派發任何任務。分類器 SHALL NOT 使任何 lane 取得讀取該資料的權限。
資料的授權來源 SHALL 維持不變。

#### Scenario: 分類不會產生執行
- **WHEN** 分類器判定資料屬於某條 lane
- **THEN** 系統 SHALL NOT 因此派發任務

#### Scenario: 分類不會產生授權
- **WHEN** 分類器判定資料屬於某條 lane，而該 lane 隨後在未被授權該資料的情況下執行
- **THEN** 該 lane 讀取該資料 SHALL 被拒絕

### Requirement: 分類器只看得到 routing table 與有界樣本
分類器的輸入 SHALL 僅包含 routing table、資料的中繼資料與一份有界的內容樣本。
分類器 SHALL NOT 取得 job 的計畫、目標或任何分析產出。

#### Scenario: 計畫與目標不進入分類器
- **WHEN** 為一份進入的資料建構分類器的輸入
- **THEN** 該輸入 SHALL NOT 包含 job 的計畫內容
- **AND** SHALL NOT 包含 job 的目標敘述

#### Scenario: 樣本受上限約束
- **WHEN** 進入的資料遠大於樣本上限
- **THEN** 分類器取得的樣本 SHALL 同時受行數與字元數上限約束

#### Scenario: 樣本經由既有的儲存介面取得
- **WHEN** 系統為分類取用資料內容
- **THEN** 該取用 SHALL 透過既有的 artifact 儲存介面進行

### Requirement: 分類失敗不影響資料落地
資料的儲存 SHALL NOT 依賴分類成功。分類逾時、發生錯誤或回傳無效結果時，
系統 SHALL 將該資料記錄為未路由並繼續，SHALL NOT 拒絕該次資料進入。

#### Scenario: 分類逾時
- **WHEN** 分類在時間上限內沒有完成
- **THEN** 資料 SHALL 仍然完成儲存
- **AND** 系統 SHALL 記錄該資料未路由

#### Scenario: 分類回傳不存在的 lane
- **WHEN** 分類結果指向一條不存在或未開放的 lane
- **THEN** 系統 SHALL 視同未路由
- **AND** SHALL NOT 指派給該 lane

#### Scenario: 未路由的三種原因可分辨
- **WHEN** 資料未被路由
- **THEN** 系統 SHALL 能分辨其原因為「沒有分類器」「分類失敗」或「分類器判定無歸屬」

### Requirement: 分類的花費獨立歸屬
分類產生的 token 與金錢花費 SHALL 記錄為分類器自身的花費，SHALL NOT 計入被判定的
那條 lane。

#### Scenario: 花費不落在被路由到的 lane 上
- **WHEN** 分類器判定資料屬於某條 lane 並產生花費
- **THEN** 該花費 SHALL 歸屬於分類器

### Requirement: 分類請求不攜帶執行框架的固定開銷
分類是單次、無工具、無 session 的請求。當後端支援直接呼叫時，系統 SHALL 以直接
請求執行分類，SHALL NOT 附帶代理執行框架的系統提示與工具定義。分類請求的
input token 數 SHALL 與其自身的提示長度同一量級。

#### Scenario: 直接路徑的請求只含自己的提示
- **WHEN** 後端宣告了可直接呼叫的位址而系統執行一次分類
- **THEN** 該請求的內容 SHALL 僅包含分類器的系統提示、routing table 與樣本

#### Scenario: 不支援直接呼叫的後端仍可分類
- **WHEN** 後端未宣告可直接呼叫的位址
- **THEN** 系統 SHALL 仍能完成分類

#### Scenario: 兩條路徑的結果形狀相同
- **WHEN** 同一份資料分別經由直接路徑與框架路徑分類
- **THEN** 兩者回傳的結果 SHALL 具有相同的結構與欄位

### Requirement: 直接路徑仍受既有的節流與重試治理
直接呼叫 SHALL 使用與其他後端呼叫相同的節流機制。系統 SHALL NOT 為分類建立
第二套獨立的限流或重試策略。

#### Scenario: 分類請求經過節流閘
- **WHEN** 後端正處於冷卻狀態而系統要執行分類
- **THEN** 該分類請求 SHALL 等待冷卻結束或依既有規則放棄

#### Scenario: 放棄時仍降級為未路由
- **WHEN** 節流或重試最終放棄
- **THEN** 資料 SHALL 仍完成儲存並記錄為未路由

