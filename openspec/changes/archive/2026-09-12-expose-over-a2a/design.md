## Context

對外只有 MCP over stdio 一條路，客戶端必須把 `myharness-mcp` spawn 成子程序，
因此只有同機的 agent 用得到。A2A 是把 agent 對外開放給**遠端**呼叫方的現成協定。

現有的分層對這件事是友善的。`myharness/mcp/server.py` 的 docstring 已經寫明：

> Everything above this file is protocol-free, so this module stays thin:
> list the tools, route a call, and keep stdout clean.

也就是說 `AnalysisService`（`myharness/mcp/service.py`）不知道 MCP 存在，
它只認 `start / poll / provide / answer / result / drill_section` 六個方法。
A2A 端點應該是**與 `mcp/server.py` 同層的第二個薄殼**，不是新的一層。

不友善的是語意。這個 harness 對最外層的承諾是 `analysis_result` **不回報告全文**，
只回摘要與各章節的 `est_tokens`，讓呼叫方看完價錢再決定讀哪節 —— 這是四層
context 閘門的最外面那一道。A2A 的 artifact 模型預設相反：task 做完，artifact 就是成品。

**這份設計的核心就是處理這個落差。**

## Goals / Non-Goals

**Goals:**
- 判定 A2A 能否**自然表達**「這是章節價目表，不是章節內容」，並以此為實作與否的閘
- 定出雙軌輸出的形狀：預設價目表、全文須顯式要求、兩種模式都在 agent card 上宣告
- 定出 A2A 的進度串流與現有 long-poll `revision` 游標的對應，且不漏事件
- 量出一次 A2A 請求的固定開銷，對照 spike #12 在 proxy 上量到的 8,372 tokens（93%）
- 把「job 只活在單一 process」這面牆寫清楚，而不是先實作再撞

**Non-Goals:**
- **不取代 MCP。** stdio 那條路一行不改，兩條邊界並存
- **不做認證、多租戶、速率限制。** spike 階段綁 loopback；正式開放前這是必答題，
  但答案不在這個 change 裡
- **不做 push notification / webhook。** 呼叫方拉，不是我們推 —— 與現有
  `_NoNotifications`（「Long-poll is the push」）一致
- **不做跨 process 的 task 重連。** 見 D5，這裡只負責把落差說清楚
- 不動 orchestrator、lane、artifact store、proxy

## Decisions

### D1. 雙軌輸出，預設價目表

三個選項：

| | 做法 | 得到 | 失去 |
|---|---|---|---|
| A | artifact 只放價目表 | 兩個邊界承諾一致 | A2A 呼叫方會以為 artifact 壞了 |
| **B** | **宣告兩種模式，預設價目表** | **保護仍在，且變成呼叫方的顯式選擇** | **多一條要維護與測試的路徑** |
| C | artifact 就是報告全文 | A2A 呼叫方零驚訝 | 最外層那道閘在 A2A 上不存在 |

**選 B。** 理由是這個 codebase 自己的行事風格：`myharness/proxy/` 把分類失敗拆成
`no_table` / `no_match` / `failed` 三種，因為「對 orchestrator 的意義不同」。
同一個精神 —— **把「我要全文」變成一個看得見的動作**，而不是靜靜地給或靜靜地不給。

B 也讓評估本身有結論可言：可以真的量出走價目表模式多花幾次 round-trip，
再判斷值不值得。A 與 C 都是先射箭再畫靶。

> **spike #13 的修正：宣告的載體不是 `outputModes`。**
> 原本以為模式宣告會落在 agent card 的輸出模式上。查 canonical `a2a.proto`
> 才發現 `default_output_modes` 與 `AgentSkill.output_modes` 都是 **media types**
> —— 把「價目表／全文」宣告成 output mode 是誤用欄位，不是使用欄位。
>
> 正確的載體有兩個，而且比原本的想法好：
>
> - **`AgentSkill`**：兩個 skill，各自的 id / name / description 說清楚差別。
>   它們正好對應已經存在的 `analysis_result` 與 `analysis_drill`。
> - **declared extension**：`AgentCapabilities.extensions` 宣告一個 extension URI，
>   `Artifact.extensions` 在每份價目表上標記它。關鍵是
>   `AgentExtension.required` —— proto 的原話是「If true, the client must
>   understand and comply with the extension's requirements」。
>   **不懂這個約定的客戶端會被告知它不懂**，而不是默默把目錄當報告讀。
>
> 這正是 D1 要的「把它變成看得見的動作」，而且是協定認可的機制，
> 不是塞在 `metadata` 裡的私有約定 —— `Artifact.metadata` 是一個位置，
> 不是一個語意。

### D2. 全文模式仍逐節給，不整份倒出

即使呼叫方顯式要求全文，也**沿用 `drill_section` 逐節取**，而不是新開一條
「回傳整份報告」的路徑。理由有二：

- `drill_section` 已經有 token 上限（`MAX_SECTION_TOKENS`），新路徑等於新上限，
  而這個專案的上限是實際執行的，不是宣告的
- 只有一條產出路徑，兩個邊界才不會漂移

「全文模式」的具體意義因此是：**端點代呼叫方把每一節依序取完**，而不是換一個 API。

