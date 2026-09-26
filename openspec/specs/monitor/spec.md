# monitor Specification

## Purpose
讓人看得見 job 正在做什麼，以及結束後它到底做了什麼。即時模式顯示當下的活動，
事後模式展開完整資料流。輸出同時要能給人讀也能給機器解析，而且無論如何都不能
影響被觀察的那個 job。
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

### Requirement: 一次派工的逐輪軌跡可被檢視
系統 SHALL 能對單一次派工顯示其**依序發生的每一輪**，至少涵蓋三類事實：
推理發生的位置、工具呼叫及其參數、以及該呼叫回傳的結果。順序 SHALL 與實際發生的
順序一致。這些事實 SHALL 全部由既有的 transcript 推導，SHALL NOT 新增任何寫入路徑。

「這次派工為什麼沒交出 finding」這個問題，過去四次 golden 診斷都是靠人工翻
transcript 回答的；派工層的 `status` 對它一個字都說不出來。

#### Scenario: 工具呼叫與其結果成對顯示
- **WHEN** 檢視一次呼叫了三個工具的派工
- **THEN** SHALL 顯示三次呼叫、各自的參數、以及各自回傳的結果

#### Scenario: 失敗的工具呼叫可被辨識
- **WHEN** 某次工具呼叫回傳錯誤
- **THEN** 該次呼叫 SHALL 與成功的呼叫在視覺上可區分，且不只以顏色區分

#### Scenario: 順序保留
- **WHEN** 檢視一次派工的軌跡
- **THEN** 各輪的先後 SHALL 與 transcript 中的順序相同

### Requirement: 未留存與非精確的內容不得被冒充
凡是 harness 沒有留存、或只留存了近似值的東西，視圖 SHALL 明確標示其為不可得或近似，
SHALL NOT 以相鄰的資訊代為呈現，亦 SHALL NOT 以留白讓讀者自行推測。

這是既有紀律的延伸：授權邊不冒充讀取邊，因為兩者一致時沒有資訊、不一致時全是資訊。
同樣的理由適用於推理內容 —— `_block_to_dict` 刻意丟棄推理文字只留字元數，
而在不回傳推理的後端上該字元數恆為零。**把工具呼叫排成一列並稱之為「推理過程」，
就是讓一份不存在的紀錄看起來存在。**

#### Scenario: 推理內容不可得
- **WHEN** 某一輪含推理區塊，而 harness 只留存了其長度
- **THEN** 視圖 SHALL 顯示該輪發生過推理並註明內容未留存，SHALL NOT 顯示任何推理文字

#### Scenario: 後端回傳空的推理區塊
- **WHEN** 推理區塊的長度為零
- **THEN** 視圖 SHALL 與「有內容但未留存」區分開來

#### Scenario: 被截斷的工具結果
- **WHEN** 某次工具結果在留存時被截斷
- **THEN** 視圖 SHALL 標示其為節錄，並指出被略過的量

#### Scenario: 估計的 token 數
- **WHEN** 某次派工的 token 數來自 harness 的估計而非後端回報
- **THEN** 該數字 SHALL 被標示為估計值

### Requirement: 流向與軌跡在同一個畫面內相連
視圖 SHALL 讓資料流向與逐輪軌跡在同一個畫面內互相到達：選定流向中的一次派工
SHALL 顯示該次派工的軌跡；軌跡中產出 artifact 的那一次呼叫 SHALL 能連回流向中的
該節點。

「這份報告是根據什麼寫出來的」與「那一步是怎麼做出來的」是同一個問題的兩端，
分在兩個工具裡就等於沒有答案。

#### Scenario: 由流向進入軌跡
- **WHEN** 在流向中選定一次派工
- **THEN** 該次派工的逐輪軌跡 SHALL 被顯示

#### Scenario: 由軌跡回到流向
- **WHEN** 軌跡中某次呼叫產出了一份 artifact
- **THEN** SHALL 能由該次呼叫到達流向中對應的節點

#### Scenario: 選定的狀態可被分享
- **WHEN** 選定某一次派工後取得該畫面的位址
- **THEN** 以該位址重新開啟 SHALL 回到同一次派工

### Requirement: 預算的消耗沿著軌跡可見
視圖 SHALL 沿著軌跡顯示該次派工的預算消耗，並標示 harness 對該次執行發出過的
預算警告與閘門的位置。派工以 `budget_exceeded` 結束時，SHALL 能看出是在哪一輪耗盡的。

golden #24 的 d1 在 57% 時被警告「只夠再 5 次請求」，而它在下一輪就寫完了 finding；
golden #23 的 d1 跑了 13 次查詢後回傳 `artifact: null`。兩者的 `status` 分別是
`ok` 與 `budget_exceeded`，而**沒有任何既有輸出說得出這兩件事的差別在哪一輪發生**。

#### Scenario: 耗盡的位置
- **WHEN** 檢視一次以 `budget_exceeded` 結束的派工
- **THEN** SHALL 能看出預算在哪一輪耗盡

#### Scenario: 警告的位置
- **WHEN** harness 在某一輪對該次執行發出預算警告
- **THEN** 該輪 SHALL 被標示

