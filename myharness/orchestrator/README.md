# myharness.orchestrator

指揮層。Orchestrator 規劃與調度，**不彙整** —— 報告由 synthesis lane 寫。

規格見 `openspec/specs/{orchestrator,job-runner}/`，實測見 `spikes/RESULTS.md` §Spike #9。

## 六個工具，就這些

```
plan_update(plan, lanes)              寫計畫、宣告 lane
dispatch(lane, task, inputs)          ~0.1ms 返回，背景執行
await_tasks(task_ids, mode, timeout)  單次阻塞收割（all / any）
peek(artifact, section, max_tokens)   窺看細節，job 級硬預算
ask_user(question, default, kind)     提問，有配額與逾時
finish(report_artifact, summary)      收工
```

**這個集合不會長大。** 有一條測試驗證宣告 20 條 lane 之後它完全不變 ——
「原始資料不可能進 orchestrator 的 context」必須是**工具面的性質**，不是 prompt 的約束。
`peek` 是唯一的讀取工具，遇到 blob 直接拒絕。

## inputs 就是授權

```python
dispatch(lane="syn1", task="彙整成報告",
         inputs=["job/note/lanes/ta1/findings/001"])   # ← 純字串 id
```

Lane 能讀的 = 自己的 namespace + 這裡列出的 id。**在任務文字裡提到 artifact 不會授予任何權限。**

Golden job 第四次跑時 orchestrator 傳了 `[{"blob_path": "..."}]`，被 `str()` 毀損成
一個永遠匹配不到的 grant，然後失敗在兩條 lane 之後以難以理解的 `not_granted` 浮現。
現在無法解析的 inputs 在 dispatch 當場就被拒絕並列出有問題的項目。

## 為什麼 dispatch 不阻塞

Spike #1：同一輪的多個工具呼叫**只有 `readOnlyHint=True` 才併發**。`dispatch` 會寫
artifact，謊報唯讀等於欺騙一個我們不控制的執行期。所以 `dispatch` 起背景任務後立刻返回，
真正的併發發生在我們自己的 event loop 裡。

不用輪詢，是因為**每次空輪詢都是一整個 orchestrator turn 的代價** ——
完整 context 重送、完整推理、真金白銀。`await_tasks` 真的阻塞到有結果（實測可阻塞 600s）。

## 三道防迴圈，全部零成本

| 防線 | 擋什麼 |
|---|---|
| 重複 dispatch（hash 比對） | 失敗後反覆派同一項 —— 回傳**前次 handle** 而非錯誤 |
| Job 硬上限（次數／金額／時間） | 慢速失控 |
| 無進展 + 徒勞上限 | 連續無新產出；超過門檻兩倍**強制**進入 wrap-up |

最後一條是 golden job 第一次跑逼出來的：14 次 dispatch 全部以同一個 401 失敗，
無進展警告有發出，orchestrator 照樣繼續派。**勸告不是防線。**

## Context 紀律

一個 job 一段對話 —— 推理連貫性是 orchestrator 最有價值的東西。代價是 context 只增不減，
三件事框住它：

1. **`peek` 有 job 級硬預算**（預設 30k）。它是這層變異最大的一項；變成常數，
   整層的上界才從估計變成保證。用完後退化成「派 lane 去讀」，其他工具照常運作。
2. **計畫活在對話之外**（一份普通的 note artifact，因此繼承預檢、版本與事件記錄）。
3. **60% 觸發交接重啟** —— 接手者拿到計畫與 job 狀態，**永遠拿不到 transcript**。
   沒寫進計畫的東西就是沒了，這正是交接請求存在的理由。交接次數也有上限。

實測（golden job，5 次 dispatch、14 turns）：context 峰值 **9,720 / 196,000**，peek 用了 **0**。
Orchestrator 信任 handle 的 headline 就足以決策 —— 那正是 handle 契約想要的效果。

## Job 永遠不會空手而歸

Orchestrator 沒呼叫 `finish`（寬限用盡、回合耗盡、交接觸頂、後端持續不可用），
harness 就用事件流已知的資訊自己寫交付，並在 `salvaged` 欄位誠實標示。

「為什麼停下來」與「是不是 harness 代寫的」是**兩個欄位** ——
它們是兩件不同的事實，混在一起會讓真正的原因消失。

## 交付

```
executive_summary   ≤1500 字元，取報告的第一個 ## 章節
key_findings        ≤5 條
sections            章節 + est_tokens 價目表 ← 呼叫者據此決定要不要 drill
caveats             從事件流推導，不從報告讀
cost                usd / dispatches / throttle_s
```

整份交付實測 1,442 字元。呼叫者常常是一個做到一半的 agent，
**在 server 內省下的一切，不能在最後一步全部還回去。**

`finish` 會拒絕沒有 `##` 分節的 note —— 沒有章節就產不出價目表，
那就不是報告，不管它叫什麼名字。

## 已知缺口

**Proxy 尚未實作。** `analysis_provide` 會落 blob 並通知 orchestrator，
但不會用 LLM 判斷資料該給哪條 lane（DESIGN #4），回應以 `routed: false` 明講。

**`artifact.read` 事件尚未發出**，所以資料流圖的讀取邊是空的
（`read_edges_available=False`，不以授權冒充）。
