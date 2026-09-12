## 1. 可行性驗證（決策閘之前，不寫 production code）

- [x] 1.1 `spikes/spike13_a2a_artifact_semantics.py`：查明 A2A 的 artifact 能否
      自然標示「這是章節價目表，不是章節內容」。**這是 D7 的閘**
- [x] 1.2 同一支 spike：查明 agent card 能否宣告兩種輸出模式，讓呼叫方顯式選擇
      （規格：能力宣告含兩種模式）
- [x] 1.3 `spikes/spike22_a2a_stream_cursor.py`：把 `revision` 當串流序號跑一次，
      刻意中斷再重連，確認中斷期間的改變仍可被得知
      （規格：重連時不漏事件）**可滿足，但不能只靠 resubscribe** ——
      `SubscribeToTaskRequest` 只有 `id` / `tenant`，重連開的是從現在開始的
      新串流。cursor 走 `TaskStatusUpdateEvent.metadata`，缺掉的從
      `Task.history` 補
- [x] 1.4 同一支 spike：確認不構成實質改變的內部事件不會觸發推送
      （規格：逐輪的內部事件不觸發推送）**已經成立** —— `ctx` 不在 `MEANINGFUL`
- [x] 1.5 `spikes/spike23_a2a_task_states.py`：把「查無此分析／存在但不在此程序
      執行中／執行中」三種狀態映射到 A2A 的 task 狀態，記錄哪一種沒有合適的對應
      （規格：存在但不在此程序執行中）**那一種會裂成兩半**：在別的程序跑完的
      → COMPLETED，沒有落差；跑到一半被丟下的 → 九個值沒有一個合適
- [x] 1.6 `spikes/spike24_a2a_overhead.py`：量一次 A2A 請求的固定開銷，
      與 spike #12 在 proxy 上量到的 8,372 tokens（93%）對照
      **+156 token，佔回應 14%，沒有重演**

> spike 編號與 tasks.md 原本寫的 14 / 15 / 16 不同：那三個號碼在這份文件寫成之後
> 被 token 相關的 spike 用掉了。spike 編號是全域遞增的，所以改用 22 / 23 / 24。

## 2. 記錄結論

- [x] 2.1 四支 spike 的結論寫進 `spikes/RESULTS.md`，含**沒驗過的部分與原因**
      —— 未驗證的是 JSON-RPC binding 的錯誤碼：canonical proto 沒有錯誤列舉，
      而 `specification/json/a2a.json` 在這個 repo 其他 spike 抓的路徑上是 404。
      §7.1 實作前要補
- [x] 2.2 不適用 —— 1.1 通過了（spike #13），沒有否決理由要寫

## 3. 決策閘

- [x] 3.1 **走 B（雙軌）。** `Artifact.extensions` 加上 agent card 上
      `AgentExtension(required=true)` 是協定認可的標示方式，不是塞在 metadata
      裡的私有約定（spike #13）。#24 又證實這條路的信封成本是 1.16x，不是 93%
- [x] 3.2 不適用 —— 沒有退回 A
- [x] 3.3 繼續第 4 節

## 4. 邊界骨架（僅在 3.3 成立時）

- [x] 4.1 `pyproject.toml` 新增 `[project.optional-dependencies]` 的 `a2a`，
      HTTP server 不進核心依賴（D6）—— `a2a-sdk[http-server]` + `uvicorn`。
      `http-server` 是窄的那個 extra：沒有 grpc、sqlalchemy、fastapi
- [x] 4.2 `myharness/a2a/server.py`：與 `myharness/mcp/server.py` 同層的薄殼，
      建構時接受同一個 `AnalysisService`，不改 service（D3）—— service 一行沒動，
      兩種 skill 的差別只活在 `myharness/a2a/executor.py` 裡
- [x] 4.3 agent card：宣告兩種輸出模式與各自的取得方式
      （規格：能力宣告含兩種模式）—— 兩個 skill + `required=true` 的 extension。
      `default_output_modes` 維持 media type，沒有被誤用
- [x] 4.4 僅綁 loopback，且不進預設啟動路徑
      （規格：預設不啟用、僅接受本機連線）—— 沒有 console script，沒有別的模組
      import 它；`require_loopback` 對 hostname 不留情面（只有 `localhost` 這個
      名字本身例外，其餘必須**是**一個 loopback 位址）

## 5. 輸出模式

- [x] 5.1 未指定模式時以 `service.result()` 的摘要與章節價目表作答
      （規格：未指定模式）
- [x] 5.2 價目表回應帶上「如何取得章節全文」的指引，不只是 id 清單
      （規格：價目表自帶取用指引）—— artifact 上帶 extension URI，data 裡帶 hint
- [x] 5.3 全文模式以 `service.drill_section()` 逐節取得，不新增第二套內容上限
      （規格：全文由逐節組成）
- [x] 5.4 超過上限的章節裁切並標示，其餘章節仍產出
      （規格：超過上限的章節被裁切而非略過）
- [x] 5.5 契約測試：同一個 job 經兩條邊界，摘要與章節 id 清單相同
      （規格：摘要與章節清單一致）

