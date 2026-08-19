# MyHarness — 設計文件

以 `claude-agent-sdk` 建構的多 agent harness，以 **MCP server** 形式對外提供資料分析能力。

## 1. 問題與核心原則

**問題**：單一 agent 的 196k context 在資料分析任務中極易耗盡。

**核心原則**：*外部化狀態 + 短命執行者*。此招數在四個層級遞迴套用：

| 層級 | 被保護者 | 手法 |
|---|---|---|
| MCP 邊界 | host agent 的 context | job-based API，只回摘要 + 章節價目表 |
| Orchestrator | 全局規劃者的 context | subagent 只回 ~120 token 的 handle，資料落 artifact |
| Lane worker | 執行者的 context | ephemeral agent + durable lane state |
| Artifact 讀取 | 任何讀取者 | blob 拒絕讀入 context，note 有 est_tokens 預檢 |

**不變式**：*原始資料不可能進入 orchestrator 的 context* — 因為沒有任何一條程式碼路徑能把它送進去。這是由構造保證，不是由 prompt 祈禱。

## 2. 架構總覽

```
             ┌──────────── MCP Client (Claude Code / Desktop) ───────────┐
             │  analysis_start / poll(long) / provide / answer / result  │
             │                    / drill                                │
             └────────────────────────────┬──────────────────────────────┘
                                          │
   ┌──────────────────────────────────────┴───────────────────────────────┐
   │                        MyHarness MCP Server                          │
   │                                                                      │
   │   analysis_provide(payload)                                          │
   │        │ 落 blob（0 token）                                          │
   │        ▼                                                             │
   │   ┌─────────┐  metadata + bounded sample + routing_table             │
   │   │  Proxy  │  (haiku, stateless, 單次)                              │
   │   └────┬────┘                                                        │
   │        │ {lane, reason}  ──── ~40 token 通知 ───┐                    │
   │        ▼                                        ▼                    │
   │   ┌─────────────────┐   dispatch(blocking)  ┌───────────────┐        │
   │   │  Lane instances │◄──────────────────────│ Orchestrator  │        │
   │   │  txn-2024       │───── handle ~120t ───►│ (opus, 常駐)  │        │
   │   │  txn-2023       │                       │  plan.md      │        │
   │   │  kyc-docs       │   plan_update ───────►│  peek 預算    │        │
   │   │  synthesizer    │   (routing_table)     └───────────────┘        │
   │   └────────┬────────┘                                                │
   │            │ 讀寫                                                    │
   │   ┌────────▼──────────────────────────────────────────────┐          │
   │   │ Artifact Store (介面) — blobs / notes / index          │          │
   │   │ v1: FS + SQLite    →    v2: MinIO + MariaDB/Oracle     │          │
   │   └───────────────────────────────────────────────────────┘          │
   │   Event log (JSONL) + 每個 worker 的完整 transcript                   │
   └──────────────────────────────────────────────────────────────────────┘
```

## 3. 決策紀錄

