# Spike 結果

環境：`claude-agent-sdk` 0.2.139、Claude Code CLI 2.1.234、Python 3.13.5、model=sonnet。

> ⚠️ 環境中的 `ANTHROPIC_API_KEY` 無效（401，重試 10 次後失敗）。所有 spike 都在
> 腳本開頭 `os.environ.pop("ANTHROPIC_API_KEY")` 讓子行程走 claude.ai 登入。

---

## Spike #1 — 同 turn 多個 custom MCP tool call：併發還是循序？

**結論：取決於 `readOnlyHint`。這是唯一的開關。**

`spike01b_annotation_matrix.py`，每次呼叫 sleep 4 秒：

| Annotation | 時間軸 | peak concurrency | 判定 |
|---|---|---|---|
| `readOnlyHint=True` | A[0.0→4.0] B[0.6→4.6] C[0.6→4.6] | 3 / 3 | **併發** |
| `readOnlyHint=True, destructive=True, idempotent=False` | A[0.0→4.0] B[0.5→4.5] C[0.6→4.6] | 3 / 3 | **併發** |
| `readOnlyHint=False, idempotent=True` | A[0.0→4.0] B[4.0→8.0] C[8.0→12.0] | 1 / 3 | **循序** |
| `annotations=None` | A[0.0→4.0] B[4.0→8.0] C[8.0→12.0] | 1 / 3 | **循序** |
| `readOnlyHint=True`, n=6 | 0.0 / 0.5 / 1.0 / 1.6 / 2.0 / 2.0 起跑 | 6 / 6 | **併發** |

- `destructiveHint` / `idempotentHint` / `openWorldHint` **完全不影響**。
- 循序案例中，B 在 A 結束後 **5 毫秒**就啟動 —— 證明是同一 turn 內循序執行，
  而非模型多輪往返（往返會是 1–3 秒間隔）。
- 併發時每個 call 之間有 ~0.4–0.5s 的啟動 stagger，n=6 時實際併發度 ~4x 而非 6x。
- 從 CLI binary 取得：`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY ?? 10` → **併發上限預設 10**。

**注意**：SDK 會把同一 turn 的每個 `tool_use` 拆成**獨立的 `AssistantMessage`**，
所以「每則 AssistantMessage 的 tool_use 數量」不能拿來判斷是否同 turn。要看時間軸重疊。

### 對決策 #15 的影響

原決策「`dispatch` 阻塞，平行度靠同 turn 多 tool call」**不成立** ——
`dispatch` 會寫 artifact 與 lane state，不是唯讀。把它標成 `readOnlyHint=True`
可以騙到併發，但那是謊報語意，且 CLI 可能對唯讀工具另有假設（自動核准、重試、rewind）。

**修正後的設計（比原本兩個選項都好）：**

```
dispatch(lane, task, inputs)        非阻塞。asyncio.create_task 起 worker，
                                    立刻回 {task_id, lane, status:"running"}（~20 tokens）
await_tasks(ids, mode, timeout)     單一阻塞呼叫，等到 all/any 完成，回傳 handles
```

```
turn 1: dispatch(A) dispatch(B) dispatch(C)   ← 循序執行也無所謂，每個只花 ~5ms
        （worker 在 harness 自己的 event loop 裡真正併發）
turn 2: await_tasks([a,b,c], mode="all")      ← 單一 call，不需要併發
        → 三份 handle 一起回來
```

- **不需要謊報 annotation**，`await_tasks` 是單一呼叫，`readOnlyHint` 無關。
- **沒有空 poll** —— `await_tasks` 真的阻塞到有結果。
- **併發度由我們自己的 semaphore 決定**，不受 CLI 的 10 上限約束。
- `mode="any"` 免費附送 pipelining 能力。
- 成本：每批 fan-out 多一個 orchestrator turn，且該 turn 輸入很小。

順帶：`peek` 是唯讀，可以標 `readOnlyHint=True` 拿到併發（一次 turn 內窺看多個 artifact）。

## Spike #1c — 阻塞型 tool call 的時間上限

從 CLI binary 靜態分析：

- `MCP_TOOL_TIMEOUT` 預設 `1e8` ms ≈ **27.8 小時**，上限 `2147483647` ms。
  per-server `timeout` 可覆寫（<1000ms 忽略）。「Hard wall-clock limit per call;
  progress notifications do not extend it.」
- `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`：無回應／無進度通知即中止，**設 0 可停用**。
  預設值未能從 binary 確認。

實測結果：見下方（`spike01c_blocking_duration.py 180`）。

**實測**：`spike01c_blocking_duration.py 180` → `blocked_s=180.0, wall=187.2s, is_error=False`。
`spike01c_blocking_duration.py 600` → `blocked_s=600.0, wall=606.6s, is_error=False`。
**SDK in-process MCP tool 靜默阻塞 180 秒與 600 秒都沒有被 idle timeout 中止。**

