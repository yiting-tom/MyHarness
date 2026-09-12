# a2a-server Specification

## Purpose
把同一次分析開成第二條對外邊界，讓另一個 agent 能用 A2A 協定跑它、看進度、
取結果。它是殼不是腦：所有判斷都在 orchestrator 與 lane，這裡只負責把那些判斷
翻成 AgentCard、Task 與 Artifact。它與 MCP 邊界對同一個 job 必須給出一致的答案，
而預設不把報告全文送出去——先給摘要與章節價目表，由呼叫端決定要買哪一節。

## Requirements
### Requirement: 兩種輸出模式，且皆對外宣告
系統 SHALL 對外宣告它支援的輸出模式：**摘要與章節價目表**，以及**章節全文**。
呼叫方 SHALL 能在請求中指定其一。系統 SHALL NOT 讓其中一種模式僅以未載明的
慣例存在。

#### Scenario: 能力宣告含兩種模式
- **WHEN** 呼叫方取得這個 agent 的能力描述
- **THEN** 該描述 SHALL 載明兩種輸出模式與各自的取得方式

#### Scenario: 呼叫方指定模式
- **WHEN** 呼叫方在請求中指定其中一種輸出模式
- **THEN** 系統 SHALL 依該模式產出結果

### Requirement: 預設不外洩報告全文
未指定輸出模式時，系統 SHALL 以摘要與章節價目表作答。該回應 SHALL 含每個章節的
識別碼與其 token 估計，SHALL NOT 含章節全文。

#### Scenario: 未指定模式
- **WHEN** 呼叫方未指定輸出模式而取得一次分析的結果
- **THEN** 回應 SHALL 含摘要、重點與各章節的識別碼與 token 估計
- **AND** SHALL NOT 含任何章節的全文

#### Scenario: 價目表自帶取用指引
- **WHEN** 系統以價目表模式作答
- **THEN** 該回應 SHALL 載明如何取得章節全文
- **AND** SHALL NOT 僅提供識別碼清單

### Requirement: 全文模式仍受逐節上限約束
全文模式 SHALL 以逐節取用的方式產出，每一節 SHALL 受與逐節鑽取相同的 token 上限
約束。系統 SHALL NOT 為全文模式建立第二套內容上限。

#### Scenario: 全文由逐節組成
- **WHEN** 呼叫方要求全文模式
- **THEN** 系統 SHALL 逐節取得內容，且每節 SHALL 受既有的逐節上限約束

#### Scenario: 超過上限的章節被裁切而非略過
- **WHEN** 某一節的內容超過逐節上限
- **THEN** 該節 SHALL 被裁切並標示已裁切
- **AND** 其餘章節 SHALL 仍被產出

### Requirement: 兩條邊界對同一次分析給出一致的結果
經此邊界與經既有的工具邊界取得同一次分析的結果時，兩者的摘要與章節清單 SHALL 一致。
系統 SHALL NOT 為此邊界複製一套結果組裝邏輯。

#### Scenario: 摘要與章節清單一致
- **WHEN** 同一次已完成的分析分別經兩條邊界被查詢
- **THEN** 兩者回傳的摘要與章節識別碼清單 SHALL 相同

### Requirement: 進度串流以版本序號標記，且可自中斷處續接
每一則進度事件 SHALL 帶有該次分析當時的版本序號。呼叫方 SHALL 能在重新連線時
帶回它最後看到的序號。系統 SHALL 據此判斷呼叫方是否已落後，並 SHALL NOT 讓
連線中斷期間發生的改變無法被得知。

#### Scenario: 事件帶版本序號
- **WHEN** 系統推送一則進度事件
- **THEN** 該事件 SHALL 含當時的版本序號

#### Scenario: 重連時不漏事件
- **WHEN** 呼叫方在中斷後以它最後看到的序號重新連線，且中斷期間分析曾有實質改變
- **THEN** 系統 SHALL 使該改變仍可被得知，SHALL NOT 只推送其後的事件

#### Scenario: 逐輪的內部事件不觸發推送
- **WHEN** 分析僅產生不構成實質改變的內部事件
- **THEN** 系統 SHALL NOT 因此推送進度事件

### Requirement: 不存在、不在執行中、執行中是三種答案
系統 SHALL 分別表達這三種狀態：查無此分析、分析存在但不在此程序執行中、
分析執行中。系統 SHALL NOT 將前兩者壓成同一個回應。

#### Scenario: 查無此分析
- **WHEN** 呼叫方以一個不存在的識別碼查詢
- **THEN** 系統 SHALL 回覆查無此分析

#### Scenario: 存在但不在此程序執行中
- **WHEN** 呼叫方查詢一個由先前的程序執行、且已不在此程序中的分析
- **THEN** 系統 SHALL 回覆該分析不在執行中
- **AND** SHALL 載明其結果仍可被取得

#### Scenario: 結果查詢不依賴分析仍在執行
- **WHEN** 呼叫方對一個已由其他程序完成的分析取得結果
- **THEN** 系統 SHALL 回傳其結果

### Requirement: 拒絕以可據以行動的回應表達
此邊界的拒絕 SHALL 以呼叫方可讀的結果表達，並 SHALL 說明被拒絕的原因。
系統 SHALL NOT 以未經處理的內部錯誤作為對呼叫方的回應。

#### Scenario: 不合法的請求
- **WHEN** 呼叫方發出一個系統無法滿足的請求
- **THEN** 系統 SHALL 回覆一則說明原因的結果，SHALL NOT 回覆內部錯誤細節

#### Scenario: 併行上限
- **WHEN** 執行中的分析已達上限而呼叫方再啟動一次
- **THEN** 系統 SHALL 拒絕該請求並載明上限與執行中的數量

### Requirement: 未答的認證問題不得以預設開放的形式存在
在認證與存取控制有答案之前，此邊界 SHALL NOT 出現在系統的預設啟動路徑中，
且 SHALL 僅接受本機的連線。

#### Scenario: 預設不啟用
- **WHEN** 系統以預設設定啟動
- **THEN** 此邊界 SHALL NOT 被開啟

#### Scenario: 僅接受本機連線
- **WHEN** 此邊界被明確啟用
- **THEN** 它 SHALL 僅接受來自本機的連線

