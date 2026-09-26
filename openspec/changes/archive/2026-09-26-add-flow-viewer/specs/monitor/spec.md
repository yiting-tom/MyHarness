## ADDED Requirements

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
