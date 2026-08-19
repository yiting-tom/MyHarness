## 1. Routing table

- [x] 1.1 `RoutingEntry`（lane / accepts / status）與 `RoutingTable` 型別，
      放在 `myharness/orchestrator/plan.py` 旁邊
- [x] 1.2 存取：寫成 job 的一個 note artifact，與 plan 同層級（不是新的儲存路徑）
- [x] 1.3 `plan_update` 新增選填的 `routing_table` 參數
      （規格：Orchestrator 以宣告式資料遙控分類器）
- [x] 1.4 未附 routing table 時不動既有的（規格：未附 routing table 不影響計畫更新）
- [x] 1.5 再次宣告會取代（規格：routing table 可被取代）
- [x] 1.6 明確的 JSON Schema，不用簡寫 —— `routing_table` 必須是選填
      （前兩個 change 的教訓）
- [x] 1.7 壞掉的 entry 要拒絕並說明，不要靜默丟掉

## 2. 樣本

- [x] 2.1 `myharness/proxy/sample.py`：行數 + 字元數兩道閘（design D4）
- [x] 2.2 樣本經由 `store.localize` 取得，不自己開檔（規格：樣本經由既有的儲存介面取得）
- [x] 2.3 二進位內容不要當文字倒出來
- [x] 2.4 兩道閘各自生效的測試：很多短行、單一超長行
- [x] 2.5 CJK 寬度與 token 估計沿用既有函式，不重寫

## 3. 分類器

- [x] 3.1 `myharness/proxy/classify.py`：單次、無狀態、`ModelTier.CHEAP`（design D7）
- [x] 3.2 prompt 只由 routing table + metadata + 樣本組成
      （規格：分類器只看得到 routing table 與有界樣本）
- [x] 3.3 **測試斷言 prompt 不含 plan 文字與 goal 文字**（design D3）
- [x] 3.4 輸出 `{lane, confidence, reason}`，以寬容的 JSON 解析承接
      （fence、夾在散文裡都收）—— 分類是單次呼叫，不值得為它加 schema 強制
- [x] 3.5 回傳的 lane 不在 routing table 或未開放 → 視同未路由
      （規格：分類回傳不存在的 lane）
- [x] 3.6 短逾時（design D6）；逾時 → 未路由（規格：分類逾時）
- [x] 3.7 三種未路由原因可分辨：no_table / failed / no_match
      （規格：未路由的三種原因可分辨）

## 4. 接進 ingress

- [x] 4.1 `analysis_provide` 落 blob 後呼叫分類器，行內、短逾時
- [x] 4.2 發 `proxy.route` 事件，含 payload / lane / reason / tokens / usd
- [x] 4.3 給 orchestrator 的通知帶上路由結果與理由
- [x] 4.4 回應的 `routed` 為布林並附 `routed_to` 與未路由原因
- [x] 4.5 分類失敗時 blob 仍然存在的測試（規格：分類失敗不影響資料落地）
- [x] 4.6 分類器不派工的測試（規格：分類不會產生執行）
- [x] 4.7 分類器不授權的測試：被判定的 lane 未經 `inputs` 仍讀不到
      （規格：分類不會產生授權）

## 5. 記帳

- [x] 5.1 `proxy.route` 的 usd/tokens 落在 `(proxy)` bucket
      （`events/query.py` 已如此實作，補測試）
- [x] 5.2 `unprocessed_payload` caveat 在有路由時不再誤報
      （`derive_caveats` 已讀 `proxy.route`，補測試）
- [x] 5.3 資料流圖把 `proxy.route` 呈現出來：新的 `SUGGESTED` 邊，
      外加 `suggestion_ignored` 異常（WARNING）—— 有那條邊的理由就是要能問
      「orchestrator 有沒有理它」

## 6. 文件與端到端

- [x] 6.1 `myharness/proxy/README.md`：為什麼只分類、為什麼不授權、零 context 共享
- [x] 6.2 `DESIGN.md` #4 標為已實作；§9 收斂為「觸發範圍」。**架構圖也改了** ——
      原圖把 proxy 的箭頭畫進 lane（看起來像會派工），且寫著已被 spike #1
      推翻的 `dispatch(blocking)`
- [x] 6.3 `myharness/mcp/README.md` 更新「還沒做：proxy」那一節
- [x] 6.4 離線端到端：宣告 routing table → provide → 路由 → orchestrator 收到通知
- [ ] 6.5 live：兩份不同型態的資料進同一個 job，確認分別路由到不同 lane
