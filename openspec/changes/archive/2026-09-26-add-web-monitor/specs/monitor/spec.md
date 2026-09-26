## ADDED Requirements

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
