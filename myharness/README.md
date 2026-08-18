# myharness

MyHarness 的地基層：`artifacts` 與 `events`。這兩個套件不含任何 LLM 呼叫，
是整個 harness 中唯一可以被確定性驗證的部分。

設計依據見專案根目錄的 `DESIGN.md`，可行性驗證見 `spikes/RESULTS.md`，
本層的規格與決策見 `openspec/changes/add-artifact-store-and-event-log/`。

## 兩層的用途

### `myharness.artifacts` — 資料放哪、誰能讀、讀多少

支撐 harness 的核心不變式：**原始資料不可能進入 orchestrator 的 context**。
它靠三道由程式碼保證（而非 prompt 約束）的機制達成：

- **blob / note 二分**：blob（CSV、log、PDF）呼叫 `read_note` 會直接被拒絕，
  並回傳 schema 與建議的工具型存取方式。沒有任何路徑能把 blob 變成 context。
- **讀取前的 token 預檢**：`est_tokens` 存在 index，超額在**開檔之前**就被拒絕，
  並附上可分段讀取的 section 清單。讀了才發現太大，token 已經花掉了。
- **capability 授權**：worker 只能讀自己的 namespace 加上 `dispatch(inputs=[...])`
  明確授權的 id。資料流完全可從呼叫參數推導 —— 這是除錯多 agent 系統的唯一起點。

```python
store = LocalArtifactStore(root)
await store.init_job("j7")

blob = await store.put_blob("j7", "raw/txns-2024", source=path,
                            produced_by="user", schema={"columns": [...]})
note = await store.put_note("j7", "lanes/txn-2024/findings/003", text,
                            produced_by="lane:txn-2024")

grants = GrantSet.for_lane("j7", lane_namespace("txn-2024"), granted=[blob.id])
text = await store.read_note(note.id, grants=grants, max_tokens=3000)

async with store.localize(blob.id, grants=grants) as path:
    duckdb.sql(f"SELECT * FROM '{path}'")
```

Lane state 用 `compare_and_set_note(..., expected_revision=n)`。上層紀律是
「同一 lane 序列化執行」，CAS 的作用是讓紀律失效時失敗得響亮，而不是默默覆寫。

### `myharness.events` — 實際發生了什麼

每個 job 一份 append-only 的 `events.jsonl`。成本報表、儀表板、
OpenTelemetry span、回歸斷言全部是它的投影，不是第二份紀錄。

```python
log = LocalEventLog(root)
await log.append("j7", DISPATCH_END, id="d3", lane="txn-2024",
                 status=STATUS_OK, tokens={"in": 41200, "out": 3800}, usd=0.31)

summary = summarize(await log.read("j7"))
assert summary.context_peak < 120_000     # context 紀律真的有效嗎
assert summary.duplicates == 0
assert summary.total_usd < 3.0
```

`derive_caveats()` 從事件流推導報告裡的「未做到什麼」（超預算的 lane、
未回答的提問、沒被任何 lane 使用的 payload）。**LLM 最會忘記說的就是自己
沒做到什麼，所以這件事由程式碼保證。**

## 如何新增一個後端

1. 實作 `ArtifactStore`（或 `EventLog`）的抽象方法。
2. 在 `tests/contract/conftest.py` 的 `harness` fixture 加入你的 backend，
   並提供 `destroy_content()` —— 合約測試用它來驗證「被拒絕的讀取沒有碰內容」。
3. 跑 `pytest tests/contract`。**合約測試不得修改**；需要改它才過，
   表示行為不一致而不是測試需要放寬。

`localize()` 必須是 async context manager 而非回傳路徑的函式：
回傳路徑無法表達「用完了」，而物件儲存後端一定需要這個訊號。

## 如何新增一個事件型別

在 `events/types.py` 加常數並放進 `KNOWN_TYPES`，然後直接
`log.append(job, MY_TYPE, **payload)`。讀取端對未知型別寬容，
新增型別永遠是純加法。

若這個型別代表某種**降級**，同時要在 `query.derive_caveats()` 加對應分支 ——
否則它會從最終報告的 caveats 裡消失。

## 一條會失敗的測試

`tests/unit/test_layout_is_private.py` 靜態檢查：除了 `myharness/local_layout.py`，
沒有任何模組可以拼出 `jobs` / `blobs` / `notes` / `traces` / `index.sqlite` /
`events.jsonl` 這些字面值。後端抽象只有在這條守得住時才成立 ——
一旦某個 lane 工具自己組路徑，換 MinIO 就從「新增一個類別」變成「重寫」。