> **5.3–5.5 的做法**
> 「全文」不是第二條回傳整份報告的路徑 —— `drill_section` 已經有 token 上限，
> 第二條路等於第二道上限，而這個專案的上限是實際執行的（D2）。所以全文模式是
> **端點代呼叫方把價目表走完**，一節一個 artifact：呼叫方可以中途停，被裁切的
> 那一節也在它被裁切的地方說明自己被裁切了。
> 某一節取不到不會拖垮其餘 —— 呼叫方要的是報告，讀得到的那部分仍然值得拿到。
> 契約測試用 MCP 測試套件同一個 scripted loop，兩條邊界量的是同一個 job。

## 6. 生命週期與串流

- [x] 6.1 啟動：非阻塞，回傳可供後續查詢的識別碼，共用 `service.start()`
- [x] 6.2 進度事件帶 `revision`（規格：事件帶版本序號）
- [x] 6.3 重連帶回最後看到的 `revision`，沿用 `wait_for_change(since=)` 的判斷
      （規格：重連時不漏事件）
- [x] 6.4 三種狀態分別映射，不壓成同一個回應
      （規格：查無此分析、存在但不在此程序執行中）
- [x] 6.5 結果查詢只讀事件流與 store，跨程序仍可作答
      （規格：結果查詢不依賴分析仍在執行）

> **6.3 補完**：每一個進度 tick 說兩次。metadata 給現在正在聽的人，message 給
> 不在的人 —— SDK 會在下一個狀態到達時把 status message 移進 `Task.history`，
> 而 history 是斷線的客戶端唯一的回頭路（`SubscribeToTask` 不重播，它的 request
> 也沒有地方放 cursor）。只放在 metadata 等於讓重連的人查不到自己漏了什麼。

> **6.1–6.2、6.4–6.5 的做法**
> 非阻塞走協定自己的 `configuration.return_immediately`，不是外接的機制。
> **A2A 的 task id 就是 job id**，所以之後每一次 GetTask／SubscribeToTask／
> 讀結果都在講同一個東西。`revision` 放在 status update 的 metadata 裡；
> `ctx` 不 bump revision，所以逐輪事件不會產生推送。
> 三種狀態在 `myharness/a2a/store.py`：在別的程序跑完的 → COMPLETED，
> 跑到一半被丟下的 → FAILED 加說明（spike #23：九個值沒有一個對，而 terminal
> 才能讓呼叫方停止等待）。用的不是 SDK 的 in-memory store，否則一個在磁碟上
> 讀得到的 job 會回 404。
>
> **6.3 只做了一半。** executor 的每一次 poll 都帶 cursor，事件也帶著它出去。
> 但 `SubscribeToTaskRequest` 沒有 cursor 欄位（spike #22），客戶端沒有地方把它
> 送回來 —— 補救路徑是 `Task.history`，而 history 目前沒有被填。
> 這一項要連 history 一起做。

## 7. 拒絕與上限

- [x] 7.1 拒絕以可讀結果表達，不外洩內部錯誤細節
      （規格：不合法的請求）
- [x] 7.2 併行上限的拒絕帶上限值與執行中數量
      （規格：併行上限）

> 拒絕以 failed task 送出，因為 terminal 才能讓呼叫方停止等待；內容是 service
> 自己那套面向客戶端的 code/message，不外洩內部細節。`at_capacity` 的
> running/limit 原樣帶過去 —— 「太多了」而不給數字沒有辦法據以行動。

## 8. 驗證

- [x] 8.1 離線測試：以假的 `AnalysisService` 驅動整個邊界，不打網路
- [ ] 8.2 live：真實 A2A 客戶端跑完整條鏈（啟動 → 串流 → 價目表 → 取一節全文）
      **測試寫好了，但沒有跑通過——卡在後端不是卡在邊界。**
      `tests/a2a/test_live_chain.py`（`pytest -m live tests/a2a`）。
      五次嘗試抓到三個真的問題（enum 比字串、啟動阻塞、job 先起才交出 id），
      全部已修。第四、五次卡在 `phase=planning, dispatches=0`，而**同樣的卡法
      在完全不經 A2A 的情況下也會發生**——直接對自架端點下一個 5 token 的
      completion 也 timeout（`http 000`，75 秒無回應），`/v1/models` 同樣不通。
      **自架後端當時是死的。** 等它回來再跑一次就能收尾。
- [x] 8.3 量出走價目表模式比全文模式多幾次 round-trip，記錄成本差
- [x] 8.4 更新 `README.md` 與 `docs/introduction.md`：兩條邊界並存，
      並寫明 A2A 上「job 不能跨程序重連」這面牆（D5）

> **8.3 的數字**（spike #25，`jobs-scratch/golden18`，五節）
> 閘門每讓你讀一節就多一次來回：全部讀完是 6 次對 1 次。
> **讀到第二節就不再省 token。** 只需要一個數字的 agent 花 1,889 拿到它（省 27%），
> 每節都要讀的 agent 多付 38%。這是 D1 選雙軌而不是單軌的理由變成數字 ——
> 單軌會讓後面那個呼叫方沒有出路。
