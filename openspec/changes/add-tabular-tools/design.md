# Design — 表格資料處理工具

## D1 為什麼是 SQL 而不是 `run_python`

`run_python` 什麼都做得到，這正是問題。它讀得到任何檔案、輸出無界、
要真的關住它需要容器或 seccomp —— 那是另一個量級的工程，而且**沒有一個沙箱缺口
會以錯誤的形式出現**，只會以「授權模型其實不成立」的形式靜靜存在。

DuckDB 給的是宣告式、可界定、可沙箱化的子集，而且 spike #10 已經逐條驗證過關得住。
代價是它不做 ML、不畫圖、不跑任意轉換。接受這個代價：這個 harness 分析的是表格資料，
而逃生口是 `into` —— 查詢結果可以寫回成新的 blob，交給後續的 lane 或未來的其他工具。

未來若真需要任意運算，那應該是一個獨立的、跑在容器裡的 executor，而不是把這個
工具的閘門一格一格拔掉。

## D2 SQL 裡永遠不出現路徑

worker 指名 artifact id，harness 綁定表名。兩個理由：

1. worker 本來就不該知道路徑，也不需要知道。
2. **授權檢查要和其他所有檢查發生在同一個地方**：artifact id 上。
   若 SQL 可以帶路徑，就需要第二套 parser 級授權，而兩套授權裡比較弱的那套
   決定實際安全性。這是 handle contract 已經學過的「少一道只是很可能」。

表名由 artifact 名稱的最後一段消毒而來（`txn-2024` → `txn_2024`），
碰撞時加尾碼。**每一次回應（成功與失敗都是）都回報綁定表**，
所以 worker 不必猜，而寫錯表名的錯誤訊息自己就帶著正確答案。

## D3 ingest 進記憶體是沙箱的形狀，不是實作偷懶

view 是惰性的，table 不是。所以順序必須是「先把授權的檔案讀成 table，再關門」。
關門之後 worker 的 SQL 再也碰不到檔案系統。

spike #10 試過用 `allowed_paths` 做 per-file allowlist 來避開記憶體成本 ——
它是加法不是減法，只設它的情況下 `/etc/hosts` 照讀、`COPY` 還把授權的 blob 覆寫掉了。
而 `enable_external_access` 是啟動期選項，關掉之後開不回來，也就無法「先關再開一條縫」。

所以 blob 必須有 byte 上限。**上限存在的理由是沙箱，不是效能**，錯誤訊息要這樣說，
否則將來會有人以「機器記憶體夠大」為由把它調掉。

## D4 輸出走和 handle 一樣的兩道閘

查詢結果是資料，而資料進 context 正是這整個系統在防的事。所以：

- **列數閘**：預設回傳前 N 列。多取一列來判斷是否被截斷，不為了報總數而跑完全表。
- **字元閘**：即使列數在限內，總字元仍會被 clamp。一列有一個 4000 字的欄位是常態。

兩道都要，理由和 `clamp_handle()` 一樣：JSON Schema 管形狀、clamp 管長度，
一列 200 欄照樣可以在「50 列」之內炸掉 context。

`into` 是逃生口而不是選項之一：結果寫成新 blob，回傳只有 artifact id、列數、schema。
產出的 blob 落在 lane 自己的 namespace 底下，因此**自動落在既有授權模型內**
（`GrantSet.allows` 已經放行 own namespace），不需要新的授權來源 —— design.md D2
說沒有第三種存取來源，這裡就不能開一個。

## D5 逾時只能由外部執行

失控的 join 不會自己停。`conn.interrupt()` 從 timer thread 呼叫 0.51 秒內就中斷。
查詢跑在 `asyncio.to_thread` 裡，逾時由 harness 的 timer 打斷 —— 這是 harness 的
責任，不是 worker 自律的事。

## D6 單一 statement、限 SELECT

`execute()` 會跑多句（spike #10 實測兩句都執行）。所以工具自己用
`duckdb.extract_statements()` 要求**恰好一句、型別為 SELECT**。

限 SELECT 不會少掉什麼：CTE 是 SELECT，而在單句規則下 `CREATE TEMP TABLE`
本來就沒有下一句可以用它。

## D7 失敗是值，錯誤訊息要能被下一回合利用

SQL 寫錯是常態，不是例外。每個拒絕都回可行動的文字：綁定表名有哪些、
欄位有哪些、上限是多少、超過的話該用 `into`。worker 只有一次 context，
一個只說「錯了」的訊息等於浪費一整回合。

## D8 `localize_blob` 的 context manager 洩漏

現行實作在 `async with self.store.localize(...) as path:` **區塊內** return，
所以 `__aexit__` 在 worker 拿到路徑之前就跑完了。本地後端僥倖沒事
（yield 的就是永久檔），但 `store.py` 的合約明說物件儲存後端「下載到 scratch
並在離開時清掉」—— 那時 worker 會拿到一條已被刪除的路徑。

修法：toolbox 持有一個 `AsyncExitStack`，localize 的生命週期綁在 worker 執行上，
而不是綁在一個 return 就結束的區塊上。這個 bug 現在只是潛伏，等到接 MinIO
才會爆炸，而那時它看起來會像「檔案隨機消失」。