### D3. A2A 端點與 `mcp/server.py` 同層，共用同一個 `AnalysisService`

不複製任何邏輯，不在 service 上加 A2A 專用參數。輸出模式的差異只發生在
A2A 那層薄殼裡 —— 它呼叫的還是 `result()` 與 `drill_section()`。

**替代方案**：在 `AnalysisService` 上加 `mode=` 參數。否決 —— 那會讓 protocol-free
的那一層開始知道有兩種協定，而這正是目前分層的價值所在。

### D4. 串流以 `revision` 為序號，斷線重連帶回最後看到的值

`JobHandle.wait_for_change(timeout, since=)` 的 docstring 已經指出失敗模式：

> ``since`` is the revision the caller last saw. If the job has moved on already,
> this returns immediately rather than waiting for the *next* change, which would
> hide the one that just happened.

A2A 的串流有同一個問題，而且更嚴重 —— 斷線期間發生的事沒有第二次機會。
所以每一則串流事件 SHALL 帶上 `revision`，重連時呼叫方帶回最後看到的值，
端點據此判斷是否已落後。**這是既有機制的沿用，不是新機制。**

`ctx` 事件不算狀態改變（既有規則），串流也照這條走 —— 否則串流變成每個
orchestrator turn 都在空轉。

### D5. 跨 process 的 task：不重連，但要區分三種狀態

`JobManager` 把 `asyncio.Task` 放在記憶體，job 只活在單一 process 裡。
`result` / `drill_section` 只讀事件流與 store，換 process 照樣答；
**進行中的 task 接不回來**。

`AnalysisService._poll_finished_elsewhere` 已經把這件事拆成兩種答案：

> A job this process did not run is not the same as no job at all.
> The two need different handling by the client.

A2A 這層必須把這個區分**映射到協定的狀態上**，而不是壓成同一個錯誤。
三種狀態各自對應什麼，是 spike 要驗的東西之一。

**替代方案**：實作 task 重連（把 job 狀態外部化到可跨 process 恢復）。
否決 —— 那是另一個 change，範圍遠大於這裡，而且會動到 job runner。

### D6. HTTP 依賴放進 optional extras，不進核心

目前執行期依賴只有 `claude-agent-sdk`、`duckdb`、`jsonschema`，沒有任何 web 框架。
A2A 需要一個 HTTP server，但**只用 MCP 的人不該被迫裝它**。
因此走 `[project.optional-dependencies]` 的 `a2a`，與現有的 `dev` 同層。

### D7. 決策閘：spike 1 驗不過就退回 A

如果 A2A 的 artifact 沒有欄位能自然標示「這是目錄不是內容」，B 就退化成
「在 artifact 裡塞一個自訂約定」。那種情況下 **A 反而誠實** —— 單軌價目表，
並把「A2A artifact 語意不足以表達雙軌」寫進 `spikes/RESULTS.md` 當否決理由。

不硬塞自訂約定，理由與 `allowed_paths` 那次一樣：一個看起來能用、實際上
語意不對的機制，半年後會被當成協定支援的東西來用。

> **閘已通過（spike #13）。** A2A 有協定認可的方式標示「這是目錄不是內容」——
> `Artifact.extensions` 加上 agent card 宣告的 `AgentExtension(required=true)`。
> 走 B，第 4 節之後成立。
>
> 但同一支 spike 也發現 **`TaskState` 的九個值裡沒有一個表示
> 「存在但不在此程序執行中」**，所以 D5 的落差比原本寫的更大：
> 不只是進行中的 task 接不回來，而是連這個狀態本身在協定上都沒有原生位置。
> 交給 spike #15。

## Risks / Trade-offs

- **A2A 呼叫方拿到價目表，以為壞了** → agent card 明白宣告兩種模式；
  價目表 artifact 自身必須帶「怎麼取得全文」的指引，而不是只有一份 id 清單
- **端點是網路可達的，MCP stdio 不是** → spike 階段綁 loopback，
  且**不進預設啟動路徑**。認證未答之前不對外開
- **串流斷線漏事件** → `revision` 游標（D4），並以「斷線重連後不漏事件」為測試項，
  而不是靠實作正確
- **A2A 的固定開銷可能重演 proxy 那 93%** → 先量再說。這是 spike 4 的唯一目的
- **兩條邊界隨時間漂移** → 共用 `AnalysisService`（D3），不複製；
  以契約測試釘住「同一個 job 經兩條邊界得到同一份摘要與同一份章節清單」
- **評估本身可能無結論** → 決策閘（D7）給了明確的退場：驗不過就記錄否決理由並停，
  這仍是這個 change 的有效產出

## Open Questions

- A2A 的 task 狀態集合裡，哪一個對應「存在但不在此 process 執行」？
  若沒有合適的，要用什麼形式表達才不會被誤讀成失敗？（spike 3 / D5）
- `analysis_provide` 的中途補資料，在 A2A 上是新 message 還是 task 的後續輸入？
  分類與 `announced` 的三態回報要怎麼帶回去？
- `analysis_answer` 對應的 `input-required` 狀態，逾時語意由誰決定 ——
  harness 既有的逾時（不答會變成報告上的 caveat）還是呼叫方？
