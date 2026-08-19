# dataflow Specification

## Purpose
TBD - created by archiving change add-dataflow-monitor. Update Purpose after archive.
## Requirements
### Requirement: 資料流由事件流推導，不另行記錄
資料流模型 SHALL 完全由既有的事件流與 artifact index 推導。系統 SHALL NOT 為此
新增任何寫入路徑。若某個資料流事實無法從現有事件推導，該缺口 SHALL 在事件型別上補齊，
而非以第二份紀錄繞過。

#### Scenario: 僅需事件流即可建構
- **WHEN** 對一份既有的事件流建構資料流模型
- **THEN** 建構過程 SHALL NOT 需要事件流與 artifact index 以外的任何輸入

#### Scenario: 推導過程不寫入
- **WHEN** 建構資料流模型
- **THEN** 事件流 SHALL NOT 因此增加任何事件

### Requirement: 節點與邊涵蓋完整的流向
資料流模型 SHALL 以節點表示資料（原始資料、分析產出、報告）與處理者（lane），
以邊表示三種關係：**授權**（哪份資料被允許給哪次執行）、**產出**（哪次執行寫出了什麼）、
**讀取**（哪次執行實際讀了什麼）。授權與讀取 SHALL 分開表示，因為兩者不一致
正是最需要被看見的情況。

#### Scenario: 一次派工的三種邊
- **WHEN** 一次派工被授權讀取兩份資料，實際讀了一份，並產出一份分析
- **THEN** 模型中 SHALL 有兩條授權邊、一條讀取邊、一條產出邊

#### Scenario: 來源鏈可回溯
- **WHEN** 查詢一份報告的來源
- **THEN** SHALL 能沿產出與授權邊回溯到最初的原始資料

#### Scenario: 未被授權的資料不出現授權邊
- **WHEN** 某份資料存在於 job 中但從未被列入任何派工的授權清單
- **THEN** 該節點 SHALL 存在但沒有授權邊

### Requirement: 偵測無授權卻有產出
系統 SHALL 偵測「某次執行未被授權任何輸入，卻產出了分析結果」的情況。
Golden job 中一次沒有授權的 synthesis 執行寫出了報告並覆蓋掉正確的版本 ——
這在逐行的事件輸出中看不出來。

#### Scenario: 空授權的產出被標記
- **WHEN** 某次派工的授權清單為空，且該次執行產出了 artifact
- **THEN** SHALL 回報一項異常，含該次派工、lane 與產出

#### Scenario: 有授權的產出不被標記
- **WHEN** 某次派工被授權了輸入並產出 artifact
- **THEN** SHALL NOT 回報此項異常

### Requirement: 偵測產出被覆蓋
系統 SHALL 偵測「同一個 artifact 被多次派工寫入」的情況，並指出最終版本由哪一次派工產生。
最終交付來自哪一次執行，是判斷交付是否可信的前提。

#### Scenario: 覆蓋被標記且指出勝出者
- **WHEN** 兩次派工先後寫入同一個 artifact id
- **THEN** SHALL 回報一項覆蓋異常，並指出最終版本來自後者

#### Scenario: 各自寫入不同 artifact 不算覆蓋
- **WHEN** 兩次派工寫入不同的 artifact id
- **THEN** SHALL NOT 回報覆蓋異常

### Requirement: 偵測未被使用的資料與產出
系統 SHALL 偵測兩種浪費：進入 job 但從未被授權給任何 lane 的原始資料，
以及被產出但從未被任何後續執行讀取、也未成為最終報告的分析。

#### Scenario: 未被授權的原始資料
- **WHEN** 一份原始資料進入 job 但未出現在任何授權清單中
- **THEN** SHALL 回報為未使用

#### Scenario: 無人讀取的分析產出
- **WHEN** 一份分析產出未被任何後續派工授權，且不是最終報告
- **THEN** SHALL 回報為孤兒產出

#### Scenario: 最終報告不算孤兒
- **WHEN** 一份產出是 job 的最終報告
- **THEN** SHALL NOT 回報為孤兒

### Requirement: 成本與 token 可歸屬到資料流
資料流模型 SHALL 能回答「產生這份報告總共花了多少」——
沿來源鏈加總所有貢獻該報告的執行的成本與 token。

#### Scenario: 報告的累計成本
- **WHEN** 查詢一份報告的來源鏈成本
- **THEN** SHALL 回傳該鏈上所有派工的成本加總

#### Scenario: 未貢獻的執行不計入
- **WHEN** 某次派工的產出不在該報告的來源鏈上
- **THEN** 其成本 SHALL NOT 計入該報告

### Requirement: 部分或損壞的事件流仍可推導
資料流模型 SHALL 能從執行中的 job（事件流尚未結束）或含未知事件型別的事件流建構，
不得因此失敗。監控執行中的 job 是這一層的主要用途之一。

#### Scenario: 執行中的 job
- **WHEN** 對一個只有派工開始、尚無派工結束的事件流建構模型
- **THEN** SHALL 成功建構，且該次派工 SHALL 標示為進行中

#### Scenario: 未知事件型別
- **WHEN** 事件流中含有模型不認識的事件型別
- **THEN** SHALL 忽略之並正常建構