不過設計上**不該依賴這個** —— 讓 `await_tasks` 自己有 timeout（例如 240s），到時回傳
`{completed: [...], still_running: [...]}` 讓 orchestrator 再呼叫一次。這樣完全不依賴
CLI 的內部限制，代價只是偶爾多一個小 turn。

---

## Spike #2 — 指向自架 LiteLLM

架了會錄音的假 `/v1/messages`（`spike02_litellm_passthrough.py`），完整擷取存於
`spike02_captured.json`。

### ✅ 可行，而且可以 per-lane 指定不同後端

`ClaudeAgentOptions.env` 會併入子行程環境（`subprocess_cli.py:813`），所以每次
`query()` 可以指向不同 endpoint：

```python
ClaudeAgentOptions(env={
    "ANTHROPIC_BASE_URL": "https://litellm.internal/",
    "ANTHROPIC_AUTH_TOKEN": "sk-litellm-...",        # → Authorization: Bearer <token>
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "my-alias",    # alias 重新映射，實測生效
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
})
```

實測結果：請求確實打到假 endpoint，body 的 `model` 變成 `"my-litellm-alias"`。
CLI 會印 `[claude-code:unrecognized_model]` 警告但正常繼續。

### CLI 實際送出的內容 —— LiteLLM 必須原樣轉發這些

```
POST /v1/messages?beta=true                      ← 注意 query param
Authorization: Bearer <ANTHROPIC_AUTH_TOKEN>
anthropic-version: 2023-06-01
anthropic-beta: claude-code-20250219,
                interleaved-thinking-2025-05-14,
                thinking-token-count-2026-05-13,
                context-management-2025-06-27,
                prompt-caching-scope-2026-01-05,
                mid-conversation-system-2026-04-07,
                effort-2025-11-24
X-Stainless-Timeout: 600
```

body 欄位：`model, stream, max_tokens(32000), system, messages, tools,
thinking={"type":"adaptive"}, output_config={"effort":"high"},
context_management={"edits":[{"type":"clear_thinking_20251015","keep":"all"}]}, metadata`

**prompt caching 是真的在用**：system 3 個 block 中有 2 個帶 `cache_control: ephemeral`，
messages 也有 1 個。

### ⚠️ 風險：LiteLLM 的兩種模式差很多

| | passthrough（轉發到真 Anthropic） | translation（轉成 OpenAI/Gemini/本地模型） |
|---|---|---|
| `anthropic-beta` 7 個 beta | 原樣轉發即可 | 無意義，會被丟棄 |
| `cache_control` | 有效 → charter/state 前綴快取成立 | **失效**，每個 worker 全額付費 |
| `output_config` / 結構化輸出 | 有效 → handle 契約可強制 | 多半失效 → 退回應用層驗證 + retry |
| `context_management` / `thinking:adaptive` | 有效 | 失效或報錯 |
| **對設計的影響** | 全部機制成立 | 決策 #6 的「強制」退回「祈禱」 |

**translation 模式下必須加的降級路徑**：
- handle 契約改由應用層驗證（解析 JSON → jsonschema 驗證 → 失敗就 re-prompt，上限 2 次）
- 沒有 prompt caching → ephemeral worker 的成本模型要重算
- `task_budget` 若不支援 → 改用本地 token 計數在 harness 端硬斷

---

## Spike #2b — 內建工具定義的 token 成本（意外的大發現）

即使 `setting_sources=[]`，CLI 仍會把**全部內建工具定義**送進每次請求。
對 ephemeral worker 架構而言，這是每個 worker 都要付的固定成本。

| 設定 | 剩餘工具 | tool 定義 tokens | prefix 合計 |
|---|---|---|---|
| baseline（什麼都不設） | 20 | **≈18,944** | ≈20,785 |
| `allowed_tools=["mcp__lane__noop"]` | 20 | ≈18,944 | ≈20,785 |
| `disallowed_tools=[全部內建]` | 2 (Glob, Grep) | **≈1,184** | ≈1,421 |
| `disallowed_tools` + SDK MCP server | 3 | ≈1,223 | ≈1,460 |

**結論：`disallowed_tools` 會真的把工具從請求裡拿掉，`allowed_tools` 不會**
（後者只管自動核准，不影響 payload）。

**每個 lane worker 用 `disallowed_tools` 列掉所有不需要的內建工具，省下 ~17.7k tokens
（≈196k 的 9%）。** 上面殘留的 Glob/Grep 只是因為沒列進去，列全就接近歸零。

這條對 orchestrator 同樣適用 —— 它只需要自己那 7 個 custom tool。


