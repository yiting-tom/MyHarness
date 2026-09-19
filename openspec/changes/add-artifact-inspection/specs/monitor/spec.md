## ADDED Requirements

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