| # | 議題 | 決策 |
|---|---|---|
| 1 | 資料流向 | MCP server；user 觸發分析，orchestrator 可要資料(push)，lane 可用工具撈(pull) |
| 2 | 生命週期 | Job-based 非阻塞，in-memory state，但 state 設計成可序列化 |
| 3 | 回傳契約 | Artifact + ~120 token handle；orchestrator **不彙整**，報告由 synthesizer lane 寫 |
| 4 | Proxy | Ingress gate。Haiku、stateless、只看 metadata + bounded sample。依 orchestrator 發布的 routing table 先斬後奏；匹配不到才升級 |
| 5 | Subagent 執行模型 | Ephemeral agent + durable lane state。同 lane 序列化、跨 lane 平行 |
| 6 | 實作方式 | 自包 `dispatch` custom tool + 嵌套 `query()`。契約由 `output_format` / `task_budget` / Python 程式碼強制 |
| 7 | Orchestrator context | 常駐 `ClaudeSDKClient` + peek 預算 + plan.md + 60% rolling restart 逃生門 |
| 8 | Lane 定義 | 靜態 LaneType（charter + 工具 + 模型 + 預算）+ 動態 instance |
| 9 | 問 user | Question queue + `analysis_poll(wait=N)` long-poll；`ask_user` 有 timeout/default/配額 |
| 10 | Artifact | blob/note 二分 + dispatch 授權 + FS&SQLite（介面化，未來 MinIO+MariaDB/Oracle） |
| 11 | 模型 | Anthropic API 直連，196k = 200k 扣 margin。Opus/Sonnet/Haiku 分層 |
| 16 | 後端可插拔 | 支援 OpenRouter / 自架 LiteLLM。`ClaudeAgentOptions.env` 可 per-lane 指定 `ANTHROPIC_BASE_URL` / `AUTH_TOKEN` / model（**spike #2、#3 實測可行**）。capabilities 宣告保留，但降級實作暫不需要 |
| 17 | 工具裁切 | 每個 worker 用 `disallowed_tools` 列掉不需要的內建工具，省 ~17.7k tokens（**spike #2b 實測**；`allowed_tools` 無此效果） |
| 12 | 失敗語意 | 失敗為值（不拋例外）+ transient/semantic 兩層 retry + 三道防迴圈 + graceful degradation |
| 13 | 可觀測性 | Event log 為地基 + worker transcript 落盤 + golden job 斷言 |
| 14 | 報告交付 | Executive summary + 章節 est_tokens 價目表 + `analysis_drill`；caveats 由 framework 自動蒐集 |
| 15 | 等待機制 | ~~`dispatch` 阻塞 + 同 turn 多 tool call~~ → **spike #1 推翻**。改為 `dispatch` 非阻塞（起 asyncio task，~5ms 返回）+ `await_tasks(ids, mode, timeout)` 單一阻塞呼叫收割 |

## 4. 關鍵契約

### 4.1 Lane handle（subagent → orchestrator）

**兩道機制疊加**（`myharness/lanes/handle.py`）：`HANDLE_SCHEMA` 由 API 層強制**形狀**
（後端不支援時退回應用層驗證 + 重新提示）；`clamp_handle()` 永遠強制**長度**
（逐欄位上限 + 整體序列化上限 2000 字元）。

JSON Schema 約束不了長度 —— 模型可以回一個符合 schema 但 headline 三千字的物件，
或把報告塞進 metrics 的鍵名。**少任何一道都只是「很可能」。**

```json
{
  "artifact": "lanes/txn-2024/findings/003",
  "headline": "3 類異常交易，最大宗為深夜小額高頻",
  "metrics": {"anomaly_rate": 0.023, "n": 30412},
  "confidence": "high",
  "followups": ["需要 KYC 資料交叉比對"]
}
```

失敗同樣是 handle：`{"status": "budget_exceeded" | "tool_failure" | "duplicate", "partial": ..., "headline": ..., "suggest": ...}`

### 4.2 Routing table（orchestrator → proxy）

```json
[
  {"lane": "txn-2024", "accepts": "2024 交易明細、金流紀錄", "status": "open"},
  {"lane": "kyc-docs", "accepts": "身分/KYC 文件",          "status": "open"},
  {"lane": "legacy",   "accepts": "2023 前系統 log",         "status": "closed"}
]
```

Orchestrator 用宣告式資料遙控 proxy，兩者零 context 共享。

### 4.3 LaneType（開發者宣告，framework 的擴充點）

```python
LaneType(
    name="tabular-analyst",
    charter_path=Path("charters/tabular-analyst.md"),   # 檔案，可 diff、吃 prompt cache
    tools=("read_note", "write_finding", "update_state", "localize_blob"),
    model_tier="strong",        # 能力層級，由 backend 解析成實際模型
    backend="openrouter",
    token_budget=80_000, max_turns=25, state_max_tokens=8_000,
)
```

已實作於 `myharness/lanes/types.py`。`model_tier` 而非硬編模型名稱，是為了讓同一個
lane type 能在不同 backend 上跑（見 §5b）。charter 的雜湊會寫進事件流，
使「這次跑的是哪一版 charter」事後可查。

Instance 由 orchestrator 在 `plan_update` 時建立；同型別多 instance 各持有獨立 state。

### 4.4 Artifact 二分

| | blob | note |
|---|---|---|
| 例 | CSV / log / PDF / parquet | 分析結論、plan.md、state.md |
| `read_artifact` | **拒絕**，回傳 schema 並指向工具存取 | 允許，但先查 index 的 `est_tokens` |
| 存取方式 | `inspect_blob` / `duckdb_query`（結果受兩道上限） | `read_artifact(id, section, budget)` |

