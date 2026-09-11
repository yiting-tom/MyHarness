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
claude mcp add myharness -- myharness-mcp --root ./myharness-jobs --backend openrouter
```

然後在對話裡：

```
analysis_start(task="分析這份交易資料，找出異常樣態")
analysis_provide(job_id=..., payload=<CSV>, name="txn.csv")
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
python -m myharness.a2a.server --root ./myharness-jobs --backend openrouter
```

沒有 console script，而且**只綁 loopback**：這條邊界目前沒有認證、沒有多租戶、
沒有速率限制，所以它拒絕聽在任何非 loopback 的位址上。要對外開之前，那三題
得先有答案。

agent card 在 `/.well-known/agent-card.json`，宣告兩個 skill：

| skill | 給什麼 |
|---|---|
| `analysis.result` | 摘要與章節價目表（**預設**，不含報告全文） |
| `analysis.sections` | 指定章節的全文，逐節取 |

價目表的 artifact 帶一個 `required=true` 的 extension URI —— 不認得這個約定的
客戶端會被協定告知它不認得，而不是默默把目錄當成報告讀。

**已知的牆**：job 只活在啟動它的那個 process 裡。`analysis_result` 與
`analysis_drill` 只讀事件流與 store，換 process 照樣答；但**進行中的 task 接不
回來**。A2A 的 `TaskState` 九個值裡沒有一個表示「存在但不在此程序執行中」，
所以被丟下的 job 回報 `FAILED` 加一段說明——種類上是錯的，但它是終局狀態，
呼叫方應該停止等待（spikes/RESULTS.md，spike #23）。

## 不用 MCP 直接跑

```bash
python -m myharness.goldens --backend openrouter   # 端到端的 golden job
myharness --root jobs-scratch/golden jobs          # 列出 job
myharness --root jobs-scratch/golden inspect golden # 資料流與異常
myharness --root jobs-scratch/golden monitor golden # 即時追蹤
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
pytest                  # 離線，不花錢（855 tests）
pytest -m live          # 打真實 API，要金鑰，會花錢
openspec list           # 進行中的規格變更
```

規格驅動流程見 `openspec/`。已完成的 capability 規格在 `openspec/specs/`。
