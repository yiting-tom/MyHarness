# myharness.lanes

一個 lane worker 就是一次任務：全新的 agent context、一份延續的 lane state、
一個受限的 handle。做完就死。

規格見 `openspec/changes/add-lane-worker/`，實測依據見 `spikes/RESULTS.md`。

## 為什麼 handle 一定守得住

兩道機制疊起來 —— 少任何一道都只是「很可能」：

| 機制 | 管什麼 | 在哪 |
|---|---|---|
| `HANDLE_SCHEMA` | **形狀** | 後端支援時由 API 強制；否則應用層驗證 + 重新提示 |
| `clamp_handle()` | **長度** | 永遠執行。逐欄位上限 + 整體序列化上限 |

JSON Schema 約束不了長度 —— 模型完全可以回一個符合 schema 但 headline 三千字的物件，
或把報告塞進 metrics 的鍵名。所以 `clamp_handle()` 對**整個序列化結果**也設上限，
超出時先丟 followups、再丟 metrics、最後才截 headline，並設 `truncated: true`。

## 定義一個 LaneType

```python
LaneType(
    name="tabular-analyst",
    charter_path=Path("charters/tabular-analyst.md"),   # 檔案，不是字串
    tools=("read_note", "write_finding", "update_state", "localize_blob"),
    model_tier="strong",          # 能力層級，不是供應商的模型名稱
    backend="openrouter",
    token_budget=80_000,
    max_turns=25,
    state_max_tokens=8_000,
)
```

Type 是靜態的（**工具是程式碼，生不出來**），instance 是動態的：
`txn-2024` 和 `txn-2023` 是同一個 type 的兩個 instance，state 完全隔離、可平行跑。

## 寫 charter

Charter 放檔案，因為它是 prompt cache 的穩定前綴，也是最需要反覆調的東西 ——
放檔案才能 diff、能 review、能不動程式碼就改。每次執行會把 charter 的雜湊寫進事件流，
所以「這次跑的是哪一版 charter」事後查得到。

Charter 必須講清楚三件事：

1. **完整分析寫進 `write_finding`，不要寫在回覆裡。** 這是省 context 的關鍵。
2. **`update_state` 只寫跨任務要記得的東西** —— 結論與開放問題，不是細節。
   State 有 token 上限，超過會被**拒絕**（不是截斷），舊的 state 會保留，
   而該次執行會被標記為降級。
3. **大型資料用 `localize_blob` 取路徑後以工具處理**，不要讀進 context。

## Worker 能碰到什麼

只有四個 in-process 工具，而且每一個都在 harness 這側檢查授權。
Worker **拿不到** `Read` / `Bash` / `Glob` —— 授權模型只有在沒有旁路時才成立。

可讀範圍 = 自己的 namespace + `dispatch` 的 `inputs` 明確授權的 id。
被拒絕時 worker 會收到一段結構化的錯誤文字（不是例外），它可以據此改變做法。

## 失敗是值

`run_lane_worker` **不會因為語意失敗而拋例外**。所有失敗都是帶 status 的 handle：

| status | 意思 |
|---|---|
| `budget_exceeded` | token 預算耗盡（附部分結果引用） |
| `max_turns` | 回合數用盡 |
| `tool_failure` | 工具反覆失敗 |
| `state_rejected` | 分析成功但 state 沒寫進去 —— 下一次任務看不到這次的結論 |
| `schema_violation` | 重試後仍產不出合法 handle |
| `backend_unavailable` | 後端持續 429/5xx |

分類**不解析例外訊息字串** —— SDK 的預算錯誤字面是
`"Claude Code returned an error result: success"`，語意含糊到無法依賴。
改用「有沒有收到 `ResultMessage`」＋「本地 token 累計」＋「觀察到的 `api_retry` 狀態碼」。

Transient（429/5xx）由 framework 靜默重試；**語意失敗絕不自動重試** ——
那只會用兩倍成本得到同樣的失敗。

## 新增一個 backend

```python
BackendProfile(
    name="my-proxy",
    base_url="https://proxy.internal/",
    auth_token_env="MY_PROXY_KEY",        # 環境變數名稱，不是金鑰值
    models={"strong": "…", "mid": "…", "cheap": "…"},
    capabilities=frozenset(),             # 預設什麼都不宣告
)
registry.register(profile)
```

**Capabilities 是宣告，不是偵測** —— 偵測要試打，會產生費用與延遲，
且一次失敗不代表不支援。宣告錯了會在 live 測試中暴露，而事件流記錄了
每次執行實際走的是 `enforced` 還是 `degraded`，事後可稽核。

未宣告的 capability 會自動走降級路徑：
沒有結構化輸出 → 應用層驗證 + 重新提示；
沒有 API 端預算 → 本地 token 計數硬斷。

## 跑 live 測試

離線測試涵蓋所有邏輯分支，不需網路與金鑰。Live 測試驗證的是
「機制在真實模型上確實生效」這件事本身：

```bash
pytest -m live tests/lanes/test_live_lane_worker.py
```

需要 `.env` 裡有對應 backend 的金鑰。斷言只針對機制（handle 合 schema、
大小有上界、失敗是值），不針對內容品質 —— 模型輸出有隨機性，界限沒有。
