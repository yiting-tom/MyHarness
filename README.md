# MyHarness

以 `claude-agent-sdk` 建構的多 agent 資料分析 harness，以 **MCP server** 形式對外提供。

**要解決的問題**：單一 agent 的 196k context 在資料分析任務中極易耗盡。

**核心原則**：*外部化狀態 + 短命執行者*，遞迴套用在四個層級。
每一層都保證**原始資料不會進到上一層的 context** —— 由構造保證，不是由 prompt 祈禱。

| 層級 | 被保護者 | 手法 |
|---|---|---|
| MCP 邊界 | 客戶端 agent 的 context | job-based API，只回摘要 + 章節價目表 |
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

需要一個後端金鑰。OpenRouter 的話，在 `.env` 放 `OPENROUTER_KEY=sk-or-...`。

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

最近一次（第六次）：15/15 全過，context 峰值 6,757、成本 $0.2543，
報告裡的每個數字都與直接查 CSV 的結果相符 —— 對照見
[`docs/introduction.md`](docs/introduction.md#它保證什麼)。

## 開發

```bash
pytest                  # 離線，不花錢
pytest -m live          # 打真實 API，要金鑰，會花錢
openspec list           # 進行中的規格變更
```

規格驅動流程見 `openspec/`。已完成的 capability 規格在 `openspec/specs/`。