**授權**：lane worker 只能碰自己的 namespace + `dispatch(inputs=[...])` 明確授權的 id。

**本地化**：`async with store.localize(blob_id) as path:` — 本地實作回真實路徑，MinIO 實作下載到 scratch 並清理。**第一天就要有**，否則上物件儲存時要重寫所有工具。

### 4.5 Orchestrator 的工具面（固定 6 個）

```
plan_update(lanes, routing_table)   建立/更新 lane instance 與 routing table
dispatch(lane, task, inputs)        非阻塞派工。asyncio.create_task 起 worker，
                                    ~5ms 回 {task_id, lane, status:"running"}
await_tasks(ids, mode, timeout)     單一阻塞呼叫，等 all/any 完成，回傳 handles
peek(artifact, section, budget)     有限額窺看細節（job 級總預算 30k）。readOnlyHint=True → 可併發
ask_user(question, kind, default)   進 question queue，等 answer 或 timeout（配額 5 次）
finish(report_artifact)             收工
```

**為什麼 dispatch 不阻塞**（spike #1 實測，詳見 `spikes/RESULTS.md`）：Claude Code 只對
`readOnlyHint=True` 的 tool 併發執行同 turn 的多個 call；`readOnlyHint=False` 或無 annotation
一律循序（實測 A[0→4] B[4→8] C[8→12]，間隔 5ms，確認是同 turn 循序而非多輪往返）。
`dispatch` 會寫 artifact 與 lane state，謊報唯讀不可接受。改成非阻塞後，dispatch 循序執行
也只花 ~15ms，真正的併發發生在 harness 自己的 event loop 裡，併發度由我們的 semaphore 決定，
不受 CLI 的 `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`（預設 10）約束。

典型節奏：
```
turn 1: dispatch(A) dispatch(B) dispatch(C)      → 三個 task_id
turn 2: await_tasks([a,b,c], mode="all")         → 三份 handle 一起回來
```
沒有空 poll（`await_tasks` 真的阻塞到有結果），每批 fan-out 只多一個小 turn。
`mode="any"` 免費附送 pipelining。

### 4.6 MCP 對外工具面

```
analysis_start(task)                 → {job_id, status}
analysis_poll(job_id, wait=30)       → {status, progress, questions[]}   long-poll
analysis_provide(job_id, payload)    → {blob_id, routed_to}
analysis_answer(job_id, qid, text)   → {ok}
analysis_result(job_id)              → summary + key_findings + caveats + sections[est_tokens] + cost
analysis_drill(job_id, section_id)   → 章節全文
```

### 4.7 Event log

```jsonl
{"t":"job.start","seq":0,"ts":"2026-08-18T15:04:05Z","job_id":"j7","task":"...","budget_usd":5.0}
{"t":"plan.update","lanes":[...],"routing_table":[...]}
{"t":"ingress","payload":"blob:raw/txns-2024","bytes":52428800,"sample_tokens":1840}
{"t":"proxy.route","payload":"...","lane":"txn-2024","reason":"...","tokens":{"in":2140,"out":48},"ms":890}
{"t":"dispatch.start","id":"d3","lane":"txn-2024","task":"...","inputs":[...]}
{"t":"dispatch.end","id":"d3","status":"ok","artifact":"...","tokens":{...},"turns":9,"usd":0.31,"transcript":"traces/d3.jsonl"}
{"t":"peek","artifact":"...","tokens":2400,"peek_budget_left":18600}
{"t":"ctx","who":"orchestrator","used":74210,"pct":0.38}
{"t":"job.finish","report":"report.md","usd":2.14,"dispatches":23}
```

每筆事件都帶 `t` / `seq` / `ts` / `job_id` 四個共通欄位，其餘一律放在同層的自由欄位 ——
新增欄位或新增整個事件型別因此永遠是純加法，讀取端對未知型別寬容。

成本報表、TUI、OpenTelemetry、回歸測試全部是這份 log 的投影。

**已實作的投影**：
- `myharness/events/`：`summarize()` 一次給出 context 峰值、重複 dispatch 數、
  總成本與 caveats；`derive_caveats()` 推導報告的「未做到什麼」。
- `myharness/dataflow/`：把事件排成資料流 —— 節點（原始資料／分析／報告／lane）
  與邊（**授權**／**產出**／**讀取**，三者分開），並偵測四種資料流異常。