---

## Spike #3 — OpenRouter 作為後端（取代 LiteLLM）

**OpenRouter 有 Anthropic-compatible 的 `/api/v1/messages`**，claude-agent-sdk 可以直接指過去。

```python
ClaudeAgentOptions(
    model="anthropic/claude-sonnet-4.5",          # 直接用 OpenRouter model id
    env={"ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
         "ANTHROPIC_AUTH_TOKEN": "<OPENROUTER_KEY>",
         "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
)
```

CLI 會印 `[claude-code:unrecognized_model]` 警告（因為不是內建 alias），但正常運作。
也可改用 `ANTHROPIC_DEFAULT_SONNET_MODEL` 把 alias 映射過去。

### 能力矩陣（全部實測）

| 能力 | Anthropic 直連 | OR / claude-sonnet-4.5 | OR / gemini-2.5-flash | OR / gpt-4o-mini |
|---|---|---|---|---|
| 7 個 `anthropic-beta` header | ✅ | ✅ | ✅ | ✅ |
| `output_config` / `context_management` / `thinking:adaptive` | ✅ | ✅ | ✅ | ✅ |
| 工具呼叫（SDK in-process MCP） | ✅ | ✅ | — | — |
| **prompt caching 命中** | ✅ `cr=4780` | ✅ `cr=2252` | ✅ `cr=1987` | ✅ `cr=1792` |
| **結構化輸出 `--json-schema`** | ✅ | ✅ | ✅ | ✅ |
| `task_budget` 硬預算 | — | ✅（見下） | — | — |

原始數據：
- caching（相同 charter 前綴連跑兩次）
  - 直連：`run1 cc=5306 cr=0` → `run2 cc=526 cr=4780`
  - OR/sonnet：`run1 cc=2389 cr=0` → `run2 cc=137 cr=2252`
  - OR/gemini-flash：`run1 cc=1987 cr=1987` → `run2 cc=0 cr=1987`
  - OR/gpt-4o-mini：`run1 cc=0 cr=0` → `run2 cc=0 cr=1792`
- 結構化輸出：三個模型都吐出合法的 handle 物件（gpt-4o-mini、gemini-flash 也包含在內）

### 結論：§5b 的「translation 模式降級路徑」目前不需要做

我原本擔心走非 Anthropic 模型會弄丟 `cache_control` 與結構化輸出 ——
**實測顯示 OpenRouter 的 Anthropic-compat 層兩者都保住了，連 gpt-4o-mini 與
gemini-2.5-flash 都能吐出符合 schema 的結構化輸出。**

所以 `BackendProfile.capabilities` 這個結構保留（宣告成本近乎零），
但**降級實作先不要寫**，等真的遇到不支援的後端再說。

**額外收穫**：proxy lane（單次、無狀態、低難度分類）可以用
`google/gemini-2.5-flash` 之類的便宜模型，而且 routing 決策仍可用結構化輸出強制。

### ⚠️ `task_budget` 的行為要注意

`task_budget={"total": 200}` 配上一個需要更多 token 的任務 →
SDK **拋出例外**（`Claude Code returned an error result: success`，即 `is_error=True`
後 CLI 非零離開），而不是回傳部分結果。

**對決策 #12 的實作要求**：`run_lane_worker` 必須在串流過程中就累積訊息，
在例外發生時把已收到的內容轉成 `{status: "budget_exceeded", partial: ..., headline: ...}` handle。
不能等 `ResultMessage` —— 它不會來。

---

## Spike #2 — 巢狀 `query()` 的穩定性與資源回收

`dispatch` 會在一個 SDK in-process `@tool` handler 裡啟動另一個 `query()`。
每個 `query()` 會 spawn 一個 claude CLI 子行程 —— 若不回收，一個 40 次 dispatch 的 job
會留下 40 個殭屍行程。

`spike04_nested_query.py`（model=haiku，Anthropic 直連）：

| Phase | 內容 | Δ子行程 | Δfd | 結果 |
|---|---|---|---|---|
| A | 20 次序列 `query()` | +0 | +0 | ✅ 回收正常，20/20 成功 |
| B | 3 次在 `@tool` handler 內的巢狀 `query()` | +0 | +0 | ✅ 回收正常，巢狀語意正確 |

Phase B 的 parent agent 確實拿到了 child 的輸出（`SUB[t0]` / `SUB[t1]` / `SUB[t2]`），
證明 `dispatch` 的實作形狀可行。**決策 #6 的實作前提成立。**

---

## Spike #5 — Nemotron on OpenRouter

### Model metadata（`/api/v1/models`）

