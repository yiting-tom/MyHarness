# tabular — lane worker 的資料處理工具

Lane worker 能對**被授權的** blob 下 SQL。這一層存在的理由是：
harness 的其他部分都在防止資料進入 context，而分析資料又必然要碰資料。

## 兩個工具

```
inspect_blob(artifact)              欄位、型別、列數、前 5 列、綁定的表名
duckdb_query(artifacts, sql, into)  一句 SELECT；結果受兩道上限；into 寫成新 blob
```

## 為什麼 SQL 裡不能有路徑

授權判定發生在 artifact id 上 —— 和 `read_note`、`localize_blob` 完全同一套。
如果 SQL 可以帶路徑，就需要**第二套** parser 級授權，而兩套授權裡比較弱的那套
決定實際安全性。所以 worker 指名 artifact，harness 綁定表名，
**每一次回應（成功與失敗都是）都回報綁定表**。

## 沙箱

```
1. 把每個被授權的 blob ingest 成 table     ← view 是惰性的，table 不是
2. SET enable_external_access=false        ← 圍籬
3. 關閉三項擴充套件設定
4. SET lock_configuration=true             ← 縱深防禦，必須最後
5. 此時才執行 worker 的 SQL
```

**圍籬只有第 2 行。** 它在 duckdb 1.5.5 自我防衛（開不回來，且連 `allowed_paths`
都一併凍結）。第 4 行不是第二道圍籬，它釘住 `autoload`/`autoinstall`，
並讓上述自我防衛（實作性質，非 API 承諾）不是唯一依靠。
`tests/tabular/test_sandbox.py` 兩個測試各自釘住這兩件事。

順序是設計的一部分：ingest 必須在關門之前，所以 blob 必須有大小上限（256 MiB）。
**那個上限是沙箱的形狀，不是效能調校** —— spike #10 試過 `allowed_paths` 這條
「不用 ingest」的路，它是加法不是減法，只設它的話 `/etc/hosts` 照讀、
`COPY` 還會覆寫掉授權的 blob。

## duckdb 不會幫你做的兩件事

1. **`execute()` 會跑多句。** 所以 `guard.py` 自己要求恰好一句、型別 SELECT。
2. **失控查詢不會自己停。** `run_guarded()` 用 timer thread 呼叫 `interrupt()`。

## 兩道輸出上限

和 handle contract 同一個形狀：**列數管形狀、字元數管長度**，缺一道只是「很可能」。

- 50 列 —— 取 51 列判斷是否截斷，不為了報總數跑完全表
- 4000 字元 —— 40 欄 × 180 字的 20 列完全在列數限內，照樣灌爆 context
- 單格 200 字元 —— 更細的一道，不能取代前兩道

截斷一律明說。默默縮短的結果，會變成 worker 自信地報告一個不是最大值的最大值。

## `into` 是逃生口

`into="totals"` 把完整結果**分批**寫成 `<lane namespace>/derived/totals`，
回傳只有 artifact id、列數、欄位。它落在 lane 自己的 namespace 底下，
所以 `GrantSet.allows` 本來就放行 —— **沒有引入第三種存取來源**（DESIGN #10）。
之後可以把它當成一般 blob 再查一次。

## 併發

兩個工具都標為非唯讀，也就是同 turn 不併發（spike #1）。
理由不是寫入（`inspect_blob` 不寫），而是**每次呼叫都會把整份 blob 讀進記憶體**。
另有 process 層的 semaphore，因為多條 lane 跑在同一個 event loop 上。