- `myharness/monitor/`：`myharness inspect <job>` 事後展開，
  `myharness monitor <job>` 即時跟蹤。唯讀，對執行中的 job 零影響。

資料流投影的價值有實例：golden job 第五次在所有紀律指標上都是綠的，
但最終報告來自一次**沒有任何授權**的派工，並覆蓋掉了真正拿到 finding 的版本。
逐行讀事件流看不出來；排成流向是第一眼就看到的東西。該偵測現已成為 golden job 的斷言。

## 5. Context 預算（中型 job，<50 dispatch）

| 項目 | 原估計 | golden job 實測（5 dispatch） |
|---|---|---|
| System prompt + 6 工具定義 + LaneType 清單 | ~4k | 含在下方峰值內 |
| 使用者任務 | ~1k | — |
| plan 往返 | ~6k | plan 本身 64 tokens |
| handle 累積 | ~6k（40 × 150） | 5 × ~150 |
| peek（**硬預算上限**） | ≤30k | **0**（orchestrator 未使用） |
| Orchestrator 推理與 thinking | ~30–60k | 含在下方峰值內 |
| **合計** | **~77–107k** | **9,720（14 turns）** |

**實測遠低於估算，但兩者規模不同**：golden job 只有 5 次 dispatch，估算是針對 40 次。
以每次 dispatch 約 2k 的增量外推，40 次約落在 **40–50k**，仍在估算區間下緣。
真正的差異在於 orchestrator 幾乎不用 peek —— 它信任 handle 的 headline 就足以決策，
這正是 handle 契約想要的效果。

最大變數是 peek 與 thinking；peek 已由預算變成常數。60% (≈118k) 觸發 rolling restart，
golden job 從未接近（峰值 5%）。

## 5b. 後端可插拔（LiteLLM）

`ClaudeAgentOptions.env` 會併入子行程環境，因此每條 lane 可以指向不同後端：

```python
BackendProfile(
    base_url="https://litellm.internal/",
    auth_token_env="LITELLM_KEY",              # → Authorization: Bearer <token>
    model_map={"sonnet": "my-alias", ...},     # → ANTHROPIC_DEFAULT_SONNET_MODEL
    capabilities={"structured_output", "prompt_caching", "task_budget"},
)
```

**LiteLLM 必須原樣轉發**（spike #2 實測擷取）：`POST /v1/messages?beta=true`、
`anthropic-version: 2023-06-01`、以及 7 個 `anthropic-beta`
（`claude-code-20250219, interleaved-thinking-2025-05-14, thinking-token-count-2026-05-13,
context-management-2025-06-27, prompt-caching-scope-2026-01-05,
mid-conversation-system-2026-04-07, effort-2025-11-24`）。
body 含 `thinking:{"type":"adaptive"}`、`output_config`、`context_management`、
以及 system/messages 上的 `cache_control: ephemeral`。

### OpenRouter（目前的主要後端，spike #3 全項實測通過）

```python
BackendProfile(
    base_url="https://openrouter.ai/api",
    auth_token_env="OPENROUTER_KEY",
    model="anthropic/claude-sonnet-4.5",   # 直接用 OpenRouter model id
)
```

| 能力 | 直連 | OR/sonnet-4.5 | OR/gemini-2.5-flash | OR/gpt-4o-mini |
|---|---|---|---|---|
| 7 個 `anthropic-beta` | ✅ | ✅ | ✅ | ✅ |
| prompt caching 命中 | ✅ | ✅ | ✅ | ✅ |
| 結構化輸出 `--json-schema` | ✅ | ✅ | ✅ | ✅ |

**決策 #6 的「強制 vs 祈禱」在 OpenRouter 上完整成立**，連非 Anthropic 模型都能吐出
符合 schema 的 handle。原先預想的 translation 模式降級路徑**實測後證實不需要**：

| 缺少的能力 | 降級做法（保留設計，暫不實作） |
|---|---|
| `structured_output` | 應用層驗證：解析 JSON → jsonschema → 失敗 re-prompt（上限 2 次） |
| `prompt_caching` | charter/state 前綴不再免費 → ephemeral worker 成本模型重算 |
| `task_budget` | 改用 harness 端本地 token 計數硬斷 |

