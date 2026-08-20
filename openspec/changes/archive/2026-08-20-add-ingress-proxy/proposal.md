## Why

Proxy 是 `DESIGN.md` 十七個決策裡**最後一個沒建的**（#4），也是使用者第一則訊息
就講明的東西：「用 proxy agent 來分析進來的資料應該要給哪個 subagent
並告知 orchestrator」。

現在 `analysis_provide` 落完 blob 之後，只能丟一則「有新資料，未經路由」
給 orchestrator，由它自己看著辦。這會讓**規劃者花 context 去做分類**——
而規劃者的 context 正是整個系統在省的東西。資料多的時候尤其明顯：
每一份都要 orchestrator 讀一次 metadata、想一次、回一次。

事件流那一層早就在等它了：

| 已經存在 | 位置 |
|---|---|
| `proxy.route` 事件型別 | `events/types.py` |
| proxy 花費獨立記帳（`(proxy)` bucket） | `events/query.py:_bucket` |
| 「進來的資料沒有任何 lane 用到」caveat | `events/query.py:derive_caveats` |

缺的只有產生這些事件的那一端，以及 orchestrator 用來遙控它的 routing table。

## What Changes

- `plan_update` 新增 `routing_table`：orchestrator 宣告每條 lane 收什麼、
  開不開放。這是**宣告式資料**，proxy 與 orchestrator 之間零 context 共享。
- 新增 `myharness/proxy/`：單次、無狀態的分類器。只看 metadata + 有界樣本，
  不看 plan、不看 goal、不看任何 finding。
- `analysis_provide` 在落 blob 之後呼叫 proxy，把判斷結果寫成 `proxy.route`
  事件，並讓通知 orchestrator 的那則訊息帶上「這份資料看起來屬於哪條 lane」。
- Proxy **只分類，不派工、不授權**。授權仍然只發生在 `dispatch(inputs=...)`。
- 路由失敗（模型掛掉、逾時、比對不到）一律降級為「未路由」並照常落地。
  **ingress 不能依賴一次模型呼叫成功。**

## 不在這個 change 裡

- **Proxy 不會自己 dispatch。** 它不知道要下什麼任務 —— 任務來自目標，
  而目標在 orchestrator 手上。它只回答「這份資料屬於誰」。
- **Lane 內部的大型 tool result 不歸它管**（DESIGN §9 的開放項）。
  那需要的是 tool-result 過濾器，不是 router。

## Impact

- Specs: `proxy`（新 capability）、`orchestrator`（`plan_update` 新增欄位）
- Code: 新增 `myharness/proxy/`；`myharness/orchestrator/plan.py`（routing table
  的型別與存取）、`orchestrator/tools.py`（`plan_update`）、
  `myharness/mcp/service.py`（provide 呼叫 proxy）
- 不動：授權模型、handle contract、lane 工具面、既有事件型別
