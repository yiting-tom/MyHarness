## Why

`myharness monitor <job>` 已經在做對的事：一次 job 跑幾十分鐘，而「在思考」
「在等限流」「已經卡死」從外面看起來一模一樣，即時模式把它們分開。
實測第五次跑有 **29% 的時間在等限流**，而當下沒有任何東西這麼說。

但它只活在終端機裡：一個框、78 欄、整片重畫。你不能把它放在第二個螢幕上看，
不能捲回去看十分鐘前發生什麼，也不能點開一段已經跑完的派工看它到底做了什麼。

**而我在 `add-flow-viewer` 的 design.md 裡寫過一句錯的話：**

> 不做即時模式。瀏覽器裡的即時模式需要一個 server，那就違反了「零新增相依」。

stdlib 裡就有一個 server。`http.server` 不是相依，它跟 `json` 一樣是 Python 的一部分。
那條理由是假的，這個 change 把它收回。

## What Changes

- **新增 `myharness monitor <job> --web`**：起一個 stdlib HTTP server，
  只綁 loopback，印出網址。
  - `GET /` 給頁面
  - `GET /state` 每次重讀事件流，回傳當下的狀態
  - `GET /trace/<id>` 單一派工的逐輪紀錄，**點開才抓**
- **輪詢，不用 SSE 也不用 inotify。** 理由沿用 `live.py` 自己的：
  事件流是幾 KB 的 append-only JSONL，重讀的成本人感覺不到，
  而 SSE 在 stdlib 的 `http.server` 上要每個 client 佔一條執行緒並讓關閉變難。
- **逐輪紀錄按需抓，因此不再有「全部內嵌」的大小問題** ——
  那是 spike #29 記下的待解項（50 派工約 1.4 MB），有了 server 就自然消失了。
- **終端機的即時模式不動。** ssh 進去的時候它仍然是唯一能用的東西。

## Capabilities

### Modified Capabilities
- `monitor`: 即時模式新增一個瀏覽器的表面。既有需求（含「Monitor 不影響被觀察的
  job」）全部繼續適用，而且對 server 來說更嚴格 —— 它現在是網路可達的。

## Impact

- **Specs**: `monitor` 新增 3 條需求
- **Code**: 新增 `myharness/monitor/serve.py` + `live.html`；`monitor` 子命令加 `--web`
- **Deps**: **零新增。** `http.server` / `socketserver` / `threading` 都在 stdlib。
  `a2a` 那條 extra 帶了 starlette + uvicorn，但要求看 monitor 的人裝那一套是錯的
- **安全**：這是這個 change 唯一真正新增的風險面。MCP 走 stdio、終端機 monitor
  不聽任何 port，而這個會。所以：
  - 只綁 loopback，沿用 `a2a/server.py` 的 `require_loopback`（主機名不算證據，
    位址才算）
  - 只有 GET，沒有任何寫入路徑
  - 路徑上的 job id 與 dispatch id 一律當成不可信輸入
- **已知的落差（必須寫明，不是先實作）**：**transcript 是在派工結束時才寫下的。**
  所以執行中的派工**沒有**逐輪紀錄可看 —— 不是還沒載入，是還不存在。
  頁面必須這樣說。要真的做到逐輪即時，得讓 worker 邊跑邊落檔，那是 `lane-worker`
  的事，不是在這裡假裝