### Requirement: 視覺輸出是同一個模型的投影，且有文字等價物
視覺輸出 SHALL 由與 `inspect` 及其結構化輸出相同的資料流模型與異常偵測產生。
三者所述的異常與數字 SHALL 相同。凡是以圖形關係表達的事實（流向、邊的種類、狀態），
SHALL 同時有文字等價物，使其可被鍵盤到達、可被螢幕閱讀器讀出、且不只以顏色區分。

#### Scenario: 三種輸出一致
- **WHEN** 對同一個 job 產生 ASCII、結構化與視覺三種輸出
- **THEN** 三者所述的異常清單 SHALL 相同

#### Scenario: 圖形關係有文字等價物
- **WHEN** 視圖以圖形表示一條授權邊
- **THEN** 該邊 SHALL 同時以文字說明其來源、去向與種類

#### Scenario: 僅用鍵盤操作
- **WHEN** 僅以鍵盤操作視圖
- **THEN** 選定派工、展開呼叫、在軌跡中移動 SHALL 皆可達成，且焦點位置可見

### Requirement: 視覺輸出唯讀、離線、且不預設留在 job 目錄
產生視覺輸出 SHALL NOT 修改事件流、artifact 或 job 目錄中的任何東西。
輸出 SHALL 為單一自包檔案，開啟時 SHALL NOT 發出任何網路請求。
輸出內含 transcript 的節錄，而 transcript 是 blob —— 系統 SHALL NOT 在未經指定的
情況下把該檔案寫進 job 的儲存區。

#### Scenario: 產生輸出不改變 job
- **WHEN** 對一個 job 產生視覺輸出
- **THEN** 該 job 的事件流與 artifact SHALL 與產生前相同

#### Scenario: 離線開啟
- **WHEN** 在無網路的環境開啟輸出檔案
- **THEN** 其內容 SHALL 完整呈現

#### Scenario: 輸出位置須被指定
- **WHEN** 未指定輸出位置
- **THEN** 系統 SHALL 寫到標準輸出或要求指定，SHALL NOT 逕自寫入 job 目錄

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

### Requirement: 即時模式有一個瀏覽器的表面
系統 SHALL 能以瀏覽器頁面呈現即時模式，內容 SHALL 涵蓋終端機即時模式所呈現的
同一組事實：目前在等什麼或在做什麼、進行中與已完成的派工、累計成本與 token、
以及資料流隨執行逐步長出的樣子。

頁面 SHALL 在不重新載入的情況下反映新事件，並 SHALL 在 job 結束後停止更新
且停留在最終狀態。

#### Scenario: 不重新載入就反映新事件
- **WHEN** 頁面開啟期間事件流新增了一次派工
- **THEN** 該派工 SHALL 出現在頁面上，且不需要重新載入

#### Scenario: 兩種表面說法一致
- **WHEN** 對同一個事件流同時取得終端機與瀏覽器的即時輸出
- **THEN** 兩者所述的目前活動與派工狀態 SHALL 相同

#### Scenario: 結束後停下來
- **WHEN** 事件流出現 job 結束事件
- **THEN** 頁面 SHALL 停止輪詢並顯示最終狀態

#### Scenario: 執行中的等待可被辨識
- **WHEN** job 正在等待限流冷卻
- **THEN** 頁面 SHALL 顯示正在等待限流及已等待的時間，而非僅顯示「執行中」

### Requirement: 還沒被寫下的紀錄不得被冒充
對一個仍在執行中的派工，頁面 SHALL 明白表示其逐輪紀錄尚不存在，
SHALL NOT 以空白、載入中、或任何看起來像「稍後就會有」的樣子呈現。
逐輪紀錄是在一次派工**結束時**才落檔的 —— 執行中的那一份不是還沒載入，是還不存在。

這與「授權邊不冒充讀取邊」是同一條規則。差別在於這裡多了一個時間維度 ——
「還沒有」與「不會有」對讀者是兩件事，而兩者都不是「正在載入」。

#### Scenario: 執行中的派工沒有逐輪紀錄
- **WHEN** 檢視一個仍在執行中的派工
- **THEN** 頁面 SHALL 說明其逐輪紀錄要等該次派工結束才會存在

#### Scenario: 已結束的派工可取得逐輪紀錄
- **WHEN** 某次派工結束且其逐輪紀錄已落檔
- **THEN** 該紀錄 SHALL 可被取得

#### Scenario: 執行中仍看得到已讀取的東西
- **WHEN** 一次執行中的派工已經讀取了一份資料
- **THEN** 該讀取 SHALL 可見 —— 讀取事件是執行期間就寫下的

### Requirement: 即時視圖的 server 唯讀且只在本機可達
提供即時視圖的 server SHALL 只接受讀取操作，SHALL NOT 提供任何修改 job、
事件流或 artifact 的路徑。它 SHALL 只繫結到 loopback 位址；
主機名 SHALL NOT 被當成位址的證明。

終端機的即時模式不聽任何 port。這個表面會，所以它是這個能力唯一真正新增的
攻擊面，而它不該為了方便而變成網路可達。

