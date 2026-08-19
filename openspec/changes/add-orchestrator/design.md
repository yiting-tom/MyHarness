## Context

三層地基已完成並經過實測：`artifact-store`（blob 不可能被讀進 context、授權由
dispatch 的 inputs 決定）、`lane-worker`（handle 的形狀由 schema、長度由程式碼保證；
語意失敗一律為值）、`model-backend`（per-backend 共享節流閘）。

本 change 補上指揮層。它同時是 context 紀律的最後一道考驗：前三層保證了
**沒有任何路徑**能把原始資料送進 orchestrator，但它自己的規劃推理、handle 累積
與細節窺看仍會成長。

`DESIGN.md` §5 的預算表估算 orchestrator 在一個 <50 dispatch 的中型 job 中約用
77–107k / 196k。其中變異最大的兩項是 `peek`（估 ≤30k）與 thinking（估 30–60k）。
`peek` 由本 change 變成硬預算；thinking 只能靠交接重啟兜底。

已知的實測事實（`spikes/RESULTS.md`）：

- 同一輪的多個 custom MCP tool call **只有 `readOnlyHint=True` 才併發**，
  否則循序執行；併發上限預設 10。
- SDK in-process tool 靜默阻塞 600 秒不會被中止。
- 巢狀 `query()` 不洩漏子行程或 fd。
- `disallowed_tools` 才會把工具從請求裡拿掉，省 ≈16.5k tokens/worker。

## Goals / Non-Goals

**Goals:**
- 讓 orchestrator 的 context 成長成為程式碼保證的常數，可用一個「反覆窺看」的測試證明。
- 讓 job 在任何硬上限觸頂時仍有交付，而不是拿到空結果。
- 讓「這個 job 為什麼這樣收工」完全可從事件流重建，不需保留對話。
- 讓 lane 的並行由我們自己的排程決定，不受後端工具併發語意影響。
- 讓對外服務層之後只需薄薄一層包裝。

**Non-Goals:**
- 不實作 MCP server 的對外工具（下一個 change）。
- 不實作 proxy 與 routing table。
- 不做 orchestrator 的多 job 併行（一個 runner 服務一個 job）。
- 不做計畫的自動壓縮（與 lane state 同理：先撞牆、留下事件、再設計）。

## Decisions

### D1. `dispatch` 非阻塞 + `await_tasks` 阻塞，而非阻塞式 dispatch

Spike #1 顯示同一輪的多個工具呼叫**只有標記 `readOnlyHint=True` 才會併發**。
`dispatch` 會寫 artifact 與 lane state，把它謊報成唯讀可以換到併發，
但那是欺騙一個我們不控制的執行期，且 CLI 可能對唯讀工具另有假設。

**選擇**：`dispatch` 起一個 asyncio 背景任務後立即返回（~5ms），
`await_tasks` 以單一阻塞呼叫收割。三次 dispatch 循序執行也只花約 15ms，
真正的併發發生在我們自己的 event loop 裡，併發度由我們的 semaphore 決定。

**替代方案**：`check_tasks` 輪詢。**否決** —— 每次空輪詢都是一整個 orchestrator turn，
完整 context 重送、完整推理、真金白銀。`await_tasks` 真的阻塞到有結果（實測可阻塞 600s），
代價只是每批 fan-out 多一個小 turn。

### D2. `peek` 預算是硬的，且用完不影響其他工具

**選擇**：job 級總預算，扣減以實際回傳的 token 數計。用完後 `peek` 回傳
「預算耗盡，請改派 lane 讀」，但派工、收割、收工照常。

**理由**：`peek` 是 orchestrator context 中變異最大的一項。把它變成常數，
整層的上界才從估計變成保證。用完後仍能收工，是因為讓 job 死在窺看預算上
毫無意義 —— 它應該退化成「派 lane 去讀」，那正是本來就該做的事。

`peek` 是真唯讀，因此標記 `readOnlyHint=True`，可在同一輪併發窺看多個 artifact。

