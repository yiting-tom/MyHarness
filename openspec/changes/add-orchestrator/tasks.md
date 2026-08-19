## 1. Job runner 骨架

- [x] 1.1 定義 `JobSpec`（目標、硬上限：`max_dispatches` / `max_budget_usd` / `max_wall_clock_s`、peek 預算、提問配額）與 `JobPhase` 列舉
- [x] 1.2 定義 `JobState`：可序列化的 plain data（階段、任務登記、預算餘額、無進展計數、寬限餘額）
- [x] 1.3 實作 `JobRunner` 生命週期：建立、執行、收工、取消未收割任務（design D 風險緩解）
- [x] 1.4 實作 `status()`，回傳大小有界的狀態（規格：Job 狀態可被外部查詢）

## 2. 非阻塞派工與收割

- [x] 2.1 實作 `TaskRegistry`：`asyncio.create_task` 起背景 lane 執行，登記識別與狀態
- [x] 2.2 實作 `dispatch()` 立即返回任務識別（規格：派工立即返回）
- [x] 2.3 實作 `await_tasks(ids, mode="all"|"any", timeout)`，逾時回傳已完成者與未完成識別（規格：收割等到全部完成／可只等任一／逾時回報未完成者）
- [x] 2.4 實作跨 lane 並行的 semaphore，並確認背景任務執行區間確實重疊（規格：背景任務真正並行）
- [x] 2.5 收工時等待或取消仍在執行的任務，並記錄之

## 3. 三道防迴圈

- [x] 3.1 實作派工內容的正規化與 hash，`dispatch` 重複時回傳前次 handle 而不執行（規格：重複派工不執行）
- [x] 3.2 實作 job 硬上限檢查（次數／金額／時間），觸頂時注入收工要求（規格：觸頂時要求收工）
- [x] 3.3 實作寬限額度與用盡後的程式碼降級交付（規格：拒絕收工後才中止；design D4）
- [x] 3.4 實作無進展偵測與其重置（規格：無進展偵測的兩個 scenario）

## 4. Orchestrator 的工具面

- [x] 4.1 定義工具集合與其 schema：`plan_update` / `dispatch` / `await_tasks` / `peek` / `ask_user` / `finish`
- [x] 4.2 實作 `plan_update`：寫入計畫 note，並建立／更新 lane instance（規格：計畫隨進度更新）
- [x] 4.3 實作 `peek`，標記 `readOnlyHint=True` 以允許同輪併發（design D2）
- [x] 4.4 實作 peek 預算扣減與耗盡後的拒絕，且不影響其他工具（規格：Peek 有 job 級的總預算的三個 scenario）
- [x] 4.5 實作 `finish`：驗證報告 artifact 存在，收斂 job
- [x] 4.6 測試：工具面不隨 lane 數量成長、且無任何能回傳 blob 內容的路徑（規格：Orchestrator 的工具面是固定且極小的）

## 5. 使用者提問通道

- [x] 5.1 定義 `UserChannel` 抽象與 `Question` / `Answer` 型別
- [x] 5.2 實作 `DefaultingChannel`（永遠套用預設值）與 `ScriptedChannel`（測試用）
- [x] 5.3 實作 `ask_user` 的逾時、預設值與 job 級配額（規格：向使用者提問是抽象通道的三個 scenario）
- [x] 5.4 未確認的假設進入 caveats

## 6. 計畫與交接重啟

- [x] 6.1 實作計畫的讀寫（存為 note artifact，沿用既有預檢與版本；design D5）
- [x] 6.2 實作從既有計畫啟動全新 orchestrator（規格：計畫足以接手）
- [x] 6.3 實作 context 用量追蹤與門檻偵測
- [x] 6.4 實作交接請求與重啟，並保留未收割的任務（規格：Context 逼近上限時交接重啟的三個 scenario）

## 7. Synthesis 與交付

- [ ] 7.1 新增 synthesis lane type 與其 charter
- [ ] 7.2 實作報告產出：orchestrator 授權 synthesis lane 讀取相關 artifact 並派工（規格：Orchestrator 規劃而不彙整的兩個 scenario）
- [ ] 7.3 實作交付組裝：executive summary + key findings + 章節價目表（含 `est_tokens`）+ 自動蒐集的 caveats + 成本
- [ ] 7.4 確認硬上限觸頂時仍有交付（規格：善終後仍有交付）

## 8. 事件與可觀測性

- [x] 8.1 新增事件型別：`plan.update`、`peek`、`limit.reached`、`no_progress`、`handoff.restart`
- [x] 8.2 實作 peek 預算用量的聚合（規格：窺看預算的使用可被追蹤）
- [x] 8.3 實作收工原因的判定（規格：收工原因可從事件流判定）
- [x] 8.4 將交接重啟寫入事件流，含當時用量（規格：交接重啟被記錄）

## 9. 離線測試

- [x] 9.1 建立腳本化的 orchestrator transport，使整層可在無網路下端到端執行
- [ ] 9.2 覆蓋 `job-runner` 的全部 17 個 scenario
- [ ] 9.3 覆蓋 `orchestrator` 的全部 14 個 scenario
- [ ] 9.4 覆蓋 `event-log` delta 的 3 個 scenario
- [ ] 9.5 專門測試：反覆窺看時 orchestrator 的 context 成長受預算限制而收斂
- [ ] 9.6 專門測試：硬上限觸頂 → 寬限 → 程式碼降級交付的完整路徑

## 10. Golden job（live）

- [ ] 10.1 建立 golden job fixture：固定輸入、2–3 條 lane、可重跑
- [ ] 10.2 **Live**：完整跑一次，產出報告並記錄事件流
- [ ] 10.3 **Live**：斷言 context 峰值低於上界、無重複 dispatch、成本低於上界、必有交付
- [ ] 10.4 **Live**：驗證最終報告由 synthesis lane 產出，orchestrator 未讀入完整分析
- [ ] 10.5 量測並記錄：orchestrator 的實際 context 用量分佈（對照 `DESIGN.md` §5 的估算表）

## 11. 收尾

- [ ] 11.1 全套離線測試通過並記錄覆蓋率；golden job 至少完整跑過一次並記錄費用
- [ ] 11.2 撰寫 `myharness/orchestrator/README.md`
- [ ] 11.3 以 golden job 的實測數值更新 `DESIGN.md` §5 預算表與 §9 開放項目
- [ ] 11.4 `openspec validate add-orchestrator --strict` 通過