| 模型 | ctx | tools | structured_outputs |
|---|---|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b`（付費） | 512k | ✅ | ✅ |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 1M | ✅ | **❌** |
| `nvidia/nemotron-3-super-120b-a12b:free` | 262k | ✅ | **✅** |
| `nvidia/nemotron-3-nano-30b-a3b:free` | 256k | ✅ | ❌ |

### ⚠️ `ultra:free` 實務上不可用

`spike03_openrouter.py nvidia/nemotron-3-ultra-550b-a55b:free` 執行 **超過 12 分鐘
仍未完成三個短請求**，最後中止。free tier 的排隊延遲對一個要 fan-out
多個 worker 的 harness 是致命的。

**結論**：專案採用 `nvidia/nemotron-3-super-120b-a12b:free` 作為 OpenRouter 的
預設模型 —— 同樣免費、262k context、且**宣告支援結構化輸出**，
決策 #6 的「強制」路徑因此在免費層仍然成立。

### ⚠️ `:free` 每日配額（阻擋 live 測試）

`nemotron-3-super-120b-a12b:free` 三個測試中兩個回 429：

```
Rate limit exceeded: free-models-per-day-high-balance
```

SDK 內建重試 10 次後仍失敗（每次耗時 ~170s）。金鑰本身**不是** free tier
（`is_free_tier: false`，週額度 $50 剩 $49.94）—— 這是 OpenRouter 對 `:free`
變體的每日請求上限，與帳戶餘額無關。

### 付費變體價格

| 模型 | in | out | cache read | ctx | struct |
|---|---|---|---|---|---|
| `nvidia/nemotron-3-super-120b-a12b` | $0.08/M | $0.40/M | — | 1M | ✅ |
| `nvidia/nemotron-3-nano-30b-a3b` | $0.05/M | $0.20/M | $0.03/M | 262k | ✅ |
| `nvidia/nemotron-3.5-lightning` | $0.08/M | $0.20/M | $0.04/M | 1M | ✅ |
| `nvidia/nemotron-3-ultra-550b-a55b` | $0.60/M | $3.60/M | $0.20/M | 512k | ✅ |

整套 live 測試（6 個、預估 ~150k in / ~30k out）在 super-120b 付費版上約
**$0.02**。

---

## Spike #6 — 失敗在真實後端上長什麼樣（推翻了一個設計假設）

Live 測試把「預算耗盡」與「回合用盡」都歸類成 `tool_failure`。
`spike06_failure_shapes.py` 直接印出訊息序列，結果推翻了 design.md D1 的部分前提。

| 設定 | 結果 |
|---|---|
| `task_budget={"total": 600}`（不足） | `ResultMessage` **會來**：`subtype="success"`、`is_error=true`、`terminal_reason="api_error"`、`api_error_status=400`、`output_tokens=0`、`num_turns=1`，然後 SDK 拋例外 |
| `task_budget={"total": 40000}`（充足） | 正常完成，`is_error=false` |
| 不帶 `task_budget` | 正常完成 |
| `max_turns=1` | **沒有** max_turns 錯誤 —— 模型一回合就答完了 |

### 三個推論

**1. `ResultMessage` 會抵達（與 spike #3c 的觀察相反）。** D1 說「等 `ResultMessage`
會什麼都拿不到」在 Anthropic 直連上成立，但在 OpenRouter 上它會來 —— 只是帶著
`is_error=true`。所以 `_classify` 不能用「有沒有收到 `ResultMessage`」當主要依據，
改用 `terminal_reason` 與 `api_error_status` 這兩個事實欄位。

**2. OpenRouter 的 `task_budget` 不是優雅的預算，是 400。** 預算不足時整個請求被拒絕，
`output_tokens=0` —— **拿不到任何部分結果**，而部分結果正是超預算時最需要的東西。
因此 `OPENROUTER` profile **不再宣告 `TASK_BUDGET`**，改用 harness 端的本地 token 計數。
這正是 design.md D7 預期的情況：capability 宣告錯了，由 live 測試暴露。

**3. `thinking_tokens` 系統訊息一次會來數百則。** 原本每則都寫進 transcript，
造成 transcript 嚴重膨脹。改為只累計次數。

### 附帶：`ANTHROPIC_API_KEY` 會蓋掉 `ANTHROPIC_AUTH_TOKEN`

開發機環境裡若殘留一把（可能已失效的）`ANTHROPIC_API_KEY`，它的優先權高於
`ANTHROPIC_AUTH_TOKEN`，會讓自訂 endpoint 的驗證走錯路。
`BackendProfile.to_sdk_env()` 在設定 `base_url` 時一併把它清空。

---

## Spike #7 — Lane worker 的實際量測（task 8.7）

### 固定 prefix 成本（離線、錄音端點、零成本）

以真實的 `run_lane_worker` 設定攔下請求比較：

| 設定 | tools | tool 定義 tokens | system | prefix 合計 |
|---|---|---|---|---|
| 未裁切 | 29 | ≈16,971 | ≈233 | ≈17,204 |
| `disallowed_tools` 裁切後 | 5 | ≈503 | ≈233 | **≈736** |

**每個 ephemeral worker 省下 ≈16,468 tokens（196k 的 8.4%）。**
這是**每一次** dispatch 都省 —— 一個 40 次 dispatch 的 job 就是 65 萬 token。

### 實際執行成本（live，`nemotron-3-super-120b-a12b`）

同一條 lane 連跑兩次「寫一句話進 finding 並回報 handle」：

| | tokens in | tokens out | turns | usd | handle |
|---|---|---|---|---|---|
| d1 | 6,178 | 940 | 6 | $0.044 | 216 chars |
| d2 | 6,454 | 1,133 | 6 | $0.040 | 215 chars |

節流等待 44.7s，無 caveats，兩次皆 `ok`。

**帳單核對**：OpenRouter 的 `usage` 從 $5.5882 → $5.6087，
整段 live 工作（含 28 分鐘的完整測試套件與多次探測）實際計費 **$0.0205**。
SDK 回報的 `total_cost_usd` 明顯高於實際帳單，成本斷言應以帳單為準。

### ⚠️ 事件裡看不出 cache 命中（已修）

`tokens_in` 原本把 `input_tokens`、`cache_read`、`cache_creation` 加總成一個數字，
使 prompt cache 的效果無法從事件流量測 —— 而 ephemeral worker 的成本模型**完全建立在
charter 前綴會被快取**這個前提上。現在 `dispatch.end` 的 `tokens` 拆成
`in / out / fresh_in / cache_read / cache_write`，並新增 `cache_hit_ratio()` 聚合。

### 單次執行的 handle 大小

live 實測 215–216 字元，上限 2000。即使被要求寫 3000 字報告，handle 仍在上界內。

---

## Spike #9 — Golden job（orchestrator 端到端）

五次跑才通。每一次擋住的都是不同的東西，而且**其中兩個是離線測試在原理上抓不到的**：

| 次 | 表面現象 | 根因 |
|---|---|---|
| 1 | 14 次 dispatch 全 401，燒完整個預算 | fixture 的 lane backend 用了預設值而非 job 的 backend；且無進展偵測當時只是勸告 |
| 2 | 0 次 dispatch，1 turn 就回報 `finished` | **loop 把每則訊息都丟掉**（`_ = message`），看不見 turn 做了什麼 |
| 3 | 1 次 dispatch 後停止 | 已完成工作的 turn 尾端撞限流，被當成致命錯誤 |
| 4 | 端到端走完，報告寫「我什麼都讀不到」 | `inputs` 傳物件被 `str()` 毀損成無效 grant，且被靜靜接受 |
| 5 | **通過** | — |

第 2 與第 4 是離線測試抓不到的：腳本化 session 本來就不呼叫工具（盲點被寫進測試前提），而離線測試都傳格式正確的字串。

### 第五次的量測

```
phase=complete  salvaged=False  turns=14  handoffs=0
context_peak=9,720 / 196,000    dispatches=5  duplicates=0  failures=0
cost=$0.4440  cache_hit=0.735   throttle=534.9s  peek=0
caveats=['limit_reached', 'unanswered_question', 'unanswered_question']
```

九項紀律斷言全過：交付存在、context 峰值 9,720 < 120k、peek 在預算內、零重複
dispatch、成本 $0.444 < $1.00、交付 1,442 字元 < 4,000、報告由 `lane:syn1` 產出、
caveats 與事件流一致、原始 blob 未變成 note。

### ⚠️ 暴露的能力缺口：沒有任何工具能處理 blob

Lane 成功 localize 到那份 138KB 的 CSV（拿到了本地路徑與 schema），然後**無事可做** ——
它只有 `read_note` / `write_finding` / `update_state` / `localize_blob` 四個儲存工具。

`charters/tabular-analyst.md` 寫著「大型資料一律用 `localize_blob` 取得路徑後以工具處理」，
但那個「工具」從來沒被建出來。Lane 誠實地把這件事寫進 finding，synthesizer 誠實地
寫進報告的「限制」—— 整條鏈的行為都是對的，缺的是能力本身。

**這不影響 golden job 的效力**：design.md D7 明確說它斷言的是紀律而非分析品質，
而紀律全數通過。但在 `duckdb_query` 之類的工具出現前，這個 harness 還不能真的分析資料。

> **已補上**（`add-tabular-tools`）：`inspect_blob` 與 `duckdb_query`，
> 沙箱設計見下方 Spike #10。

### 成本

`limit_reached` 是 `max_wall_clock_s`（1,863s，上限 1,800s）—— 其中 **534.9s
花在限流等待**，佔 29%。付費的 nemotron-3-super 仍會被上游限流。

---

## Spike #10 — DuckDB 能不能鎖到不破壞 grant model？

`spikes/spike10_duckdb_sandbox.py`（duckdb 1.5.5，`exit 0` 表示無洩漏）

給 worker 一個 SQL 引擎，等於給它一個檔案讀取器。design.md D2 的 grant model
只在「worker 沒有繞過的辦法」時才成立，所以這個 spike 只問一件事：
**ingest 之後，worker 的 SQL 能不能碰到任何沒被授權的東西？**

### 沙箱設定（順序有意義）

```sql
-- 先 ingest：view 是惰性的，table 不是。授權的檔案要在關門前讀進來。
CREATE TABLE t AS SELECT * FROM read_csv_auto('<granted blob>');
SET enable_external_access=false;
SET allow_community_extensions=false;
SET autoinstall_known_extensions=false;
SET autoload_known_extensions=false;
SET lock_configuration=true;    -- 必須最後：把上面全部凍結
```

### 逃逸嘗試結果

| 嘗試 | 結果 |
|---|---|
| 讀已授權的 table | ✅ 可用（合法路徑沒被鎖死） |
| `read_csv_auto('/未授權.csv')` | blocked `PermissionException` |
| glob `.../*.csv` | blocked |
| `ATTACH '...duckdb'` / `ATTACH ... (TYPE sqlite)` | blocked（後者連 extension 都載不了） |
| `COPY t TO '...'`（把授權資料寫出去） | blocked |
| `SET enable_external_access=true` | blocked `InvalidInputException` |
| `SET lock_configuration=false` | blocked |
| `INSTALL httpfs` / `LOAD httpfs` | blocked |
| `read_csv_auto('https://...')` | blocked |
| `read_text('/未授權')` | blocked |
| `getenv('OPENROUTER_KEY2')` | 函式不存在 |
| `duckdb_settings()` | **allowed —— 接受** |

`duckdb_settings()` 只暴露沙箱自己的組態（temp dir、extension dir），
沒有任何授權資料的旁路，不值得為它加一層 parser 級封鎖。

### 兩個 duckdb 不會幫你擋的洞

1. **`execute()` 會跑多個 statement。**
   `"CREATE TEMP TABLE z AS SELECT 1 AS a; SELECT * FROM z"` 兩句都執行了。
   工具必須自己用 `duckdb.extract_statements()` 檢查 **恰好一句**。
2. **失控查詢不會自己停。** `SELECT count(*) FROM range(1e11) a, range(1e5) b`
   會跑到天荒地老；`conn.interrupt()` 從另一個 thread 呼叫 0.51s 內就中斷了
   （`InterruptException`）。所以牆鐘上限必須由 harness 用 timer + interrupt 執行。

`extract_statements()[0].type` 可分辨 `SELECT` / `CREATE` / `DROP` / `UPDATE`，
所以守門規則是：**恰好一句，且型別為 SELECT**。

### 修正：只有一道是圍籬

上面那張表最初被我讀成「兩道閘，缺一不可」。實測不是。
在 duckdb 1.5.5，**`enable_external_access=false` 自己守得住自己**：

```
SET enable_external_access=false   （不加 lock_configuration）
  re-enable external access    blocked  Cannot enable external access while database is running
  allowed_paths widen          blocked  Cannot change allowed_paths when enable_external_access is disabled
  allowed_directories widen    blocked  同上
  LOAD / INSTALL httpfs        blocked
  autoload_known_extensions=true   ALLOWED   <-- 唯一還能動的
```

所以：

- **圍籬只有一道**：`enable_external_access=false`。拿掉它，上面整張表全部重新打開。
- `lock_configuration=true` **不是第二道圍籬**，是縱深防禦：它釘住
  `autoload`/`autoinstall`（沒有它仍可改），並且讓「圍籬會自我防衛」這件事
  不是唯一的依靠 —— 那是 duckdb 的實作性質，不是它的 API 承諾。

留著它，但不能宣稱它是圍籬。這一條寫進 `tests/tabular/test_sandbox.py`：
一個測試證明拿掉圍籬全破，另一個測試證明鎖確實多釘住了 autoload。

### 被否決的替代方案：`allowed_paths`

`allowed_paths` / `allowed_directories` 看起來像 per-file allowlist ——
如果它成立，就能用惰性 view 直接查 blob 檔案，**沒有記憶體上限、沒有 ingest 成本**。

它不成立。只設 `allowed_paths=['<授權檔>']` 加上 `lock_configuration=true` 之後：

```
granted file     ALLOWED
UNGRANTED file   ALLOWED       <-- 同目錄的未授權檔案照讀
/etc/hosts       ALLOWED       <-- 1080 bytes
COPY over granted file  ALLOWED  <-- 把授權的 blob 覆寫掉了
```

這些設定是**加法**（在外部存取已開啟的前提下追加允許），不是減法。
而 `enable_external_access` 是啟動期選項：關掉之後無法再開
（`Cannot enable external access while database is running`），
所以也沒辦法「先關再針對性開一條縫」。

**結論：ingest 進記憶體再上鎖是唯一站得住的形狀，代價是 blob 必須有 byte 上限。**
這條負面結果已寫進 `spike10_duckdb_sandbox.py`，會隨 duckdb 升版一起被重測。

---

## Golden job 第六次 —— 第一次真的分析了資料

`add-tabular-tools` 之後的第一次 live 跑（nemotron-3-super-120b-a12b via OpenRouter）。

```
anomalies=none
phase=complete  salvaged=False  turns=5  handoffs=0
context_peak=6,757（orchestrator）/ 30,589（lane 峰值）
dispatches=3  duplicates=0  failures=0
cost=$0.2543  peek=0  throttle=129.6s  cache_hit=0.735
caveats=['unanswered_question', 'unanswered_question']
```

**十五項斷言全過**，包含兩項對第五次跑會失敗的新斷言。

### 數字是對的，不是看起來對

Lane 用 `duckdb_query` 算出來的每一個數字，都與直接對 CSV 下 SQL 的結果**完全相符**：

| | 報告 | 直接查詢 |
|---|---|---|
| distinct accounts | 765 | 765 |
| app 平均 | 13,981.81 | 13,981.81 |
| web 平均 | 20,612.47 | 20,612.47 |
| atm 平均 | 21,347.33 | 21,347.33 |
| branch 平均 | 21,655.25 | 21,655.25 |

第五次的報告寫的是「由於權限限制，未能讀取…」。這一次寫的是
「交易帳戶數為 765，且 app 通道平均金額最低（約 13,981.81）」。

### 與第五次的對照

| | 第五次 | 第六次 |
|---|---|---|
| 交付內容 | 「未能讀取資料」 | 五項具體發現，數字全對 |
| orchestrator context 峰值 | 9,720 | **6,757** |
| 成本 | $0.4440 | **$0.2543** |
| dispatches | 5 | 3 |
| 限流等待 | 534.9s（佔 29% 牆鐘） | **129.6s** |
| 資料流異常 | ungranted_production（CRITICAL） | 無 |

更便宜、更少 context、更少 dispatch —— 因為 lane 這次**一次就把事情做完了**，
不需要 orchestrator 反覆重派。能力補上之後紀律指標一起變好，不是巧合。

### 兩個值得記下的行為

1. **Orchestrator 派了 d2 去「取 blob 的前兩行」。** 它想驗證資料格式。
   這沒有違反不變式（lane 把兩行寫進 finding，orchestrator 沒有直接讀 blob），
   但顯示模型仍會試圖親眼看資料。charter 說得再清楚，它還是會問。

2. **兩個問題沒人回答，走了 default。** `ask_user` 的 timeout 生效，
   兩者都正確變成 `unanswered_question` caveat 出現在交付裡。
   其中 q1 問「lane 一直 timeout，blob 路徑對嗎？」—— 第一次 `duckdb_query`
   呼叫確實比較慢（要 ingest 138KB），orchestrator 把它讀成故障。
   這不是 bug，但值得記著：第一次查詢的延遲會被誤讀。

---

## Spike #11 — 真實 MCP client 驅動真實 server

`spikes/spike11_mcp_client.py`。前面所有測試都在 process 內；這個 spike 問的是
in-process 測試問不到的那件事：**客戶端把 `myharness-mcp` 當子程序 spawn、
用 stdio 講話時，整條鏈會不會動？**——也就是 Claude Code 實際的用法。

40 列 CSV、一個數得出來的問題，所以帳單是分錢等級、答案可以核對。

```
tools: [analysis_answer, analysis_drill, analysis_poll,
        analysis_provide, analysis_result, analysis_start]

start   → {job_id: job7ab30092db, state: running, revision: 0}
provide → {artifact: .../blob/raw/txn.csv, bytes: 1472,
           routed: false, announced: true}

[  30.5s] running   rev=3   ingress
[ 121.7s] running   rev=4   plan.update
[ 146.7s] running   rev=5   ask.user  → 客戶端回答 → rev=7 ask.answer
[ 179.8s] running   rev=8   dispatch.start d1 → ta1
[ 299.5s] running   rev=10  dispatch.start d2 → syn1   $0.0523
[ 453.0s] running   rev=11  dispatch.end   d2 ok: 7 distinct accounts
[ 493.9s] finished  rev=13  job.finish

result: 659 chars, 4 sections, 219 section tokens
  摘要 34 / 方法 69 / 發現 26 / 限制 90
drill '摘要': 28 chars, truncated=false
```

**5/5 檢查通過**：報告含算出來的帳戶數（7，`A{i%7}` 共 7 個，正確）、
result payload 659 字元（遠在 4,000 以內）、原始列沒有回流、章節都有標價、
drill 回得出內容。總計 494 秒、$0.1008。

### 這個 spike 抓到的三件事

1. **SDK 子程序的警告走 stderr，不會汙染 stdio 的 JSON-RPC。**
   `⚠ claude.ai connectors are disabled` 與 `[claude-code:unrecognized_model]`
   都在 stderr。如果它們走 stdout，MCP 協定會直接壞掉 —— 這是最容易踩到、
   而且症狀最難懂的一種失敗。

2. **`ClientSession` 的 `read_timeout_seconds` 預設是 `None`。**
   所以 `analysis_poll(wait=30)` 不會撞到客戶端逾時。
   這回答了 DESIGN §8 Q5（先前只驗證過 SDK in-process tool 阻塞 180s/600s，
   那不是真的 MCP client session）。

3. **Orchestrator 不知道 job 裡已經有什麼資料。**
   第一次跑，它花了**兩個問題**問一個 harness 早就知道的 artifact id
   （「Please provide the artifact ID for the transaction data」，
   重新規劃後又問一次）。

   `run_golden` 把 id 寫進 goal 字串所以看不出來；客戶端做不到 ——
   它先 `analysis_start` 再 `analysis_provide`，orchestrator 讀到 goal 的當下，
   裡面根本沒有 id 可以指。

   修法：kickoff 直接從 store 列出這個 job 的 blob（id、大小、宣告的欄位），
   不管事情發生的順序如何都正確。修完之後同樣的 job 只問了一個問題就開始派工。

---

## Spike #12 — 真實分類器，兩份資料，兩條 lane

`spikes/spike12_proxy_live.py`。離線測試用關鍵字分類器，證明了接線但沒證明
**一個便宜模型真的分得出來**。這個 spike 讓真的 nano 模型只看 routing table
加十二行樣本，判斷兩份 CSV 的歸屬。

```
[209.7s] routing table published: True

txn.csv   routed=True  -> txn  "a CSV of transaction records with fie..."
kyc.csv   routed=True  -> kyc  "a structured KYC CSV containing holder an..."

5/5 通過：表有發布、兩份各自路由正確、都通知了 orchestrator、
         回應裡沒有帶到任何一列資料
```

兩份都是 `confidence: high`，理由也對得上。**分類這件事，便宜模型做得到。**

### ⚠️ 每次分類有 ~8,400 tokens 不是我寫的

| | tokens |
|---|---|
| 我的 system prompt | 126 |
| 我的 user prompt（最壞情況樣本） | 493 |
| **合計自己寫的** | **619** |
| 實際計費 input | **8,991** |
| **差額** | **~8,372（佔 93%）** |

`disallowed_tools` 有生效（擋掉 31 個內建工具，spike #2b 量過那省下 16,468）。
剩下的 ~8.4k 是 **Claude Code CLI 自己的 base system prompt**，`disallowed_tools`
碰不到它。

對一條 60k 預算的 lane worker 來說，8.4k 是用 SDK 的已知代價。
對一個自己 prompt 只有 619 tokens 的分類器來說，**開銷是請求的 93%**。

### 這推翻了 D7 的前提嗎？

D7 說「proxy 每份資料呼叫一次，所以用最便宜的模型」。模型層級的選擇沒錯，
但省下的是分母裡比較小的那一項 —— 真正的成本是固定開銷，換模型不會改變它。

**跟進方向：proxy 不要走 SDK。** 它是單次、無工具、無 session、
輸出一小段文字 —— 正好是 agent SDK 什麼都沒幫上的情境。直接打後端的 HTTP API
可以把 8,991 降到 ~700。這與之前那個沒跑的
`spikes/spike08_compare.py`（「不用 claude-agent-sdk 會怎樣」）是同一個問題，
而現在有一個具體的、值得優化的數字了。

### 金額數字不可信

事件記的是 `usd=0.0509`（txn.csv）。9k input 的 nano 模型不可能是這個價
（nano 約 $0.03/M，應該是 $0.0003 量級）。這與 spike #7 早就記下的
「SDK 回報值高於實際帳單」一致 —— **token 數是實數，金額是 SDK 的估計。**

已把 `model` 加進 `proxy.route` 事件：第一次看到數字不對時，
「到底跑的是哪個模型」在事件流裡答不出來。
