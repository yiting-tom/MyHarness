## ADDED Requirements

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