#### Scenario: 拒絕非 loopback 的繫結
- **WHEN** 要求 server 繫結到一個非 loopback 的位址
- **THEN** SHALL 拒絕並說明原因

#### Scenario: 沒有寫入路徑
- **WHEN** 對 server 發出任何非讀取的請求
- **THEN** SHALL 拒絕，且 job 的事件流與 artifact SHALL NOT 改變

#### Scenario: 路徑上的識別不被信任
- **WHEN** 請求的路徑含指向儲存區之外的識別
- **THEN** SHALL 拒絕，SHALL NOT 讀取該位置

### Requirement: 即時視圖以圖呈現資料流與 agent 的處理
瀏覽器即時視圖 SHALL 以圖呈現資料流：資料、處理它的 agent、agent 的產出、
以及最終報告，依流向排列，且 SHALL 隨事件流即時長出新的節點與邊。
每個執行中的 agent SHALL 顯示它最近一步在做什麼、目前第幾輪、
以及預算已用的比例；這些 SHALL 來自執行期間寫下的步驟事件。

圖中的每一種視覺編碼（顏色、實線或虛線、動畫）SHALL 有文字等價物，
SHALL NOT 以顏色作為唯一的訊號。

#### Scenario: 執行中的 agent 顯示當下的步驟
- **WHEN** 一次派工正在執行，且已寫下步驟事件
- **THEN** 該 agent 的節點 SHALL 顯示其最近一次呼叫的工具與參數摘錄、輪次與預算比例

#### Scenario: 新的讀取即時出現在圖上
- **WHEN** 頁面開啟期間，一次執行中的派工讀取了一份資料
- **THEN** 該資料與該 agent 之間的邊 SHALL 出現，不需要重新載入

#### Scenario: 點開一個 agent 看它的步驟
- **WHEN** 使用者選取一個 agent
- **THEN** SHALL 顯示它的步驟序列；已結束的派工 SHALL 可進一步取得完整逐輪紀錄

### Requirement: 沒有步驟紀錄的派工不被畫成閒置
當事件流中某次派工沒有任何步驟事件時，視圖 SHALL 說明原因，
SHALL NOT 將其呈現為「沒有在做事」。原因至少區分：
該事件流早於步驟事件的存在、以及派工剛開始尚未完成第一輪。

#### Scenario: 舊的事件流
- **WHEN** 檢視一個整份事件流都沒有步驟事件的 job
- **THEN** 視圖 SHALL 說明這份事件流沒有記錄步驟，而非顯示 agent 閒置

#### Scenario: 剛開始的派工
- **WHEN** 一次派工已開始，但尚未寫下任何步驟事件，而同一事件流中其他派工有步驟事件
- **THEN** 視圖 SHALL 說明它還在等第一輪回應

### Requirement: 產出可以被點開檢視
即時視圖中的每一份資料與產出 SHALL 可被選取以檢視其內容。
文字產出 SHALL 顯示其目前內容；資料表 SHALL 顯示其大小、欄位，
以及在可以當文字讀取時的前幾行，否則 SHALL 說明它是二進位格式。

檢視 SHALL 經由存放區讀取，SHALL 只接受屬於被監看 job 的識別，
且 SHALL NOT 提供任何寫入。

#### Scenario: 點開一份 finding
- **WHEN** 使用者選取圖上的一份 finding
- **THEN** SHALL 顯示它目前的全文

#### Scenario: 不屬於這個 job 的識別被拒絕
- **WHEN** 請求檢視的識別屬於另一個 job，或含有路徑穿越
- **THEN** SHALL 拒絕，且 SHALL NOT 讀取任何內容

### Requirement: 版本與差異由紀錄重建，不完整時說明原因
一份產出若被寫入不只一次，檢視 SHALL 列出每一個版本：由哪一次派工寫入、
在該派工中的第幾次寫入、以及字數；並 SHALL 顯示每一版相對前一版新增與刪除的內容。
版本 SHALL 由逐輪紀錄中實際成功的寫入重建，SHALL NOT 推測。

當版本史不完整時，檢視 SHALL 說明原因，至少區分：
仍在執行的派工尚未留下逐輪紀錄、以及存放區的內容與紀錄中最後一版不一致。

一條 lane 寫入、但未在結束事件中回報的產出，SHALL 依逐輪紀錄出現在圖上。

#### Scenario: 被改寫的產出顯示刪除的內容
- **WHEN** 一份產出先由一次派工寫入，再由另一次派工以較短的內容改寫
- **THEN** 檢視 SHALL 顯示第二版相對第一版刪除的行

#### Scenario: 失敗的寫入不算一個版本
- **WHEN** 一次寫入被工具拒絕
- **THEN** 它 SHALL NOT 出現在版本史中

#### Scenario: 執行中的派工
- **WHEN** 一條仍在執行的 lane 可能正在改寫某份產出
- **THEN** 檢視 SHALL 說明該派工的寫入要在它結束後才看得到

#### Scenario: 沒回報的產出仍在圖上
- **WHEN** 一次派工寫了兩份 finding，只在結束事件中回報其中一份
- **THEN** 另一份 SHALL 仍以該派工的產出出現在圖上

