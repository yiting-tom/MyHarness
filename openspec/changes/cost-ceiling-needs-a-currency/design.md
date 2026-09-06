## Context

`JobRunner._limit_breached()` 目前長這樣：

```python
if self.state.dispatches >= self.spec.max_dispatches:
    return LimitKind.DISPATCHES
if self.state.spent_usd >= self.spec.max_budget_usd:
    return LimitKind.BUDGET_USD
if self.elapsed_s() >= self.spec.max_wall_clock_s:
    return LimitKind.WALL_CLOCK
```

三道閘並列，但它們量的東西可信度不同。派工次數是我們自己數的，
時間是我們自己計的，**金額是後端報的** —— 而 `total_cost_usd()` 只是把事件流裡
的 `usd` 加總，加總不會讓一個估計值變成實數。

`JobSpec` 的 docstring 說「Every ceiling exists because something in this layer
would otherwise grow without bound」。金額上限確實是這樣來的，但它預設了一件
沒被檢查的事：**這個後端會報成本**。Golden #7 是第一個不成立的例子。

## Goals / Non-Goals

**Goals:**
- 讓金額上限只在它量得到東西的時候生效
- 讓「這次執行沒有金額上限」是**可見的**，而不是靜靜發生
- 保持 job 仍然有界 —— 關掉金額閘不等於無界

**Non-Goals:**
- 不修成本估計本身（對 $0 的端點也沒有意義）
- 不動 lane 的 `token_budget`（Golden #7 的另一個發現，獨立處理）
- 不新增執行時探測。能力是宣告的，不是量出來的（design.md D7）

## Decisions

### D1. 用 capability 宣告，不用「金額是不是 0」推斷

推斷版本會是：`spent_usd` 一直是 0 就當作後端不報成本。否決，兩個理由：

- **0 是合法的成本。** 一個很短的 job 在便宜模型上就是趨近 0，
  分不出「免費」和「還沒花到」
- 那會是**探測**。這個 codebase 對 capability 的立場寫在
  `profile.py` 的開頭：declared, not probed，因為「a single failure does not
  prove absence」。一個「目前為止都是 0」的觀察，比單次失敗還弱

所以：`BackendCapability.COST_REPORTING`，跟現有三項並列。

### D2. `max_budget_usd: float | None`，`None` 表示沒有這道閘

替代方案是用 `float("inf")`。否決 —— `inf` 是「上限高到不會撞到」，
`None` 是「沒有上限」。前者仍會出現在報告的上限清單裡，讀的人會以為
有一個天文數字的預算；後者說的是實話。

`_limit_breached()` 因此變成：

```python
if self.spec.max_budget_usd is not None and \
        self.state.spent_usd >= self.spec.max_budget_usd:
    return LimitKind.BUDGET_USD
```

### D3. 決定的地方是 `OrchestratorLoop`，不是 `JobRunner`

`JobRunner` 拿的是 `JobSpec`，它不知道 backend 存在 —— 而這是對的分層。
知道 backend 的是 `OrchestratorLoop`，所以由它在建構 spec 時決定。

> **實作時修正：沒有「呼叫端明確指定就照做」這個例外。**
>
> 原本寫的是自動關閉只在呼叫端沒有主張時發生。想清楚就知道那是錯的，
> 而錯的理由正是這個 change 的理由：
>
> 在不報成本的後端上，`spent_usd` **不是缺席，是虛構的**。Golden #7 的
> $1.0014 就是憑空長出來的 —— 那個模型是 $0。所以「呼叫端明確要求」
> 的金額上限，會照著同一批捏造的數字觸發，把 bug 原封不動地重演一次。
>
> 一個上限如果比較的是一個不對應現實的數字，它就不是一道閘，是一個亂數。
> 呼叫端的意圖不能讓那個數字變真。
>
> 所以：**後端未宣告成本回報，金額上限就不適用，沒有例外。** 想在這種
> 後端上限制用量的人，該用派工次數或時間 —— 那兩個量的是實數。

### D4. 關掉的事實寫進交付的已知限制

這是這個 change 裡唯一新增的行為，而不只是移除。

一次「有 $1 上限而只花了 $0.3」的執行，和一次「根本沒有上限」的執行，
在報告上目前長得一模一樣。後者是使用者應該知道的 ——
**尤其因為它同時意味著金額數字本身不可信**。

所以新增一種 caveat，內容要說清楚兩件事：沒有金額上限，以及把關的是什麼
（派工次數與時間）。既有的 caveat 機制不用改，只多一個 kind。

### D5. 不動另外兩道閘

`max_dispatches` 與 `max_wall_clock_s` 量的都是我們自己數得出來的東西，
在任何後端上都成立。金額閘關掉之後，job 仍然雙重有界。

Golden #7 實測：金額閘在 +120.5s 觸發，但真正讓 job 收斂的是 orchestrator
自己把任務縮小（d2 → d3 → d4）。那不是任何一道硬上限做的事。

## Risks / Trade-offs

- **少一道閘** → 它在這些後端上從來不是真的閘門。真正把關的兩道都量實數，
  且缺席會寫在報告上
- **有人在不報成本的後端上真的想要金額上限** → 明確傳 `max_budget_usd`
  仍然有效（D3）。自動關閉只在沒有主張時發生
- **`float | None` 是一個型別放寬，可能有呼叫端沒處理 `None`** →
  只有一處讀它（`_limit_breached`）與一處傳它（`loop.py`），
  兩處都在這個 change 裡改；`_limit_value` 也要跟著
- **宣告錯了怎麼辦** → 跟其他 capability 一樣，由 live 測試表面化。
  OpenRouter 宣告報成本，而 spike #7 記過它的數字偏高 —— 偏高仍是「有報」，
  這個 change 不處理準確度

## Open Questions

- `direct` profile（proxy 的直接路徑）目前把 `usd` 一律記 0，
  因為端點不回報價格。那是「不報成本」的一種，宣告上一致；
  但它不經過 `JobRunner`，所以這個 change 碰不到它 —— 需不需要一併說明？
