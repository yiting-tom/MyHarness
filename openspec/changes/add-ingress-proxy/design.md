# Design — Ingress proxy

## D1 Proxy 只分類，不派工

「這份資料屬於哪條 lane」和「該對它做什麼」是兩個問題。
第二個問題的答案來自**目標**，而目標在 orchestrator 手上 ——
proxy 看不到目標（見 D3），所以它根本沒有資訊可以下任務。

讓 proxy 自己 dispatch 還會有兩個後果：它會花掉 dispatch 預算與金錢，
而那兩樣是 job 級的守門機制（`JobRunner` 的三道防迴圈）在管的；
以及一份分類錯誤的資料會直接變成一次真的執行，而不是一則可以被否決的建議。

所以 proxy 的產出是**一則判斷**：`{lane, confidence, reason}`，
寫成 `proxy.route` 事件，並附在給 orchestrator 的通知裡。
orchestrator 照樣決定要不要用、下什麼任務。

DESIGN #4 寫的「先斬後奏」在這裡的意思是**分類先做、事後告知**，
不是「先執行、事後告知」。

## D2 Proxy 不授權

分類不等於授權。`GrantSet` 的來源仍然只有兩個：lane 自己的 namespace，
和 `dispatch(inputs=[...])` 明確列出的 id（DESIGN #10、design.md D2）。

如果 proxy 能授權，就出現第三種來源，而且是一個**由模型判斷產生**的來源。
資料流監視器現在能斷言「沒有未授權的產出」，正是因為授權的來源少到數得完。

實務上的差別：proxy 說「這份屬於 txn-2024」，orchestrator 仍然要在 dispatch 時
把 id 放進 `inputs`，lane 才讀得到。少了那一步，lane 拿到的是 `not_granted`。

## D3 零 context 共享是可測的，所以要測

DESIGN §4.2 說「orchestrator 用宣告式資料遙控 proxy，兩者零 context 共享」。
這句話如果只是意圖，過幾個月就會有人為了「讓 proxy 判斷得更準」
把 plan 或 goal 塞進它的 prompt —— 那時 proxy 就不再是無狀態的分類器，
而是第二個規劃者，只是沒有預算控制。

所以 proxy 的 prompt **只由兩樣東西組成**：routing table，和一份有界的樣本。
這寫成需求，並用一個測試斷言 prompt 裡不含 plan 文字與 goal 文字。

## D4 樣本同樣走兩道閘

Proxy 是一個 LLM，餵給它的東西就是 context。所以樣本受兩道約束：
行數與字元數 —— 和查詢結果、handle 完全同一個形狀。

分類需要的資訊其實很少：檔名、大小、宣告的格式與欄位、外加開頭幾行。
CSV 的話 header 就幾乎決定了答案。上限訂得小，是因為分類不需要更多，
而不是因為省錢剛好。

樣本從 blob 讀取這件事本身要走 store，不能自己開檔 —— 否則就是第二條讀取路徑。

## D5 Ingress 不能依賴模型呼叫成功

資料一定要落地。Proxy 逾時、模型掛掉、回傳的 lane 不存在、
routing table 是空的 —— 每一種都降級成「未路由」，而 blob 已經存好了。

這條的反面是「路由失敗就拒絕 ingress」，那會讓一個雲端 API 的抖動
變成使用者的資料上傳失敗。

降級要明說：回應與事件都要能分辨「沒有 proxy」「proxy 失敗」「proxy 說不知道」，
因為這三者對 orchestrator 的意義不同。

## D6 同步呼叫，短逾時

`analysis_provide` 是一次同步的 MCP 呼叫。兩個選擇：

**（採用）行內執行，短逾時。** 客戶端多等幾秒，換到的是回應本身就帶著
路由結果，而給 orchestrator 的通知一次就完整。契約單純：
`routed` 是布林，不是三態。

**（否決）背景執行，poll 時才知道。** 保住了 ingress 的低延遲，
但 `routed` 變成 pending/routed/unrouted 三態，客戶端要多一輪才知道結果，
而 orchestrator 會先收到一則「未路由」再收到一則「其實是 txn-2024」。
為了幾秒鐘讓兩邊都變複雜，不划算。

逾時訂得比 lane 短很多：分類是一次短呼叫，跑久了代表出事，不是代表快好了。

## D7 用最便宜的模型

Proxy 每份資料呼叫一次，而且只做分類。用 `ModelTier.CHEAP`。
DESIGN #4 原本寫 Haiku，這裡改成用 tier 而不是硬編模型名稱，
理由和 lane 一樣：同一個 proxy 要能在不同 backend 上跑。

成本記在 `(proxy)` bucket，不算在被路由到的那條 lane 頭上 ——
`events/query.py` 的 `_bucket` 已經這樣做了。
