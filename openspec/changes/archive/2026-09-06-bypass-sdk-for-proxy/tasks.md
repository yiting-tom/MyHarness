## 1. 直接路徑

- [x] 1.1 `myharness/proxy/direct.py`：以 `BackendProfile.base_url` /
      `auth_token_env` 發一次 messages 請求，不經 SDK
- [x] 1.2 只送分類器自己的 system + user prompt
      （規格：直接路徑的請求只含自己的提示）
- [x] 1.3 回傳與 SDK 路徑相同形狀的 `(text, usd, tokens_in, tokens_out)`
- [x] 1.4 逾時、非 2xx、無法解析的回應各自的降級

## 2. 選路

- [x] 2.1 `classify()` 在後端**宣告 `direct_wire`** 時走直接路徑，否則走 SDK
      （規格：不支援直接呼叫的後端仍可分類）
      — 原文寫「有 `base_url` 時」，那是錯的：OpenRouter 有 base_url 但講
      Anthropic，照那個條件會被送去直接路徑打 404。見 RESULTS.md §Spike #17
- [x] 2.2 兩條路徑的 `Routing` 結果形狀相同的測試
      （規格：兩條路徑的結果形狀相同）
- [x] 2.3 現有的 `transport` 注入點保留，離線測試不改

## 3. 節流

- [x] 3.1 直接路徑走既有的 `BackendGate`（規格：分類請求經過節流閘）
- [x] 3.2 放棄時仍降級為未路由，blob 已落地（規格：放棄時仍降級為未路由）
- [x] 3.3 測試：冷卻中的 backend 會讓分類等待而不是直接打過去

## 4. 驗證

- [x] 4.1 量測直接路徑的 input tokens，與 spike #12 的 8,991 對照
- [x] 4.2 live：重跑 spike #12，斷言路由結果不變而 token 數大幅下降
- [x] 4.3 記錄到 `spikes/RESULTS.md`
