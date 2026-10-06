# MyHarness

以 `claude-agent-sdk` 建構的多 agent 資料分析 harness，對外有兩條路：
**MCP over stdio**（同機）與 **A2A over HTTP**（遠端，共用同一個 service）。

**要解決的問題**：單一 agent 的 196k context 在資料分析任務中極易耗盡。

**核心原則**：*外部化狀態 + 短命執行者*，遞迴套用在四個層級。
每一層都保證**原始資料不會進到上一層的 context** —— 由構造保證，不是由 prompt 祈禱。

| 層級 | 被保護者 | 手法 |
|---|---|---|
| 對外邊界（MCP／A2A） | 客戶端 agent 的 context | job-based API，只回摘要 + 章節價目表 |
| Orchestrator | 全局規劃者的 context | subagent 只回 ~120 token 的 handle |
| Lane worker | 執行者的 context | ephemeral agent + durable lane state |
| Artifact 讀取 | 任何讀取者 | blob 拒絕讀入 context，note 有 est_tokens 預檢 |

**先讀這個**：[`docs/introduction.md`](docs/introduction.md) —— 介紹與使用教學，
從安裝到加自己的 lane，含每一道上限的理由與實測數字。

完整設計見 [`DESIGN.md`](DESIGN.md)，實測結果見 [`spikes/RESULTS.md`](spikes/RESULTS.md)。

圖：[分層架構](docs/diagrams/layers.html)、[一次分析的資料流](docs/diagrams/dataflow.html)（用瀏覽器開，可點節點看對應原始碼；`*.json` 是產生它們的 archify 原稿）。

## 安裝

```bash
uv pip install -e ".[dev]"
```

需要一個後端。兩條路都可以：

```bash
# 託管：OpenRouter
OPENROUTER_KEY=sk-or-v1-...

# 自架：任何 Anthropic-compatible 的 proxy（LiteLLM 等）
HARNESS_PROXY_BASE_URL=http://host:4000
HARNESS_PROXY_MODEL=your-model
HARNESS_PROXY_KEY=sk-...            # 端點不需認證就留空
```

自架跑得完整條鏈 —— 見下面的 golden run #7／#8。

## 從 Claude Code 連接

```bash
claude mcp add myharness -- myharness mcp --root ./myharness-jobs --backend openrouter
```

（舊的 `myharness-mcp` 仍然可用，是同一個東西 —— 已經設定好的 MCP client 不必改。）

資料用 `path` 交進去：server 自己讀檔，資料不經過 client 的 context。可讀的目錄預設是 server 的工作目錄，用 `--allow-read DIR`（可重複）指定別處；範圍外的路徑、包括指出去的 symlink，一律拒絕。用 path 時 CSV 以外的格式（JSON、Parquet）也行。`payload` 仍然收，但那份資料在送出前已經進過 client 的 context 一次。

然後在對話裡：

```
analysis_start(task="分析這份交易資料，找出異常樣態")
analysis_provide(job_id=..., path="data/txn.csv")  # server 自己讀檔
analysis_poll(job_id=..., wait=30)          # 等到真的有進展才回
analysis_result(job_id=...)                 # 摘要 + 章節價目表
analysis_drill(job_id=..., section_id="方法") # 需要哪節才讀哪節
```

六個工具與其上限見 [`myharness/mcp/README.md`](myharness/mcp/README.md)，
完整教學見 [`docs/introduction.md`](docs/introduction.md)。

## 從遠端的 agent 連接（A2A）

MCP over stdio 要求客戶端把 `myharness-mcp` spawn 成子程序，所以只有同一台
機器上的 agent 用得到。A2A 是第二條路，**不取代第一條**——兩條共用同一個
`AnalysisService`。

```bash
uv pip install -e ".[a2a]"
myharness a2a --root ./myharness-jobs --backend openrouter
```

沒有自己的 console script，只在有人打 `myharness a2a` 時才起來，而且**只綁 loopback**：這條邊界目前沒有認證、沒有多租戶、
沒有速率限制，所以它拒絕聽在任何非 loopback 的位址上。要對外開之前，那三題
得先有答案。

agent card 在 `/.well-known/agent-card.json`，宣告兩個 skill：

