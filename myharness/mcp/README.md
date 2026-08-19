# mcp — 對外的介面

這是 `DESIGN.md` 第一行說的那個 MCP server：客戶端（Claude Code / Desktop）
用它觸發資料分析，而**原始資料永遠不會進到客戶端的 context**。

## 連接

```bash
claude mcp add myharness -- myharness-mcp --root ./myharness-jobs --backend openrouter
```

`--root` 預設 `./myharness-jobs`。**不叫 `jobs`** —— layout 本身會在 root 底下放一層
`jobs/`，取名 `jobs` 會得到 `jobs/jobs/<job_id>/`。這個命名衝突是 layout privacy
測試抓到的。

## 六個工具

```
analysis_start(task)                      → {job_id, state}      立刻返回
analysis_poll(job_id, wait, since)        → 有界進度 + 待答問題   long-poll
analysis_provide(job_id, payload, name)   → {artifact, routed}   落 blob
analysis_answer(job_id, question_id, text)→ {ok}
analysis_result(job_id)                   → 摘要 + 章節價目表
analysis_drill(job_id, section_id)        → 單節全文
```

## Long-poll 的 `since`

`analysis_poll` 會等到**狀態真的改變**才回，不是睡到時間到。回應裡有一個
`revision`，下次 poll 把它帶回來：

```
poll(job) → {revision: 3, ...}      # 客戶端離開去做別的事
                                     # 此時一條 lane 完成了
poll(job, since=3) → 立刻返回        # 不會等下一次改變
```

沒有 `since` 的話，兩次 poll 之間發生的事會被漏掉 —— 等待會為了「下一次」改變而阻塞，
而剛剛那次永遠不會被提起。這是測試抓到的。

`ctx` 事件**不算改變**。它每個 orchestrator turn 都會發，拿它當訊號等於把 long-poll
變成每 30 秒空轉一次。

## 上限

這一層保護的是 host agent 的 context —— DESIGN §1 遞迴表的第一列。
下面每一層都有上限，這一層沒有理由是漏的：

| | 上限 |
|---|---|
| 進度回應整體 | 2,000 字元（**實際執行**，不是期望） |
| 最近事件 | 8 行 × 120 字元 |
| 待答問題 | 5 則 × 400 字元 |
| 結果摘要 | 1,500 字元；發現 8 條 × 200 字元 |
| 單節鑽取 | 20,000 tokens |

**每項上限相乘會超過整體上限**（8×120 + 5×400 ≈ 3,000 > 2,000），所以整體上限是
真的執行的：超過就丟內容，先丟最近事件、再減問題數，最後才裁切最後一個問題的文字。
問題比日誌重要 —— 沒答的問題會擋住 job，漏掉的日誌行不會。

## 結果比 process 活得久

`analysis_result` 與 `analysis_drill` **只讀事件流與 artifact store**，兩者都在磁碟上。
所以重啟後、甚至換一個 process，它們照樣答得出來。

只有 `analysis_poll` 與 `analysis_answer` 需要活著的 job。對已消失的 job，
它們回「**不在執行中**」而不是「查無此 job」—— 那是兩件不同的事，
客戶端的下一步也不同（一個還能讀結果，一個是打錯了）。

這條性質是 DESIGN #13「事件流為地基」一路撐到這裡的結果，不是免費的。
它被寫成規格需求，免得日後有人為了方便把交付快取進記憶體而讓它悄悄失效。

## 分流

`analysis_provide` 落完 blob 之後會呼叫分流器，判斷它屬於哪一條 lane，
並把判斷結果附在給 orchestrator 的通知裡。回應分成四個欄位：

```
routed: true/false          有沒有判定出歸屬
routed_to: "txn-2024"       判定的 lane（未路由時為 null）
routing_reason: "..."       一句話理由
unrouted_because: null      no_table / no_match / failed
```

**分流只是建議。** 它不派工也不授權 —— orchestrator 仍然要在 dispatch 時
把 artifact id 放進 `inputs`，lane 才讀得到。詳見
[`myharness/proxy/README.md`](../proxy/README.md)。