**proxy lane 可用便宜模型** —— 單次、無狀態、低難度分類，
而且 routing 決策仍能用結構化輸出強制。

⚠️ **OpenRouter 的 `:free` 變體不適合當主力**（spike #5）：
`nemotron-3-ultra-550b-a55b:free` 不宣告結構化輸出，且 12 分鐘跑不完三個短請求；
`nemotron-3-super-120b-a12b:free` 雖宣告支援，但會撞到
`free-models-per-day-high-balance` 的每日配額而回 429。付費變體
（`nemotron-3-super-120b-a12b`，$0.08/M in、$0.40/M out）無此限制。

⚠️ **`task_budget` 超限時會拋例外，不會回部分結果**（spike #3c 實測）。
`run_lane_worker` 必須在串流過程中累積訊息，例外時轉成 `budget_exceeded` handle —— 
`ResultMessage` 不會來。

## 5c. 每個 worker 的固定 prefix 成本

CLI 即使在 `setting_sources=[]` 下仍會送出全部內建工具定義（spike #2b 實測）：

| 設定 | tool 定義 tokens |
|---|---|
| 預設 | ≈18,944 |
| `allowed_tools=[...]`（**無效**，只管自動核准） | ≈18,944 |
| `disallowed_tools=[全部內建]` | ≈1,184 |

**每個 LaneType 必須明確列出 `disallowed_tools`**，否則每個 ephemeral worker 白付
~17.7k tokens（196k 的 9%）。orchestrator 同理 —— 它只需要自己那 7 個 custom tool。

## 6. 失敗語意

| 類型 | 處理者 | 行為 |
|---|---|---|
| Transient（429/500、網路、工具 timeout） | framework | 靜默重試 1–2 次，指數退避 |
| Semantic（超預算、格式錯、找不到、max_turns 用盡） | orchestrator | 做成 handle 回傳，由 orchestrator 決定縮小/換 lane/接受部分 |

**三道防迴圈**：
1. `dispatch(lane, task)` hash 重複 → 不執行，回 `{status:"duplicate", previous:<handle>}`
2. Job 硬上限 `max_dispatches` / `max_budget_usd` / `max_wall_clock` → 觸頂時發系統訊息要求立即收尾（**給機會善終，不硬砍**）
3. 連續 N 次 dispatch 未產生新 note → 升級 `ask_user` 或強制收尾

## 7. 目錄結構

```
jobs/<job_id>/
  blobs/<name>                  原始資料，永不進 context
  notes/                        LLM 產出的文字（id 為 <job>/note/<name>）
    plan.md                     orchestrator 的全局狀態（resume 與可觀測性）
    report.md
    lanes/<lane_id>/
      charter.md                角色定義（穩定前綴，吃 prompt cache）
      state.md                  累積認知，硬上限 8k；分 stable(只增) / working(可覆寫)
      findings/*.md             完整分析產出
  traces/d<n>.jsonl             每次 dispatch 的完整 worker transcript
  events.jsonl                  event log
  index.sqlite                  id → {kind, bytes, est_tokens, schema, sections,
                                      produced_by, created_at, revision}
```

blob 與 note 分屬兩棵子樹，因為兩者的存取規則完全不同（見 §4.4），
混在同一個命名空間會讓 `notes/blobs/x` 這種名稱產生歧義。
路徑組成由 `myharness/local_layout.py` 獨佔，其他模組一律走 store 介面
（由 `tests/unit/test_layout_is_private.py` 靜態檢查強制）。

## 8. 待驗證的 Spike（動工前）

1. ~~同 turn 多 custom MCP tool call 是併發還是循序？~~ **✅ 已完成** — 見 `spikes/RESULTS.md`。
   結論：只有 `readOnlyHint=True` 會併發，上限預設 10。決策 #15 已據此修正。
2. 巢狀 `query()`：在 custom `@tool` handler 內啟動另一個 `query()` 是否穩定、資源是否正常回收。
3. `output_config` / 結構化輸出 + `task_budget` 的實際行為：超預算時是拋錯還是回部分結果？部分結果拿不拿得到？
6. ~~LiteLLM 實機驗證~~ **✅ 已完成（改測 OpenRouter）** — 全項通過，見 `spikes/RESULTS.md` §Spike #3。
4. ~~Prompt caching 命中率~~ **✅ 已完成** — charter 當穩定前綴，直連 `cr=4780`、OR/sonnet `cr=2252`，命中。
5. MCP long-poll：`analysis_poll(wait=30)` 在 Claude Code 端會不會撞到 client 的 tool timeout。
   （已知：`MCP_TOOL_TIMEOUT` 預設 1e8ms≈27.8h；SDK in-process tool 靜默阻塞 **180s 與 600s 實測皆通過**。）

