# proxy — 進來的資料屬於誰

這是 `DESIGN.md` 決策 #4，也是使用者第一則訊息裡就講明的東西：
「用 proxy agent 來分析進來的資料應該要給哪個 subagent，並告知 orchestrator」。

它回答**一個問題**：這份剛進來的資料屬於哪一條 lane。

## 為什麼只分類

「屬於誰」和「該對它做什麼」是兩個問題。第二個的答案來自**目標**，
而 proxy 看不到目標（見下），所以它根本沒有資訊可以下任務。

讓 proxy 自己 dispatch 還會有兩個後果：

- 它會花掉 dispatch 預算與金錢，而那是 `JobRunner` 三道防迴圈在管的
- 一次分類錯誤會直接變成一次真的執行，而不是一個可以被否決的建議

DESIGN #4 寫的「先斬後奏」在這裡是**分類先做、事後告知**，不是「先執行、事後告知」。

## 為什麼不授權

分類不等於授權。`GrantSet` 的來源仍然只有兩個：lane 自己的 namespace，
和 `dispatch(inputs=[...])` 明確列出的 id。

如果 proxy 能授權，就出現第三種來源 —— 而且是**由模型判斷產生的**那一種。
資料流監視器現在能斷言「沒有未授權的產出」，正是因為授權來源少到數得完。

實務差別：proxy 說「這份屬於 txn-2024」，orchestrator 仍然要在 dispatch 時
把 id 放進 `inputs`，lane 才讀得到。少了那一步，lane 拿到的是 `not_granted`。
給 orchestrator 的通知會提醒這件事。

## 零 context 共享 —— 這條有測試

Proxy 的 prompt **只由兩樣東西組成**：routing table，和一份有界的樣本。
沒有 plan、沒有 goal、沒有任何 finding。

這不是意圖而是需求，因為「讓 proxy 判斷得更準」的下一步永遠是餵它更多東西，
而一個看得到 plan 的 proxy 就是第二個規劃者 —— 只是沒有預算控制，也沒人會發現。

`tests/proxy/test_classify.py::TestZeroContextSharing` 斷言 prompt 裡不含
plan 模板文字與 goal 文字，並斷言 `classify()` 的簽名裡沒有任何
能取得 plan 的把手（`job_id`、`store`、`runner`…）。

## Routing table

Orchestrator 在 `plan_update` 時宣告，是**宣告式資料**，兩邊不共享 context：

```json
[
  {"lane": "txn-2024", "accepts": "2024 交易明細、金流紀錄", "status": "open"},
  {"lane": "kyc-docs", "accepts": "身分/KYC 文件"},
  {"lane": "legacy",   "accepts": "2023 前系統 log", "status": "closed"}
]
```

`status` 預設 `open`；`closed` 的 lane 連出現在 proxy 的清單裡都不會。
省略 `routing_table` 不會清掉既有的那份。

壞掉的 entry 一律**拒絕並說明**，不靜默丟掉 —— 少一個 accepts 的 lane
永遠不會收到東西，而沒有任何訊息說為什麼。

## 樣本的兩道閘

Proxy 是一個 LLM，餵給它的東西就是 context。所以樣本受兩道約束：

| | 上限 |
|---|---|
| 行數 | 12 |
| 字元數 | 1,200 |
| 單行 | 300（超過裁切） |
| 讀檔上限 | 64 KiB |

分類需要的資訊其實很少 —— CSV 的 header 就幾乎決定了答案。
上限小是因為更多也不會分得更準，不是因為省錢剛好。

二進位內容只會被描述（`(binary content, not sampled)`），不會被解碼倒進 prompt。

樣本經由 `store.localize` 取得，不自己開檔：第二條讀取路徑會整個站在授權模型外面。

## 失敗一律降級，資料一定落地

Ingress 不能依賴一次模型呼叫成功。每一種失敗都變成「未路由」，而 blob 已經存好了：

| 情況 | `unrouted_because` |
|---|---|
| 還沒有 routing table / 沒有開放的 lane | `no_table` |
| 分類器說判斷不出來 | `no_match` |
| 回了一個不存在或已關閉的 lane | `no_match` |
| 逾時、端點掛掉、回傳無法解析 | `failed` |

三者分開，是因為對 orchestrator 的意義不同：等一下再說、再看一眼、出事了。

## 記帳

`proxy.route` 事件的花費記在 `(proxy)` bucket，**不算在被判定的那條 lane 頭上** ——
那條 lane 什麼都還沒做，只是被點名。

資料流圖上，路由是一條 `SUGGESTED` 邊（blob → lane），與 `GRANTED` 分開。
如果 proxy 判定了而那條 lane 從未取得它，會出現 `suggestion_ignored`（WARNING）。
那不一定是錯 —— orchestrator 本來就可以推翻分類 —— 但**被丟掉的資料和
刻意的推翻在事件流裡長得一模一樣**，所以不預設寬容的那個解讀。