### D3. 三道防迴圈依成本排序，最便宜的擋最常見的

| 防線 | 成本 | 擋什麼 |
|---|---|---|
| 重複 dispatch 偵測（hash 比對） | 零 | 最常見：失敗後反覆派同一項 |
| Job 硬上限（次數／金額／時間） | 零 | 慢速失控 |
| 無進展偵測（連續 N 次無新產出） | 零 | 換湯不換藥的迴圈 |

三者都是純程式碼判斷，不需額外 LLM 呼叫。**重複偵測回傳前次 handle 而非錯誤** ——
orchestrator 要的本來就是那個結果，只是它忘了自己已經有了。

### D4. 觸頂是「請收工」而非「砍掉」，但寬限有限

**選擇**：觸頂時注入一則系統訊息要求立即收工，並給一個寬限額度（額外的派工次數）
讓它產出報告。寬限用完才中止，並由程式碼自行產出降級交付。

**理由**：硬砍讓使用者拿到空 job。但無上限的「請收工」等於沒有上限 ——
一個不聽話的 orchestrator 會一直派工。所以寬限必須有界，且用完之後
**由程式碼而非 LLM** 產生交付。

### D5. 計畫是 note artifact，不是特殊機制

**選擇**：計畫存成一份普通的 note，orchestrator 用既有的 artifact 工具讀寫。

**理由**：這樣它自動獲得 `est_tokens` 預檢、版本、事件記錄與可續跑性，
不需要第二套狀態機制。交接重啟因此只是「讀計畫、開新 client」。

### D6. 提問通道是介面，預設實作是「不問」

**選擇**：`UserChannel` 抽象；本 change 提供一個永遠套用預設值的實作，
與一個測試用的腳本化實作。MCP 層之後提供真正的佇列實作。

**理由**：orchestrator 不該知道使用者在哪裡。而預設「不問」讓整層在沒有
對外介面的情況下也能端到端跑完 —— 這是本 change 能被獨立驗證的前提。

### D7. Golden job 是本層唯一可信的驗收

Orchestrator 的規劃品質無法離線斷言。**但 context 紀律可以。**

**選擇**：一個小型 golden job（2–3 條 lane、固定輸入），斷言只針對可量化的性質：
context 峰值上界、無重複 dispatch、成本上界、必有交付、caveats 完整。
不斷言結論的內容。

## Risks / Trade-offs

- **Orchestrator 忘記呼叫 `await_tasks`** → `dispatch` 的回傳值明說要收割；
  且 job 在收工時若仍有未收割的任務，SHALL 先等待它們並計入交付。
- **Thinking 累積撐爆 context**：`peek` 已成常數，但 thinking 不可控 →
  交接重啟是唯一兜底，因此它必須被測到，而不只是寫在文件裡。
- **交接重啟遺失細節**：計畫的壓縮是 LLM 做的，會漏 → 重啟事件記錄當時的
  context 用量與計畫版本，使遺失可被事後發現；正常規模的 job 不應觸發。
- **背景任務在 job 結束後仍在跑** → runner 在收工時取消未收割的任務並記錄，
  避免洩漏行程與繼續計費。
- **重複偵測誤判**：語意相同但字面不同的派工擋不住 → 接受。以字面 hash 為準，
  寧可漏擋也不要錯擋一次合法的重派。

## Migration Plan

無既有資料需要遷移。本 change 只使用既有三層的公開介面，不修改其行為；
`event-log` 的變更是純新增事件型別，舊事件流仍可解析。

## Open Questions

- Peek 的 job 級預算預設值？暫定 30k，待 golden job 的實際使用量校準。
- 交接重啟的門檻比例？暫定 60%，需要一個真的會撞到的長 job 才能驗證。
- 觸頂後的寬限額度？暫定 3 次派工。
- 無進展的判定次數？暫定連續 3 次。
- Job 硬上限的預設值（次數／金額／時間）？暫定 60 次／$5／30 分鐘。
