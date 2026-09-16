## ADDED Requirements

### Requirement: 資料提供者看得見自己的資料去了哪裡
系統 SHALL 能對一個已結束的 job 產生一份**以資料流向為主體**的報告，
其讀者是交出資料的人，而非開發者。該報告 SHALL 顯示每一份進入 job 的資料
被哪些執行讀取、每一次執行產出了什麼、以及這些產出如何收斂成最終報告。

報告 SHALL NOT 要求讀者理解 dispatch、lane、artifact 或 token 等實作詞彙
才能回答「我的資料去了哪裡」。

#### Scenario: 每一份輸入都說得出去向
- **WHEN** 一個 job 有兩份原始資料，其中一份被兩次執行讀取、另一份沒有被任何執行讀取
- **THEN** 報告 SHALL 分別說明兩者的去向，且「沒有被任何執行讀取」SHALL 被明白指出

#### Scenario: 不以實作詞彙作為唯一說法
- **WHEN** 報告提到一次未完成的執行
- **THEN** 該說明 SHALL 以自然語言陳述發生了什麼，SHALL NOT 僅以狀態代碼呈現

### Requirement: 報告的來源鏈預設就被指出
報告 SHALL 預設標示出最終報告的來源鏈 —— 也就是沿產出與授權邊回溯到原始資料的
那條路徑。不在該鏈上的執行 SHALL 與在鏈上的執行在視覺上可區分，且該區分
SHALL 同時有文字說明。

讀者的第一個問題是「這份結論是根據什麼」。給他一張要自己找路的圖，等於沒有回答。

#### Scenario: 來源鏈被標示
- **WHEN** 檢視一份由兩次執行的產出收斂而成的報告
- **THEN** 那兩次執行與其讀取的原始資料 SHALL 被標示為來源鏈的一部分

#### Scenario: 未貢獻的執行可被區分
- **WHEN** 某次執行的產出不在最終報告的來源鏈上
- **THEN** 該次執行 SHALL 可與在鏈上的執行區分，且理由 SHALL 以文字說明

### Requirement: 沒做到的事與資料流異常以讀者能理解的語言呈現
報告 SHALL 呈現 framework 自動蒐集的 caveats 與偵測到的資料流異常，
並以自然語言說明其意義與影響。異常的內部識別代碼 SHALL NOT 是唯一的說明。

這些是 framework 主動蒐集的，不依賴 LLM 記得申報；把它們留在只有 API 看得到的
地方，等於保留了那個不申報的可能。

#### Scenario: 異常被翻成讀者能理解的說法
- **WHEN** 報告中含一項「產出未被任何後續執行讀取」的異常
- **THEN** 報告 SHALL 以自然語言說明這代表什麼，SHALL NOT 僅顯示其識別代碼

#### Scenario: 未完成的分析出現在讀者看得到的地方
- **WHEN** 某次執行因預算耗盡而未完成，而 job 正常收工
- **THEN** 該情況 SHALL 出現在報告中，與結論同樣顯眼

#### Scenario: 沒有問題時也要說
- **WHEN** 一個 job 沒有任何 caveat 與異常
- **THEN** 報告 SHALL 明白陳述這件事，SHALL NOT 以留白表示

### Requirement: 章節先標價再展開
報告 SHALL 先列出各章節與其 token 估計，讀者 SHALL 能在看到估計之後才取得全文。

這是 DESIGN #14 的價目表，對象換成人。對 agent 是為了省 context，
對人是為了讓他知道自己要讀多少。

#### Scenario: 價目表先於全文
- **WHEN** 檢視一份含六個章節的報告
- **THEN** 六個章節的標題與 token 估計 SHALL 皆可見，且全文 SHALL 在讀者要求後才呈現

#### Scenario: 沒有章節時
- **WHEN** 報告沒有可辨識的章節
- **THEN** SHALL 直接呈現內容，SHALL NOT 顯示空的價目表
