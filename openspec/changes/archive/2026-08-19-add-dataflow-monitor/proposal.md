## Why

這個 harness 刻意把資料藏起來：blob 不可能被讀進 context、lane 的可讀範圍由授權清單
決定、worker 做完就消失。那些性質讓它能在 196k 內運作，代價是**出事時沒有人看得見發生了什麼**。

代價已經以實例出現。Golden job 跑了五次才通，每一次的診斷都是用臨時腳本翻事件流，
其中兩個 bug 只在把事件排成資料流之後才浮現：

- 第四次：`inputs` 傳成物件被 `str()` 毀損，失敗在兩條 lane 之後以難以理解的
  `not_granted` 出現。
- 第五次（表面上成功的那次）：synthesizer 被派了兩次，**第二次沒有帶任何授權**，
  卻仍寫出報告並覆蓋掉真正拿到三份 finding 的那一版。最終交付的是「我什麼都讀不到」。
  這件事在事件流的逐行輸出裡看不出來，排成流向圖一眼就看到。

`DESIGN.md` D7 從一開始就說「成本報表、TUI、OpenTelemetry、回歸測試全部是事件流的投影」。
事件流已經是唯一事實來源，缺的只是把它投影出來的那一層。

另一個立即的痛點：一次 golden job 要跑 30 分鐘，期間完全看不到進度 ——
分不出它是在思考、在等限流、還是已經卡死。

## What Changes

- 新增 `dataflow` capability：從事件流推導資料的流向與來源鏈 ——
  哪份資料進了哪條 lane、憑什麼授權、產出了什麼、誰又讀了那個產出。
- 新增 `monitor` capability：兩個終端介面。`monitor` 即時跟蹤執行中的 job，
  `inspect` 事後展開完整的資料流、成本分布與異常。
- 定義「資料流異常」的偵測：無授權卻有產出、產出被覆蓋、孤兒 artifact、
  未被任何人讀取的產出。
- 不新增任何事件寫入。Monitor **只讀**事件流與 artifact index。

## Capabilities

### New Capabilities
- `dataflow`: 由事件流推導出的資料流模型 —— 節點（blob / finding / report / lane）
  與邊（授權、產出、讀取），以及據此偵測的資料流異常。純函式，不觸碰網路或 LLM。
- `monitor`: 終端呈現層。即時模式跟蹤執行中的 job 並顯示進度、成本與正在等待什麼；
  事後模式展開資料流、來源鏈、成本歸屬與偵測到的異常。

### Modified Capabilities
（無 —— `event-log` 的既有 requirement 已足以支撐這一層。若某個資料流事實無法從
現有事件推導，那是 `event-log` 的缺口，應在該處補齊而非在此繞過。）

## Impact

- **新增程式碼**：`myharness/dataflow/`（模型、推導、異常偵測）、
  `myharness/monitor/`（即時與事後的終端 renderer）、CLI 進入點，以及測試。
- **相依**：不引入新的執行期相依。終端輸出以標準庫實作，不使用 rich/textual ——
  這層的價值在於正確地投影，不在於畫面漂亮。
- **對既有層**：唯讀。不修改任何既有模組的行為。
- **回歸價值**：異常偵測可被 golden job 直接斷言 ——
  「這次跑有沒有出現無授權產出」會變成一條測試，而不是一次幸運的發現。
- **不含**：web 介面、OpenTelemetry 匯出、跨 job 的彙總儀表板。
