## 1. 前置驗證與相依

- [x] 1.1 完成 spike #2：在 SDK in-process `@tool` handler 內啟動巢狀 `query()`，驗證穩定性與子行程資源回收（連跑 20 次後檢查殘留行程與 fd），結果記入 `spikes/RESULTS.md`
- [x] 1.2 將 `claude-agent-sdk` 與 `jsonschema` 加入 `pyproject.toml` 的執行期相依
- [x] 1.3 加入 `live` pytest marker 與預設 `-m "not live"`，並在 README 說明如何跑 live 測試與預期費用

## 2. Backend profile

- [x] 2.1 定義 `BackendProfile`：`base_url`、`auth_token_env`、`model_aliases`、`capabilities`（規格：Per-lane 的後端設定、模型別名映射）
- [x] 2.2 定義 `BackendCapability` 列舉：結構化輸出、prompt caching、API 端預算
- [x] 2.3 實作金鑰解析：從環境變數讀取，缺少時在執行前以明確訊息失敗（規格：缺少金鑰時明確失敗）
- [x] 2.4 實作模型別名解析，未映射時失敗並列出可用別名（規格：未映射的別名明確失敗）
- [x] 2.5 實作 `to_sdk_env()`：產生 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 等環境覆寫
- [x] 2.6 內建三個預設 profile：Anthropic 直連、OpenRouter、自架 OpenAI-compatible 代理
- [x] 2.7 定義 `BUILTIN_TOOLS` 清單與 `disallowed_for(declared_tools)`，將未宣告的內建工具排除（規格：內建工具的裁切）

## 3. Handle 契約

- [x] 3.1 定義 `HANDLE_SCHEMA`（`artifact`、`headline`、`confidence`、選填 `metrics`、`followups`）與對應的 `LaneHandle` 型別
- [x] 3.2 定義失敗 handle 的 `status` 值域與欄位（`budget_exceeded`、`tool_failure`、`max_turns`、`state_rejected`、`schema_violation`、`backend_unavailable`）
- [x] 3.3 實作 `clamp_handle()`：逐欄位長度上限 + 整體序列化上限，超出即截斷並標記 `truncated`（規格：過長的欄位被截斷而非放行；design D2）
- [x] 3.4 實作應用層 schema 驗證與重新提示，供不支援結構化輸出的後端降級使用（規格：不支援時退回應用層驗證）
- [x] 3.5 單元測試：schema 合法但欄位過長、整體過大、缺必填欄位、多餘欄位

## 4. Lane type 與 instance

- [x] 4.1 定義 `LaneType`：charter 路徑、宣告的工具、模型別名、backend、token 預算、`max_turns`、state 上限（規格：Lane type 與 lane instance 分離）
- [x] 4.2 實作 charter 檔案載入與雜湊計算（design D5）
- [x] 4.3 定義 `LaneInstance`：id、type、scope 描述、namespace 推導
- [x] 4.4 實作 `LaneRegistry`：註冊 type、建立 instance、未註冊 type 時列出可用清單（規格：拒絕未註冊的 lane type）
- [x] 4.5 單元測試：同型別兩個 instance 的 namespace 與 state artifact 互不重疊（規格：同型別的兩個 instance 各持有獨立 state）

## 5. Worker 的工具面

- [x] 5.1 實作 in-process MCP 工具 `read_note`，以 `GrantSet` 檢查授權，錯誤以結構化文字回給 worker（規格：Worker 的可讀範圍受授權限制；design D4）
- [x] 5.2 實作 `write_finding`：寫入 lane namespace 下的 findings artifact
- [x] 5.3 實作 `update_state`：以 compare-and-set 寫入 lane state，超過上限時拒絕並回報（規格：Lane state 的上限與寫入安全）
- [x] 5.4 實作 `localize_blob`：以 context manager 物化 blob 供工具使用，並檢查授權
- [x] 5.5 單元測試：未授權讀取失敗且錯誤對 worker 可見（規格：讀取未授權的 artifact 失敗）

## 6. Worker 執行迴圈

