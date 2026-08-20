# MyHarness 介紹與使用教學

以 `claude-agent-sdk` 建構、對外以 MCP server 形式提供的多 agent 資料分析 harness。

> 這份文件也有排版過的網頁版：
> <https://claude.ai/code/artifact/89dbdabe-3ed7-4dce-9546-b267a092c6c1>
> 兩者內容相同，改動時請一起更新。

---

## 目錄

- [它解決什麼問題](#它解決什麼問題)
- [核心原則](#核心原則外部化狀態--短命執行者)
- [快速開始](#快速開始)
- [六個 MCP 工具](#六個-mcp-工具)
- [Lane 能做什麼](#lane-能做什麼)
- [資料進來時發生什麼](#資料進來時發生什麼)
- [每一道上限](#每一道上限)
- [出錯的時候](#出錯的時候)
- [它保證什麼](#它保證什麼)
- [加一條自己的 lane](#加一條自己的-lane)
- [不用 MCP 直接跑](#不用-mcp-直接跑)
- [還沒有的東西](#還沒有的東西)

---

## 它解決什麼問題

**單一 agent 的 196k context，在真實的資料分析任務中會被輕易耗盡。**

一份 138KB 的 CSV 讀進 context 就佔掉數萬 token；分析要來回幾十輪，每一輪的工具輸出都
累積在同一個視窗裡。等到需要整合結論的時候，規劃者早就沒有空間可以思考了。

常見的做法是壓縮或摘要，但那是在賭「被丟掉的剛好不重要」。MyHarness 走另一條路：
**讓大量資料在結構上就到不了規劃者手上**。規劃者負責判斷與調度，實際碰資料的是一批
用完即棄的執行者，它們把完整分析寫進檔案，只回傳一個約 120 token 的指標。

---

## 核心原則：外部化狀態 + 短命執行者

同一招在四個層級遞迴套用。每一層都保護它上面那一層的 context，
手法都是「狀態存到外面去，執行者做完就消失」。

```
┌─ MCP 邊界 ─────────── 保護客戶端 agent — 只回摘要與章節價目表 ──────────┐
│                                                                        │
│  ┌─ Orchestrator ──── 保護規劃者 — subagent 只回 ~120 token 的 handle ─┐│
│  │                                                                    ││
│  │  ┌─ Lane worker ── 保護執行者 — 用完即棄，記憶存成 lane state ────┐ ││
│  │  │                                                              │ ││
│  │  │  ┌─ Artifact 讀取 ─ 保護任何讀取者 — 讀之前先查 token 估計 ─┐ │ ││
│  │  │  │                                                        │ │ ││
│  │  │  │   ╔══════════════ 原始資料 BLOB ══════════════╗         │ │ ││
│  │  │  │   ║ 沒有任何一條程式碼路徑能把它送進上面        ║         │ │ ││
│  │  │  │   ║ 任何一層的 context。                      ║         │ │ ││
│  │  │  │   ║ Blob 只能被查詢，不能被讀取。              ║         │ │ ││
│  │  │  │   ╚══════════════════════════════════════════╝         │ │ ││
│  │  │  └────────────────────────────────────────────────────────┘ │ ││
│  │  └──────────────────────────────────────────────────────────────┘ ││
│  └────────────────────────────────────────────────────────────────────┘│
└────────────────────────────────────────────────────────────────────────┘
```

每一層的邊界都是一道**實際存在的閘門**，不是慣例。最內層的 blob 連 `read_note`
都會拒絕它，只能透過 SQL 查詢。

### 兩個具體的機制

**Handle 契約。** Lane worker 回傳的東西同時受兩道約束：JSON Schema 管形狀、
字元數 clamp 管長度。只有形狀約束的話，一個合法的 `headline` 欄位照樣可以塞三千字進來。

**授權清單。** Lane 只能讀自己 namespace 底下的東西，加上派工時 `inputs` 明確列出的
artifact id。沒有第三種來源。SQL 裡不能寫檔案路徑，就是為了讓授權判定只發生在
artifact id 上 —— 兩套授權機制的話，實際安全性由比較弱的那套決定。

---

## 快速開始

### 1. 安裝

```bash
# 需要 Python 3.13 與 Claude Code CLI
git clone https://github.com/yiting-tom/MyHarness
cd MyHarness
uv pip install -e ".[dev]"
```

### 2. 設定後端金鑰

後端是可插拔的。用 OpenRouter 的話，在專案根目錄放一個 `.env`：

```
OPENROUTER_KEY=sk-or-v1-...
```

也支援自架的 LiteLLM 或 Anthropic 直連。每個 lane 型別可以指定自己的後端與模型層級 ——
寫的是 `model_tier="strong"` 這種能力層級，不是供應商的模型名稱。

### 3. 接上 Claude Code

```bash
claude mcp add myharness -- \
  myharness-mcp --root ./myharness-jobs --backend openrouter
```

`--root` 是 job 的存放位置，預設 `./myharness-jobs`。**刻意不叫 `jobs`**：layout
本身會在 root 底下放一層 `jobs/`，取那個名字會得到 `jobs/jobs/<job_id>/`。

### 4. 跑第一個分析

在 Claude Code 的對話裡，一次完整的互動長這樣：

```js
// 1. 開一個 job，立刻返回，不會卡住
analysis_start(task="分析這份交易資料，找出異常樣態，並給出不重複帳戶的總數")
→ { job_id: "job7ab30092db", state: "running", revision: 0 }

// 2. 把資料交進去。內容不會進任何人的 context
analysis_provide(job_id="job7ab30092db", payload=<CSV>, name="txn.csv")
→ { artifact: "job7ab30092db/blob/raw/txn.csv", bytes: 1472,
    routed: true, routed_to: "txn", announced: true }

// 3. 等到真的有進展才回（不是睡到時間到）
analysis_poll(job_id="job7ab30092db", wait=30)
→ { state: "running", revision: 8, dispatches: 1, spent_usd: 0.02,
    recent: ["dispatch.start d1 → ta1: Count distinct accounts..."],
    pending_questions: [] }

// 4. 分析問問題時回答它（不答會逾時，變成報告上的 caveat）
analysis_answer(job_id=..., question_id="q1", text="只看 2024 年")

// 5. 拿結果：摘要 + 章節價目表，不含報告全文
analysis_result(job_id="job7ab30092db")
→ { executive_summary: "...", key_findings: [...],
    sections: [{ id: "方法", est_tokens: 117 }, ...],
    cost: { usd: 0.2543, dispatches: 3 } }

// 6. 需要哪一節才讀哪一節
analysis_drill(job_id=..., section_id="方法")
```

> **為什麼是價目表而不是直接給報告？**
> 因為讀報告要花的是*你的* context。`analysis_result` 告訴你有哪些章節、
> 每節大約多少 token，你看完價錢再決定要讀哪幾節。這是整個系統對最外層的保護。

### 這段流程實測過

真實的 MCP client 把 `myharness-mcp` 當子程序 spawn、用 stdio 講話，跑完整條鏈
（40 列 CSV、一個數得出來的問題），**5/5 通過**，494 秒：

```
[  30.5s] running   rev=3   ingress
[ 146.7s] running   rev=5   ask.user  → 客戶端回答 → rev=7 ask.answer
[ 179.8s] running   rev=8   dispatch.start d1 → ta1
[ 299.5s] running   rev=10  dispatch.start d2 → syn1
[ 453.0s] running   rev=11  dispatch.end d2 ok: 7 distinct accounts
[ 493.9s] finished  rev=13  job.finish

result: 659 chars · 4 sections · 219 section tokens
```

---

## 六個 MCP 工具

| 工具 | 做什麼 | 回傳 |
|---|---|---|
| `analysis_start` | 開一個分析 job。非阻塞，工作在背景繼續 | `job_id`、狀態 |
| `analysis_poll` | Long-poll：等到狀態真的改變才回。逾時返回不是錯誤，是「還在跑」 | 有界進度摘要 + 待答問題 |
| `analysis_provide` | 中途補資料。存成 blob，內容不進任何 context | artifact id、`routed`、`announced` |
| `analysis_answer` | 回答分析提出的問題 | 確認 |
| `analysis_result` | 摘要、重點、caveats、成本、章節價目表 | 不含報告全文 |
| `analysis_drill` | 讀取單一章節全文 | 該節內容（仍有 token 上限） |

### Long-poll 的 `revision`

每次 `analysis_poll` 回應裡都有一個 `revision`。下次 poll 把它帶回去，
如果期間有事發生，會立刻返回而不是繼續等：

```
poll(job)            → { revision: 3, ... }   // 你離開去做別的事
                                              // 這時一條 lane 完成了
poll(job, since=3)   → 立刻返回                // 不會為了「下一次」改變而空等
```

沒有這個游標的話，兩次 poll 之間發生的事會被漏掉：等待會為了下一次改變而阻塞，
而剛剛那次永遠不會被提起。

> `ctx` 事件**不算**狀態改變。它每個 orchestrator turn 都會發，
> 拿它當訊號等於把 long-poll 變成每 30 秒空轉一次。

### 結果比 process 活得久

`analysis_result` 與 `analysis_drill` 只讀事件流與 artifact store，兩者都在磁碟上。
所以重啟之後、甚至換一個 process，它們照樣答得出來。

只有 `analysis_poll` 與 `analysis_answer` 需要活著的 job。對已經結束的 job，
它們回「**不在執行中**」而不是「查無此 job」—— 那是兩件不同的事，
客戶端的下一步也不同：一個還能讀結果，一個是打錯了。

---

## Lane 能做什麼

Lane worker 是實際碰資料的那一層。它拿不到 CLI 的檔案工具，只有這六個：

| 工具 | 用途 |
|---|---|
| `read_note` | 讀被授權的分析產出 |
| `write_finding` | 寫下完整分析（不寫在回覆裡） |
| `update_state` | 跨任務要記得的結論 |
| `localize_blob` | 取得原始檔案路徑（非表格格式才用） |
| `inspect_blob` | 看欄位、型別、列數、樣本 |
| `duckdb_query` | 對被授權的 blob 下 SQL |

### 一次典型的分析

```sql
-- 先看資料長什麼樣，猜欄位名稱會浪費一整回合
inspect_blob(artifact="job/blob/raw/txn-2024")
→ 2,940 rows；欄位 txn_id/ts/account/amount/channel；綁定表名 txn_2024

-- 再下 SQL。每個被指名的 artifact 會綁成一張表
duckdb_query(artifacts=["job/blob/raw/txn-2024"],
             sql="SELECT channel, round(avg(amount),2) AS avg_amt
                  FROM txn_2024 GROUP BY 1 ORDER BY 2")

-- 大結果用 into 寫回成新 blob，不灌進 context
duckdb_query(artifacts=[...], into="per-account",
             sql="SELECT account, sum(amount) AS total FROM txn_2024 GROUP BY 1")
→ wrote job/blob/lanes/ta1/derived/per-account — 765 rows
```

### SQL 沙箱

給 worker 一個 SQL 引擎，等於給它一個檔案讀取器。所以連線是這樣建的：

```
1. 把每個被授權的 blob ingest 成 table    ← view 是惰性的，table 不是
2. SET enable_external_access=false       ← 圍籬
3. 關閉三項擴充套件設定
4. SET lock_configuration=true            ← 縱深防禦，必須最後
5. 此時才執行 worker 的 SQL
```

十二條逃逸路徑逐一實測過：讀未授權檔案、glob、`ATTACH`、`COPY` 寫出、載入擴充套件、
http、`getenv`、重開組態，全部封死。

> **一個自我修正。** 原本宣稱第 2 行與第 4 行是「兩道閘，缺一不可」。實測不是：
> 在 DuckDB 1.5.5，`enable_external_access=false` 會自我防衛 —— 關掉之後開不回來，
> 而且連 `allowed_paths` 都一併凍結。
>
> 所以**圍籬只有一道**。`lock_configuration` 是縱深防禦，它釘住
> `autoload`／`autoinstall`（沒有它仍可改）。兩個測試分別釘住這兩件事，
> 免得文件比實作樂觀。

順序是設計的一部分：ingest 必須在關門之前，所以 blob 有 256 MiB 上限。
**那個上限是沙箱的形狀，不是效能調校** —— 試過 `allowed_paths` 那條「不用 ingest」
的路，它是加法不是減法，只設它的話 `/etc/hosts` 照讀，`COPY` 還會覆寫掉授權的 blob。

---

## 資料進來時發生什麼

`analysis_provide` 進來的資料會先被一個便宜模型分類：它屬於哪一條 lane。
orchestrator 收到的是一則約 40 token 的判斷，不是一份要它自己讀的 metadata。

這一層存在的理由和其他每一層一樣：**規劃者的 context 是要省的東西**。

### Orchestrator 用一張表遙控它

在 `plan_update` 時宣告 routing table。這是**宣告式資料**，兩者之間不共享任何 context：

```python
plan_update(
  plan="...",
  routing_table=[
    {"lane": "txn-2024", "accepts": "2024 交易明細、金流紀錄"},
    {"lane": "kyc-docs", "accepts": "身分/KYC 文件"},
    {"lane": "legacy",   "accepts": "2023 前系統 log", "status": "closed"},
  ])
```

`closed` 的 lane 連出現在分類器的清單裡都不會。省略 `routing_table` 不會清掉既有的那份。

> **分類器看不到目標，也看不到計畫。** 它的 prompt 只由兩樣東西組成：routing table，
> 和一份最多 12 行／1,200 字元的樣本。
>
> 這不是意圖而是需求，有測試背書 —— 因為「讓它判斷得更準」的下一步永遠是餵它更多東西，
> 而一個看得到 plan 的分類器就是*第二個規劃者*，只是沒有預算控制，也沒人會發現。

### 它只分類，不派工也不授權

| 它做 | 它不做 |
|---|---|
| 判斷資料屬於哪條 lane | **不派工** —— 「該做什麼」的答案來自目標，而它看不到目標 |
| 寫下 `proxy.route` 事件 | **不授權** —— lane 要讀到資料，orchestrator 仍須在 `dispatch` 的 `inputs` 帶上那個 id |

第二條特別重要。如果分類器能授權，就出現**第三種授權來源**，而且是由模型判斷產生的
那一種 —— 資料流監視器現在能斷言「沒有未授權的產出」，正是因為授權來源少到數得完。

### 失敗一律降級，資料一定落地

| 情況 | `unrouted_because` |
|---|---|
| 還沒有 routing table / 沒有開放的 lane | `no_table` |
| 分類器說判斷不出來 | `no_match` |
| 回了一個不存在或已關閉的 lane | `no_match` |
| 逾時、端點掛掉、回傳無法解析 | `failed` |

三者分開，是因為對 orchestrator 的意義不同：等一下再說、再看一眼、出事了。

### 實測：兩份資料，兩條 lane

真實的 nano 模型，只看 routing table 加十二行樣本，**5/5 通過**：

```
txn.csv   routed=True  -> txn   "a CSV of transaction records…"
kyc.csv   routed=True  -> kyc   "a structured KYC CSV containing holder…"
```

兩份都是 `confidence: high`。**分類這件事，便宜模型做得到。**

> ⚠️ **但每次分類有 8,372 tokens 不是我寫的。**
> 分類器自己的 prompt 是 619 tokens，實際計費 8,991 —— 差額（93%）是
> Claude Code CLI 自己的 base system prompt，那不是工具定義，`disallowed_tools`
> 碰不到它。
>
> 對一條 60k 預算的 lane worker 來說，那是用 SDK 換到工具面、多輪與 session 的
> 已知代價。對一個單次、無工具的分類器來說，那就是全部的成本。
> 修法是讓 proxy 不走 SDK，直接打後端 API —— 已另開 `bypass-sdk-for-proxy` 處理。

---

## 每一道上限

每個數字都對應一個「不加就會無界成長」的東西。它們是實際執行的，不是期望值。

| 項目 | 上限 | 為什麼 |
|---|---|---|
| Lane handle | 2,000 chars | Schema 管形狀、clamp 管長度。少一道只是「很可能」 |
| 查詢結果 | 50 rows **且** 4,000 chars | 40 欄 × 180 字 × 20 列在列數限內，照樣灌爆 context |
| Blob ingest | 256 MiB | 關門前必須讀完。這是沙箱的形狀，不是效能旋鈕 |
| 單次查詢 | 30 s | 失控的 join 不會自己停，由 timer thread 中斷 |
| Poll 回應 | 2,000 chars | 超過就丟內容：先丟日誌行，問題留到最後 |
| 單節鑽取 | 20,000 tokens | 你看過價錢才要的，但估計值仍可能低估 |

> **每項上限相乘會超過總上限。** 8 行事件 × 120 字加上 5 則問題 × 400 字，
> 已經約 3,000 字，而 poll 回應的總上限是 2,000。所以總上限是真的執行的：
> 超過就依序丟掉內容 —— 先丟最近事件，再減問題數量，最後才裁切最後一個問題的文字。
> 沒答的問題會擋住 job，漏掉的日誌行不會。

---

## 出錯的時候

這個系統假設模型會失敗、供應商會限流、判斷會出錯。處理方式有一條共通原則：
**失敗是值，不是例外。**

Lane 工具、orchestrator 工具、MCP 工具的拒絕，全部以文字結果回覆，不拋例外。
理由是呼叫端是一個 agent：一個說得出「為什麼被拒絕」的訊息可以讓它自己修正，
一個 stack trace 只會浪費它一個回合。

### 兩種失敗，兩種處理

| 類型 | 例子 | 處理 |
|---|---|---|
| Transient | 429、5xx、連線中斷 | per-backend 共享節流閘，300 秒時間預算，4s→60s 退避加 full jitter |
| Semantic | handle 不合格式、SQL 寫錯、參數被拒 | 回可據以行動的訊息，讓模型下一輪改正；重試 2 次後回失敗 handle |

重試用的是**時間預算而不是次數上限**：次數會在限流恢復之前就放棄。

### 三道防迴圈

- **Dispatch 上限與金額上限** —— 撞到就進入收尾模式，寬限次數用完強制中止
- **No-progress** —— 連續派工沒有新產出會被點名；兩倍門檻時強制收尾
- **被拒絕的工具呼叫不算進展** —— 全部呼叫都被拒的回合不計為「有動作」

> 最後一條是 live 跑的時候發現的：orchestrator 空轉六輪只發 `ctx` 事件。
> 它一直在呼叫工具，每次都被拒絕，而 harness 分不出差別 —— 因為拒絕是回傳值，
> `is_error` 是 false，從訊息流看跟成功一模一樣。四十個回合可以就這樣燒掉。

### 善終保證

Job 一定會有交付。orchestrator 沒有自己收工的話，harness 會用現有的產出寫一份 ——
但那份會標明是 `harness:salvage` 寫的，絕不會被歸給 orchestrator。
所有的缺漏（沒答的問題、撞到的上限、失敗的 lane）都會自動變成報告上的 caveat。

---

## 它保證什麼

規劃品質沒辦法斷言 —— 模型輸出會變，「這計畫好不好」也沒有測試。
紀律可以：用了多少 context、有沒有重複自己、花了多少錢、有沒有東西產出。

Golden job 每次跑都斷言這些（`pytest -m live tests/golden`）：

- 交付一定存在，且由 lane 產出而非 orchestrator 自己拼湊
- Orchestrator context 峰值有上限
- 沒有重複 dispatch、沒有未授權的產出、報告沒有被覆蓋
- 報告可回溯到原始資料（資料流圖上真的有那條邊）
- **報告含有只能靠實際計算得出的數字**

### Golden run #6：15/15 通過

Lane 用 `duckdb_query` 算出來的每一個數字，都與直接對 CSV 下 SQL 的結果**完全相符**：

| 指標 | 報告 | 直接查詢 |
|---|---:|---:|
| 不重複帳戶 | 765 | 765 |
| app 平均金額 | 13,981.81 | 13,981.81 |
| web 平均金額 | 20,612.47 | 20,612.47 |
| atm 平均金額 | 21,347.33 | 21,347.33 |
| branch 平均金額 | 21,655.25 | 21,655.25 |

> **最後那條斷言是後來加的，因為前面那些全過了還是能交出廢話。**
> 第五次執行通過了全部九項紀律斷言，然後交付了一份說「由於權限限制，未能讀取…」
> 的報告 —— 因為當時根本沒有工具能處理 blob。紀律是全部被檢查的東西，
> 而分析有沒有發生沒人檢查。
>
> 現在會檢查：`765` 不是模型猜得到的數字，而且對這份檔案的任何其他讀法都是錯的。
> 它出現，就代表查詢真的跑過。

### 第五次與第六次

| | 第五次 | 第六次 |
|---|---:|---:|
| 交付內容 | 「未能讀取資料」 | 五項具體發現 |
| Orchestrator context 峰值 | 9,720 | 6,757 |
| 成本 | $0.4440 | $0.2543 |
| Dispatches | 5 | 3 |
| 限流等待 | 534.9s | 129.6s |
| 資料流異常 | CRITICAL ×1 | 無 |

更便宜、更少 context、更少 dispatch —— 因為 lane 這次一次就把事情做完了，
不需要 orchestrator 反覆重派。能力補上之後紀律指標一起變好，不是巧合。

---

## 加一條自己的 lane

Lane 型別是這個 framework 的擴充點。一條 lane 由兩樣東西定義：
一份 charter（它是誰、怎麼工作），和一組預算。

```python
LaneType(
    name="tabular-analyst",
    charter_path=Path("charters/tabular-analyst.md"),   # 檔案，不是字串
    tools=("read_note", "write_finding", "update_state",
           "localize_blob", "inspect_blob", "duckdb_query"),
    model_tier="strong",        # 能力層級，不是供應商的模型名稱
    backend="openrouter",
    token_budget=60_000, max_turns=12, state_max_tokens=2_000,
    description="表格與交易資料的統計分析",   # orchestrator 看得到這句
)
```

### 幾個不明顯的設計

- **charter 是檔案不是字串** —— 可以 diff、吃 prompt caching，而且它的雜湊會寫進
  事件流，「這次跑的是哪一版 charter」事後查得到。
- **`model_tier` 而不是模型名稱** —— 同一條 lane 才能在不同 backend 上跑。
  `strong` / `mid` / `cheap` 由 backend profile 解析成實際模型。
- **`tools` 要明確宣告** —— 沒宣告的內建工具會用 `disallowed_tools` 移掉，
  省下約 16,468 tokens／worker。只用 `allowed_tools` 沒有這個效果，工具定義還是會送。
- **`description` 是給 orchestrator 讀的** —— 它憑這句決定要不要建這條 lane、
  派什麼給它。

### Charter 該寫什麼

Charter 是 worker 的全部人格。它會被放在 prompt 的穩定前綴，所以吃得到快取。
看 `charters/tabular-analyst.md` 當範本，重點是這幾條：

- 完整分析寫進 finding，**不要寫在回覆裡** —— 回覆只是一個指標
- 原始資料不要讀進 context，用 SQL 算
- 每個數字都要有樣本數；不確定就說不確定
- state 只放跨任務要記得的結論，細節在 finding 裡

寫完之後 `myharness --root <root> inspect <job>` 是檢查它有沒有照做的最快方式 ——
資料流圖會顯示這條 lane 讀了什麼、寫了什麼、有沒有被授權。

---

## 不用 MCP 直接跑

```bash
# 端到端跑一次 golden job（會花錢）
python -m myharness.goldens --backend openrouter

# 列出所有 job
myharness --root jobs-scratch/golden jobs

# 資料流與異常偵測；有 CRITICAL 時 exit code 2
myharness --root jobs-scratch/golden inspect golden

# 即時追蹤一個進行中的 job
myharness --root jobs-scratch/golden monitor golden
```

### Monitor 看得到什麼

這個系統刻意把資料藏起來，代價是出事時沒人看得見發生了什麼。
`inspect` 把事件流投影成資料流圖，並偵測五種異常：

| 異常 | 意思 | 嚴重度 |
|---|---|---|
| `ungranted_production` | 某次派工什麼都沒被授權，卻寫出了分析 | **critical** |
| `overwritten_output` | 兩次派工寫了同一個檔案，交付的可能不是有內容的那版 | warning |
| `unused_input` | 授權了但沒被用到 | warning |
| `orphan_output` | 產出了但沒人讀 | warning |
| `suggestion_ignored` | 分類器判定資料屬於某條 lane，那條 lane 從未取得它 | warning |

第一種是第五次執行實際發生的事：synthesizer 被派了兩次，第二次沒有帶任何授權，
卻仍寫出報告並覆蓋掉真正拿到三份 finding 的那一版。在逐行的事件輸出裡看不出來，
排成流向圖一眼就看到。

最後一種不一定是錯 —— orchestrator 本來就可以推翻分類 —— 但**被丟掉的資料和
刻意的推翻在事件流裡長得一模一樣**，所以不預設寬容的那個解讀。

---

## 還沒有的東西

`DESIGN.md` 的十七個決策都實作完了。以下是清單以外、真的跑起來之後才浮現的東西。

### 分類器的固定開銷（已量出，未修）

每次分類有 8,372 tokens 是 Claude Code CLI 的 base system prompt，佔請求的 93%。
分類器是單次、無工具、無 session —— 正好是 agent SDK 什麼都沒幫上的情境。
`bypass-sdk-for-proxy` 已提案，做法是讓它直接打後端 API。

### 非表格資料

`duckdb_query` 讀 CSV / Parquet / JSON。純文字與日誌目前只能 `localize_blob`，
沒有 `grep_blob`。等真的有這種 job 再決定它的輸出上限長什麼樣。

### `artifact.read` 事件

事件型別已經定義但還沒發出，所以資料流圖的「讀取邊」是空的。
這件事被誠實標成 `read_edges_available=false`，而不是拿授權邊冒充讀取邊。

### 限流

第六次 golden 執行有 129.6 秒花在等上游限流。付費模型仍然會被限。
節流閘是 per-backend 共享的，有時間預算與 full jitter 退避，
但**跨 backend 的總並行還沒有上限**。

### Lane 內部的大型 tool result

分類器只在 `analysis_provide` 這一個入口作用。如果一條 lane 自己用工具撈回一大塊資料，
那是另一個問題 —— 需要的是 tool-result 過濾器，不是 router。等真的遇到再處理。

---

## 開發

```bash
pytest                  # 離線，不花錢（716 tests）
pytest -m live          # 打真實 API，要金鑰，會花錢
openspec list           # 進行中的規格變更
```

專案用規格驅動流程（OpenSpec）：每個改動先寫 proposal 與 design，
再把需求寫成可斷言的 scenario，實作完才歸檔進 `openspec/specs/`。
目前 10 個 capability、79 條需求、716 個測試、94% coverage。

### 可行性驗證都留著

`spikes/` 底下的每個腳本都可以重跑，`spikes/RESULTS.md` 記錄了每次的結論，
包含**被否決的方案與否決的理由** —— 例如 `allowed_paths` 為什麼不是圍籬、
`task_budget` 為什麼在 OpenRouter 上不能宣告。

負面結果和正面結果一樣重要：沒有它們，半年後同一個「這樣不是更簡單嗎」的想法會再被試一次。