| skill | 給什麼 |
|---|---|
| `analysis.result` | 摘要與章節價目表（**預設**，不含報告全文） |
| `analysis.sections` | 指定章節的全文，逐節取 |

價目表的 artifact 帶一個 `required=true` 的 extension URI —— 不認得這個約定的
客戶端會被協定告知它不認得，而不是默默把目錄當成報告讀。

**啟動一定要非阻塞**：一次分析跑幾十分鐘，而 `SendMessage` 預設會等到終局狀態
才回應。呼叫方必須設 `configuration.return_immediately`，然後用 `GetTask` 回來看。

**資料還是得從另一條路進去**：A2A 上還沒有 `analysis_provide` 的對應
（那在 design 的 Open Questions 裡，刻意沒答）。

**已知的牆**：job 只活在啟動它的那個 process 裡。`analysis_result` 與
`analysis_drill` 只讀事件流與 store，換 process 照樣答；但**進行中的 task 接不
回來**。A2A 的 `TaskState` 九個值裡沒有一個表示「存在但不在此程序執行中」，
所以被丟下的 job 回報 `FAILED` 加一段說明——種類上是錯的，但它是終局狀態，
呼叫方應該停止等待（spikes/RESULTS.md，spike #23）。

## 所有入口都在 `myharness` 底下

| 命令 | 做什麼 |
|---|---|
| `myharness jobs` / `inspect` / `report` / `monitor` | 唯讀檢視一個 job（`monitor --web` 開瀏覽器版） |
| `myharness mcp` | 以 MCP（stdio）提供分析服務 |
| `myharness a2a` | 以 A2A（只綁 loopback）提供分析服務，需要 `[a2a]` extra |
| `myharness golden` | 跑端到端的 golden job（會花錢） |
| `myharness compare` | 同一個 golden 問題，單一 agent 對 MyHarness（會花錢） |

`--root` 放在子命令前面，所有子命令共用，預設 `myharness-jobs`。`mcp`／`a2a`／`run`／`golden` 的其他選項看 `myharness <command> --help`。

## 不用 MCP 直接跑

`run` 走的是和 MCP 同一個 service：開 job、提供檔案、等進度、問你問題、印摘要。
job 跑在這個行程裡，Ctrl-C 就停；事件留在磁碟上，事後照樣能 `inspect`。
stdin 不是終端機（或加 `--no-input`）時，orchestrator 的提問會自動回答。

```bash
myharness run "找出 2024 年交易的異常樣態" goldens/txn-2024.csv -o report.md
myharness golden --backend openrouter             # 端到端的 golden job
myharness jobs                                    # 列出 job
myharness inspect golden                          # 資料流與異常
myharness monitor golden                          # 即時追蹤
```

## Lane 能做什麼

Lane worker 只有這些工具，沒有 CLI 的檔案工具：

```
read_note      讀被授權的分析產出
write_finding  寫下完整分析（不寫在回覆裡）
update_state   跨任務要記得的結論
localize_blob  取得原始檔案路徑（非表格格式才用）
inspect_blob   看欄位、型別、列數、樣本
duckdb_query   對被授權的 blob 下 SQL，大結果用 into 寫回成新 blob
```

SQL 裡不能有檔案路徑 —— 指名 artifact 是唯一的取用方式。
沙箱如何守住這件事，見 [`myharness/lanes/tabular/README.md`](myharness/lanes/tabular/README.md)。

## 量測：單一 agent 對 MyHarness

`myharness compare --backend self-hosted`，2026-10-05，模型 `aird-35b`（context window 65,536）。問題是 golden 那題：138 KB 的交易 CSV，報告要給出不重複帳戶數（765）和平均金額最低的 channel（app）。

| | 答對 | 單一 context 峰值 | 總輸入 token | 總輸出 token | 秒 |
|---|---|---:|---:|---:|---:|
| 單一 agent（CSV 全文進 prompt） | 否：放不進 context window，prompt 至少 57,345 token | — | — | — | 2 |
| MyHarness | 是 | ≈13,902 | 154,093 | 22,682 | 458 |