## 9. 開放項目

### 已由實測定案

| 項目 | 定案值 | 依據 |
|---|---|---|
| `est_tokens` 估算 | ASCII/4 + 非 ASCII×1.5 | 單一 4 chars/token 會低估中文 4–6 倍，而低估正是會炸掉 worker 的方向 |
| Handle 欄位上限 | headline 200 字元、整體序列化 2000 | live 實測 handle 為 215–216 字元；被要求寫 3000 字報告時仍在界內 |
| 工具裁切 | 每個 LaneType 明確宣告，其餘 `disallowed_tools` | 省 ≈16,468 tokens/worker（196k 的 8.4%），spike #7 |
| 兩個資料工具的定義成本 | ≈150 tokens/worker turn | 把裁切省下的 16,468 還回去 0.9%；相對 60k lane 預算 0.25% |
| Transient 重試 | 時間預算 300s、退避 4s→60s 加 full jitter、per-backend 共享閘 | 次數上限會在限流恢復前就放棄；spike #6 |
| SDK 內建重試 | `CLAUDE_CODE_MAX_RETRIES=2` | 預設 10 次會在單一呼叫內耗掉數分鐘，harness 看不到也協調不了 |
| 降級路徑重試 | 2 次後回失敗 handle | 離線驗證 |
| OpenRouter 的 `task_budget` | **不宣告** | 預算不足時回 400、output 為 0，拿不到部分結果；spike #6 |
| 單次 lane 執行成本 | ≈$0.02–0.04（nemotron-3-super-120b） | spike #7；SDK 回報值高於實際帳單，成本斷言以帳單為準 |
| Blob 的存取方式 | `inspect_blob` + `duckdb_query`，SQL 內不含路徑 | 路徑入 SQL 需要第二套 parser 級授權，兩套裡弱的那套決定安全性 |
| 查詢沙箱 | ingest 進記憶體後 `enable_external_access=false` | spike #10 走過十二條逃逸路徑；`allowed_paths` 是加法不是減法，擋不住 |
| Blob 大小上限 | 256 MiB | view 是惰性的，關門後才讀 —— 上限是沙箱的形狀，不是效能調校 |
| 查詢結果上限 | 50 列 **且** 4000 字元，大結果走 `into` | 40 欄 × 180 字 × 20 列在列數限內仍可灌爆 context |

### 仍然開放

- **Lane state 的 8k 上限**：尚未在真實長 job 中撞到過。撞牆會產生一筆降級事件，
  那筆事件就是設計壓縮策略所需的資料 —— 在有它之前做壓縮只是猜。
- **Lane state 的 stable / working 分區**：目前是 charter 的文字慣例，store 不理解其結構。
  若壓縮策略需要程式介入才要改。
- **事件流的 schema 版本欄位**：傾向需要，但可在第一次破壞性變更時才加入。
- **Proxy 的觸發範圍**：目前只在 `analysis_provide`。lane 內部的大型 tool result
  是另一個問題（需要 tool-result 過濾器，不是 router）—— 待遇到再處理。
- **跨 lane 平行上限**：`BackendGate` 已限制 per-backend 並行數（預設 4），
  但跨 backend 的總並行仍無上限。
- **Charter 的撰寫規範**：`myharness/lanes/README.md` 已有基本要求，
  完整指南需要真實 job 的經驗才寫得出有用的版本。
- **跨 job artifact 共享**：v1 為 job-scoped，但 id 設計成全域唯一
  （`<job_id>/<kind>/<name>`）以便日後開放。
- **非表格 blob**：`duckdb_query` 讀 CSV / Parquet / JSON。純文字與日誌目前只能
  `localize_blob`，沒有 `grep_blob`。等到真的有這種 job 再決定它的輸出上限長什麼樣。
- **對外的 MCP server**：DESIGN #1/#9/#14 的那一層仍未建。這是目前唯一擋著
  「能不能直接使用」的東西。