- [x] 6.1 實作 prompt 組裝：charter + lane state + 任務描述 + input handle 清單（規格：Ephemeral worker 的執行循環）
- [x] 6.2 實作 `run_lane_worker` 主迴圈，邊串流邊累積訊息、token 用量與已寫入的 artifact（design D1）
- [x] 6.3 依 backend capability 選擇強制路徑或降級路徑，並記錄實際採用的路徑（規格：Backend capability 的宣告與降級）
- [x] 6.4 實作例外到失敗 handle 的轉換，不解析例外訊息字串來分類（規格：失敗是值而非例外；design 風險緩解）
- [x] 6.5 實作本地 token 累計與硬斷，供不支援 API 端預算的 backend 使用（規格：不支援 API 端預算時以本地計數硬斷）
- [x] 6.6 實作 transient 錯誤的有上限退避重試，且語意失敗不重試（規格：Transient 錯誤由 framework 處理）
- [x] 6.7 實作 transcript 落盤與在 handle、事件中的引用（規格：執行過程可完整重現）
- [x] 6.8 寫入 `dispatch.start` / `dispatch.end` / `ctx` 事件，含 status、artifact、tokens、turns、usd、transcript、charter 雜湊（規格：執行寫入事件流）

## 7. 離線測試

- [x] 7.1 建立可錄製／回放的假 transport 或訊息序列 fixture，使所有分支不需網路即可測
- [x] 7.2 覆蓋規格「Ephemeral worker 的執行循環」的三個 scenario，含「前一次對話不影響下一次」
- [x] 7.3 覆蓋規格「Lane state 的上限與寫入安全」的兩個 scenario
- [x] 7.4 覆蓋規格「失敗是值而非例外」的三個 scenario，並斷言 `run_lane_worker` 不拋出語意例外
- [x] 7.5 覆蓋規格「Transient 錯誤由 framework 處理」的三個 scenario
- [x] 7.6 覆蓋規格「執行過程可完整重現」與「執行寫入事件流」的各兩個 scenario
- [x] 7.7 覆蓋規格「Backend capability 的宣告與降級」的三個 scenario
- [x] 7.8 覆蓋 `model-backend` 的其餘 scenario：不同後端、缺金鑰、別名解析、工具裁切

## 8. Live 測試（驗證本 change 的存在理由）

- [x] 8.1 建立最小的純程式碼 driver：註冊一個 lane type、建立 instance、餵一個真實任務、印出 handle
- [ ] 8.2 **Live**：被明確要求寫 3000 字報告時，回傳的 handle 仍符合 schema 且不超過大小上界（規格：被要求寫長文時仍只回傳 handle）
- [ ] 8.3 **Live**：`task_budget` 設到不足以完成任務，驗證得到 `budget_exceeded` handle 而非例外，且含部分結果引用（規格：超出預算回傳部分結果）
- [ ] 8.4 **Live**：`max_turns` 設到不足，驗證得到 `max_turns` handle 而非例外
- [ ] 8.5 **Live**：同一 lane 連續兩次任務，驗證第二次能看到第一次寫入 state 的結論、且看不到第一次未寫入 state 的細節
- [ ] 8.6 **Live**：以 OpenRouter 的非 Anthropic 模型執行一次，驗證 handle 契約仍成立
- [ ] 8.7 量測並記錄：裁切前後的固定 prefix token 數、prompt cache 命中率、單次執行成本，寫入 `spikes/RESULTS.md`

## 10. 後端節流（live 測試發現：付費模型仍持續 429）

- [x] 10.1 定義 `BackendGate`：per-backend 的並行 semaphore + 共享冷卻狀態（規格：每個後端共享的節流閘）
- [x] 10.2 實作冷卻的取得與設定，含「不同後端互不影響」（規格：不同後端的冷卻互不影響）
- [x] 10.3 實作帶 full jitter 的指數退避與時間預算（規格：重試以時間預算為界，且帶隨機抖動）
- [x] 10.4 以 `CLAUDE_CODE_MAX_RETRIES` 壓低 SDK 內建重試，讓節流政策由 gate 單一掌控
- [x] 10.5 將 gate 接入 `run_lane_worker`，取代目前的固定次數退避
- [x] 10.6 新增 `throttle.cooldown` / `throttle.wait` / `throttle.gave_up` 事件型別與聚合（規格：節流事件寫入事件流）
- [x] 10.7 測試：覆蓋節流閘與退避的全部 8 個 scenario，含冷卻跨 worker 生效與抖動

## 9. 收尾

- [ ] 9.1 全套離線測試通過並記錄覆蓋率；live 測試至少完整跑過一次並記錄結果與費用
- [x] 9.2 撰寫 `myharness/lanes/README.md`：如何定義一個 LaneType、如何寫 charter、如何新增 backend
- [x] 9.3 更新根目錄 `DESIGN.md`：將 §4.3 LaneType、§4.1 handle、§5b backend 對齊實際實作
- [ ] 9.4 更新 `DESIGN.md` §9 開放項目：移除已由本 change 定案者，補上實測得出的新數值
- [x] 9.5 `openspec validate add-lane-worker --strict` 通過