- 之後修了三個浪費：lane 的閘門只看 token 不看 max_turns；handle 格式錯時 re-prompt 會整個重做分析；寫完 finding 後的警告措辭讓 lane 重寫同一份 finding。re-prompt 現在把 finding 全文附上、關掉讀寫工具，一個回合補完 handle。同一題重跑（compare-live-5）：答對，峰值 ≈12,386，總輸入 95,863，總輸出 25,124，290 秒。
- 修正前的總數少算了：派工被 re-prompt 時只記到最後一次嘗試的 token。上表的 154,093 也是少算的，所以修正前後差多少無從得知；95,863 是修正計帳後的實數。這個模型在無 schema 強制的路徑上幾乎每個派工第一次都用散文交 handle，四個派工全被 re-prompt。
- 原因是第一份 prompt 只說「回覆一個 handle」，JSON 的樣子只在 re-prompt 才給。改成一開始就給之後（compare-live-6）：五個派工全部一次交出合格 handle，答對，總輸入 105,646（這次 orchestrator 多派了一個 lane，平均每個派工約 21k，上一次約 24k）。單次結果，變異範圍內。
- `--runs 3`（compare-runs-1，同一份程式）：MyHarness 3 次都答對；峰值中位數 ≈8,634（6,890–9,687），總輸入中位數 85,674（66,876–125,635），總輸出 25,957（19,033–26,156），472 秒（225–663）。單一 agent 3 次都沒答到（兩次放不進 context window，一次請求逾時）。總輸入同一題就差到近兩倍，上面任何單次之間 10–30% 的差距都看不出趨勢；要比較改動，至少用 `--runs 3` 看範圍。
- 峰值是 orchestrator（Claude Code 自己算的 context 用量）；各 lane 單次請求的峰值在 3,835–6,950 之間。
- `≈`：LiteLLM 串流時每則訊息的 usage 都是 0，只有整次的總數，所以 lane 的單次請求大小用 worker 自己的估算。總輸入／輸出是 backend 回報的實數，包含 orchestrator。
- 有 SQL 工具的單一 agent（一個 lane worker 拿到整個任務，同樣的模型、工具和 charter，回合與預算放寬；`--only tools --runs 3`）：3 次都答對，總輸入中位數 50,732（20,052–135,441），總輸出 5,070，50 秒（22–141）。**這題它比 MyHarness 便宜也快得多**：一張 138 KB 的表，一個 agent 用 SQL 就夠，多 agent 的協調全是額外成本。MyHarness 要證明的是單一 context 撐不住的題目（多份資料、要 join、要讀文字），這題證明不了。

## 這個 harness 保證什麼

Golden job 每次跑都斷言這些（`tests/golden/`，`pytest -m live tests/golden`）：

- 交付一定存在，且由 lane 產出而非 orchestrator 拼湊
- orchestrator context 峰值有上限
- 沒有重複 dispatch、沒有未授權的產出、報告沒有被覆蓋
- 報告可回溯到原始資料
- **報告含有只能靠實際計算得出的數字**

| Run | 後端 | 時間 | Context 峰值 | 資料流異常 | 數字對不對 |
|---|---|---:|---:|---|---|
| #6 | OpenRouter 120B | ~494s | 6,757 | 無 | 全對 |
| #7 | 自架 35B | 264s | 9,491 | 無 | 全對 |
| #8 | 自架 35B | 176s | 8,543 | 無 | 全對 |

「數字對不對」指的是報告裡的 765 個不重複帳戶與四個 channel 平均金額，
與直接對 CSV 下 SQL 的結果**逐項相符**。那不是模型猜得到的數字。

**一個 35B 的自架模型跑得完整條鏈。** 對照與兩次執行暴露的問題見
[`docs/introduction.md`](docs/introduction.md#它保證什麼)
與 [`spikes/RESULTS.md`](spikes/RESULTS.md)。

## 開發

```bash
pytest                  # 離線，不花錢（862 tests）
pytest -m live          # 打真實 API，要金鑰，會花錢（24 tests）
ruff check .            # lint
mypy                    # myharness/，strict
openspec list           # 進行中的規格變更
```

規格驅動流程見 `openspec/`。已完成的 capability 規格在 `openspec/specs/`。
