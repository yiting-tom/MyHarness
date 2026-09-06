## Why

Golden run #7 跑在一個 **$0 的自架模型**上，卻在 120.5 秒觸頂收工：

```
+120.5s  limit.reached  limit=max_budget_usd  value=1.0014  dispatches=3
```

那 $1.0014 是 SDK 的估計。spike #12 早就記過「token 數是實數，金額是 SDK 的估計」——
當時那只是一個難看的數字。**這次它做了一個真的控制決定。**

`JobRunner._limit_breached()` 無條件比較 `spent_usd >= max_budget_usd`，
而 `spent_usd` 來自 `total_cost_usd()`，也就是把事件流裡的 `usd` 加總。
沒有任何一層問過：**這個後端到底報不報成本？**

Job 還是善終了（寬限派工都成功、報告完整），但那是善終保證在補救一個
本來就不該觸發的閘門。往後每一次自架執行都會撞到同一件事。

## What Changes

- `BackendCapability` 新增 **`COST_REPORTING`** —— 後端宣告它回報的金額是否可信。
  `anthropic` 與 `openrouter` 宣告支援；`self-hosted` 與 `direct` 不宣告
  （它們的 capabilities 本來就刻意留空）。
- `JobSpec.max_budget_usd` 允許為 **`None`**，表示這次執行沒有金額上限。
  `OrchestratorLoop` 在後端未宣告 `COST_REPORTING` 時把它設為 `None`。
- **金額上限不適用時，這件事 SHALL 出現在交付的已知限制裡。** 一次沒有花費
  上限的執行，跟一次有上限而沒觸頂的執行，對讀報告的人意義完全不同。
- 派工次數與時間上限**不受影響**。金額上限關掉之後，把關的是它們，
  而它們量的都是實數。

## 不在這個 change 裡

- **不改成本估計本身。** 讓 SDK 報出正確金額是另一個問題（而且對免費端點無意義）。
  這裡處理的是「用一個不可信的數字當閘門」。
- **不動 lane 的 `token_budget`。** Golden #7 的另一個發現（60k 預算對上 64k
  視窗太緊）是獨立的一件事。

## Capabilities

### New Capabilities
（無。）

### Modified Capabilities
- `model-backend`: capability 宣告的清單增加成本回報這一項，並定義未宣告時的降級路徑
- `job-runner`: 硬上限不再一律包含金額；一個沒有金額上限的執行必須可被看見

## Impact

- **Code**: `myharness/backends/profile.py`（新 capability 與各 profile 的宣告）、
  `myharness/jobs/spec.py`（`max_budget_usd: float | None`）、
  `myharness/jobs/runner.py`（`_limit_breached` 略過 `None`）、
  `myharness/orchestrator/loop.py`（依後端決定）、
  `myharness/orchestrator/delivery.py`（新的 caveat 種類）
- **風險**：關掉一道閘就是少一道閘。緩解是它從來不是這個後端上的真閘門 ——
  真正在把關的 `max_dispatches` 與 `max_wall_clock_s` 兩者都量實數，
  而且「沒有金額上限」會寫在報告上而不是靜靜地發生
- **不相容**：`JobSpec.max_budget_usd` 的型別放寬為 `float | None`。
  預設值不變，既有呼叫端不受影響
