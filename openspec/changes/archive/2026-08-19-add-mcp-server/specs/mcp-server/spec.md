## ADDED Requirements

### Requirement: 分析以非阻塞方式啟動
系統 SHALL 提供啟動一次分析的工具，該工具 SHALL 在分析完成前返回，並 SHALL
回傳一個可用於後續查詢的識別碼。系統 SHALL NOT 要求客戶端在單一呼叫中等待分析完成。

#### Scenario: 啟動後立即返回
- **WHEN** 客戶端啟動一次分析
- **THEN** 系統 SHALL 在分析完成之前返回
- **AND** 回應 SHALL 含該次分析的識別碼與其當前狀態

#### Scenario: 分析在背景繼續
- **WHEN** 啟動呼叫已返回
- **THEN** 該次分析 SHALL 繼續執行

#### Scenario: 背景失敗不得無聲消失
- **WHEN** 背景執行中的分析以例外結束
- **THEN** 該結果 SHALL 被記錄為此次分析的最終狀態
- **AND** 後續查詢 SHALL 能取得該狀態

### Requirement: 併行分析數有上限
系統 SHALL 限制同時執行的分析數量。達到上限時系統 SHALL 拒絕新的啟動請求，
並 SHALL 說明當前上限與執行中的數量。

#### Scenario: 超過上限
- **WHEN** 執行中的分析已達上限且客戶端再啟動一次
- **THEN** 系統 SHALL 拒絕該請求
- **AND** 回應 SHALL 含上限值與執行中的數量

### Requirement: 進度查詢等待狀態改變而非固定時間
系統 SHALL 提供查詢分析進度的工具，該工具 SHALL 支援一個等待上限。在等待期間，
系統 SHALL 於狀態發生實質改變時立即返回。系統 SHALL NOT 以固定間隔輪詢作為實作。
等待逾時 SHALL NOT 視為錯誤。

#### Scenario: 狀態改變時立即返回
- **WHEN** 客戶端以等待上限查詢進度，且等待期間分析狀態發生實質改變
- **THEN** 系統 SHALL 在該改變發生後返回，而非等到上限用盡

#### Scenario: 無事發生時逾時返回
- **WHEN** 等待期間沒有實質改變
- **THEN** 系統 SHALL 在等待上限到達時返回
- **AND** 回應 SHALL 表示分析仍在進行，SHALL NOT 表示錯誤

#### Scenario: 已結束的分析立即返回
- **WHEN** 客戶端查詢一個已結束的分析
- **THEN** 系統 SHALL 立即返回，SHALL NOT 等待

### Requirement: 回傳給客戶端的內容受上限約束
系統回傳給客戶端的每一份內容 SHALL 受長度上限約束。進度查詢 SHALL 回傳有界的
進度摘要，SHALL NOT 回傳事件流本身。結果查詢 SHALL 回傳摘要與各章節的 token
估計，SHALL NOT 回傳報告全文。

#### Scenario: 進度摘要不含事件流
- **WHEN** 一次分析已產生大量事件
- **THEN** 進度查詢的回應長度 SHALL 不隨事件數量成比例增長

#### Scenario: 結果回傳章節價目表
- **WHEN** 客戶端查詢已完成分析的結果
- **THEN** 回應 SHALL 含每個章節的識別碼與其 token 估計
- **AND** SHALL NOT 含章節全文

#### Scenario: 逐節鑽取
- **WHEN** 客戶端指定一個章節識別碼要求全文
- **THEN** 系統 SHALL 僅回傳該章節
- **AND** 該章節內容 SHALL 受 token 上限約束

### Requirement: 結果查詢不依賴分析仍在執行
結果查詢與章節鑽取 SHALL 僅依據事件流與 artifact store 作答，因此 SHALL 能回答
由先前的 process 執行過的分析。

#### Scenario: 跨 process 取得結果
- **WHEN** 客戶端查詢一個由已結束的 process 執行完成的分析
- **THEN** 系統 SHALL 回傳其結果

#### Scenario: 不在執行中與不存在是兩件事
- **WHEN** 客戶端對一個存在但已不在執行中的分析進行進度查詢或回答問題
- **THEN** 系統 SHALL 回覆該分析不在執行中
- **AND** SHALL NOT 回覆查無此分析

### Requirement: 客戶端能回答分析提出的問題
系統 SHALL 讓客戶端取得執行中分析的待答問題，並 SHALL 提供回答的方式。
回答 SHALL 送達提出該問題的分析。

#### Scenario: 待答問題出現在進度中
- **WHEN** 執行中的分析提出一個問題
- **THEN** 該問題 SHALL 出現在進度查詢的回應中

#### Scenario: 回答送達
- **WHEN** 客戶端回答一個待答問題
- **THEN** 提出該問題的分析 SHALL 收到該回答

#### Scenario: 回答一個不存在的問題
- **WHEN** 客戶端回答的問題識別碼不存在
- **THEN** 系統 SHALL 回覆該識別碼無效，SHALL NOT 靜默接受

### Requirement: 客戶端能補充資料且該資料不進入任何 context
系統 SHALL 讓客戶端在分析進行中補充資料。補充的資料 SHALL 存成 blob artifact，
SHALL NOT 進入 orchestrator 或客戶端的 context。系統 SHALL 通知該次分析有新資料可用。

#### Scenario: 補充的資料成為 blob
- **WHEN** 客戶端補充一份資料
- **THEN** 系統 SHALL 將其存成 blob artifact
- **AND** 回應 SHALL 含該 artifact 的識別碼，SHALL NOT 含資料內容

#### Scenario: 未經路由要明說
- **WHEN** 系統未判定該資料應交給哪一條 lane
- **THEN** 回應 SHALL 明示該資料未經路由

### Requirement: 工具失敗以可據以行動的訊息回覆
對外工具的失敗 SHALL 以結果內容回覆客戶端，SHALL NOT 以例外中斷。回覆 SHALL 含
足以讓客戶端改正的資訊。

#### Scenario: 識別碼不存在
- **WHEN** 客戶端使用一個不存在的分析識別碼
- **THEN** 系統 SHALL 回覆該識別碼不存在
- **AND** 呼叫 SHALL NOT 以例外結束

#### Scenario: 結果尚未產生
- **WHEN** 客戶端查詢一個仍在執行中的分析的結果
- **THEN** 系統 SHALL 說明結果尚未產生與當前狀態
