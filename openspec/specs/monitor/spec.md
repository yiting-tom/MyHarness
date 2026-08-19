# monitor Specification

## Purpose
TBD - created by archiving change add-dataflow-monitor. Update Purpose after archive.
## Requirements
### Requirement: 即時模式顯示 job 正在做什麼
即時模式 SHALL 跟蹤一個執行中的 job 並持續顯示：目前階段、進行中與已完成的派工、
累計成本與 token、context 用量、以及**目前正在等待什麼**。
一次 job 可能執行數十分鐘，而「在思考」「在等限流」「已經卡死」需要能被區分開。

#### Scenario: 顯示進行中的派工
- **WHEN** 一個 job 有兩次派工進行中、一次已完成
- **THEN** 輸出 SHALL 同時顯示三者及其狀態

#### Scenario: 區分等待與運算
- **WHEN** job 正在等待限流冷卻
- **THEN** 輸出 SHALL 明確顯示正在等待限流及已等待的時間

#### Scenario: 新事件出現時更新
- **WHEN** 事件流在跟蹤期間新增事件
- **THEN** 輸出 SHALL 反映新事件，不需重新啟動

#### Scenario: Job 結束時停止
- **WHEN** 事件流出現 job 結束事件
- **THEN** 即時模式 SHALL 顯示最終摘要並結束

### Requirement: 事後模式展開完整資料流
事後模式 SHALL 對一個已結束的 job 顯示：資料流向、每次派工的授權與產出、
成本歸屬、以及偵測到的所有資料流異常。輸出 SHALL 讓「這份報告是根據什麼寫出來的」
這個問題在一個畫面內可被回答。

#### Scenario: 顯示流向
- **WHEN** 檢視一個含原始資料、三次分析與一次彙整的 job
- **THEN** 輸出 SHALL 顯示從原始資料到最終報告的流向與各段的授權

#### Scenario: 異常被凸顯
- **WHEN** job 中存在無授權產出或產出被覆蓋
- **THEN** 該異常 SHALL 出現在輸出中，且與正常流向可區分

#### Scenario: 顯示成本歸屬
- **WHEN** 檢視一個已結束的 job
- **THEN** 輸出 SHALL 顯示各 lane 的成本與 token 分布

### Requirement: 輸出同時供人閱讀與供機器解析
兩種模式 SHALL 皆可輸出結構化格式，使 golden job 的斷言與外部工具能直接消費，
而不需重新解析人類可讀的排版。

#### Scenario: 結構化輸出可被解析
- **WHEN** 以結構化格式輸出一個 job 的資料流
- **THEN** 該輸出 SHALL 可被解析，且含節點、邊與異常清單

#### Scenario: 兩種格式內容一致
- **WHEN** 對同一個 job 分別產生人類可讀與結構化輸出
- **THEN** 兩者所述的異常 SHALL 相同

### Requirement: Monitor 不影響被觀察的 job
Monitor SHALL 為唯讀。啟動、關閉或崩潰 SHALL NOT 影響執行中的 job，
亦 SHALL NOT 修改事件流或任何 artifact。

#### Scenario: 監控不改變事件流
- **WHEN** 對一個 job 執行即時模式後結束
- **THEN** 該 job 的事件流 SHALL 與監控前相同

#### Scenario: 監控不需要 job 存在於同一程序
- **WHEN** job 由另一個程序執行
- **THEN** 即時模式 SHALL 仍能跟蹤之

### Requirement: 找得到可觀察的 job
系統 SHALL 能列出目前可觀察的 job 及其狀態，使使用者不需事先知道 job 識別。

#### Scenario: 列出 job
- **WHEN** 儲存區中有三個 job
- **THEN** SHALL 列出三者及其階段與最後活動時間

