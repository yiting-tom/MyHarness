## 1. 宣告

- [x] 1.1 `BackendCapability` 新增 `COST_REPORTING`
- [x] 1.2 `ANTHROPIC_DIRECT` 與 `OPENROUTER` 宣告支援；`SELF_HOSTED` 與
      `direct_openai_from_env()` 不宣告（capabilities 維持空集合）
- [x] 1.3 測試：未宣告的後端，其 profile 的 `supports(COST_REPORTING)` 為 False
      （規格：不宣告成本回報時金額不得用於控制決定）

## 2. 上限可以缺席

- [x] 2.1 `JobSpec.max_budget_usd` 型別放寬為 `float | None`，預設值不變
- [x] 2.2 `JobRunner._limit_breached()` 在 `None` 時略過金額檢查
- [x] 2.3 `_limit_value()` 一併處理 `None`
- [x] 2.4 測試：沒有金額上限時，派工次數與時間上限仍會觸頂收工
      （規格：沒有金額上限時仍然有界）

## 3. 由誰決定

- [x] 3.1 `OrchestratorLoop` 在後端未宣告 `COST_REPORTING` 且呼叫端未指定時，
      把 `max_budget_usd` 設為 `None`
- [x] 3.2 呼叫端明確指定也不使其復活 —— 不報成本的後端上，
      金額是虛構的而不是缺席的（規格：明確指定也不生效）
- [x] 3.3 測試：宣告成本回報的後端，金額上限行為不變
      （規格：宣告成本回報時金額上限照常生效）

## 4. 說出來

- [x] 4.1 `derive_caveats` 新增一種 kind：沒有金額上限，並載明仍生效的上限
      （規格：沒有金額上限時交付會說）
- [x] 4.2 有上限而未觸頂時不加註（規格：有金額上限而未觸頂時不加註）
- [x] 4.3 測試：兩種情況的交付可區分

## 5. 驗證

- [x] 5.1 離線套件全過，既有的金額上限測試不需修改
- [ ] 5.2 live：重跑 golden #7，斷言不再出現
      `limit.reached limit=max_budget_usd`，且報告帶上新的 caveat
- [ ] 5.3 對照 #7 的 5 次派工，記錄關掉金額閘之後派工數與時間的變化
- [ ] 5.4 記錄到 `spikes/RESULTS.md`
