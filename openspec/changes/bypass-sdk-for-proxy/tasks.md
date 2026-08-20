## 1. 直接路徑

- [ ] 1.1 `myharness/proxy/direct.py`：以 `BackendProfile.base_url` /
      `auth_token_env` 發一次 messages 請求，不經 SDK
- [ ] 1.2 只送分類器自己的 system + user prompt
      （規格：直接路徑的請求只含自己的提示）
- [ ] 1.3 回傳與 SDK 路徑相同形狀的 `(text, usd, tokens_in, tokens_out)`
- [ ] 1.4 逾時、非 2xx、無法解析的回應各自的降級

## 2. 選路

- [ ] 2.1 `classify()` 在後端有 `base_url` 時走直接路徑，否則走 SDK
      （規格：不支援直接呼叫的後端仍可分類）
- [ ] 2.2 兩條路徑的 `Routing` 結果形狀相同的測試
      （規格：兩條路徑的結果形狀相同）
- [ ] 2.3 現有的 `transport` 注入點保留，離線測試不改

## 3. 節流

- [ ] 3.1 直接路徑走既有的 `BackendGate`（規格：分類請求經過節流閘）
- [ ] 3.2 放棄時仍降級為未路由，blob 已落地（規格：放棄時仍降級為未路由）
- [ ] 3.3 測試：冷卻中的 backend 會讓分類等待而不是直接打過去

## 4. 驗證

- [ ] 4.1 量測直接路徑的 input tokens，與 spike #12 的 8,991 對照
- [ ] 4.2 live：重跑 spike #12，斷言路由結果不變而 token 數大幅下降
- [ ] 4.3 記錄到 `spikes/RESULTS.md`
