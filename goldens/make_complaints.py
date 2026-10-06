"""Generate the complaints fixture: a question one context cannot read its way through.

    python goldens/make_complaints.py

The golden CSV is one table a single agent answers with SQL alone (README,
"量測"). This adds what SQL cannot do: 2,500 free-text complaints whose
meaning has to be read. Some mention 盜刷 and are not about it ("我原本以為
被盜刷，後來發現是家人刷的"), so a keyword count is wrong; and the answer
needs them joined to transactions and to KYC risk.

Deterministic (fixed seed); outputs are committed. labels.csv is the answer
key and is never given to an agent.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "complaints"
SEED = 20261006
N_COMPLAINTS = 2_500

#: Each kind is built from three slots, so a text is one of hundreds: few
#: enough templates and an agent could strip the frame, SELECT DISTINCT, and
#: classify thirty sentences instead of reading.

#: Claims the transaction was not theirs, worded every way but one keyword.
UNAUTHORIZED = (
    ["我從來沒有刷過這筆。", "帳單上出現一筆我完全不認得的扣款。", "有人冒用我的帳號付款。",
     "這個商家我從沒聽過，卻扣了我的錢。", "我的卡被盜刷了。", "這筆不是我本人操作的。",
     "出現一筆陌生的消費。", "這筆交易未經我同意。"],
    ["卡一直在我身上，", "那天我人在國外，", "手機沒收到任何驗證碼，", "家人都確認過沒用過這張卡，",
     "時間是半夜三點，我在睡覺，", "我已經報警了，", "密碼好像被別人拿走了，"],
    ["請立刻凍結並退回款項。", "請協助爭議款處理。", "要求調查是誰用的。",
     "請當作冒用處理。", "懷疑資料外洩，請查明。", "請盡快把錢退回來。"],
)

#: Mentions theft, then says it was not: the trap for a keyword count.
DENIED = (
    ["我原本以為被盜刷，", "一開始懷疑卡片被冒用，", "朋友說這種扣款可能是盜刷，",
     "看到通知還以為是詐騙，", "本來想申報陌生扣款，", "差點就當成盜用報案了，"],
    ["後來發現是家人刷的，", "查了才知道是自己訂閱的服務續約，", "結果確認是我自己買的，",
     "打電話問才知道是年費，", "後來想起是小孩用我手機買點數，", "核對後是我在店裡刷的，"],
    ["只是金額跟發票不符。", "想問怎麼取消。", "希望可以減免。", "想問能不能退。",
     "但店員多打了一個零。", "但退款到現在還沒入帳。", "只是想確認帳單日期。"],
)

#: Ordinary complaints.
OTHER = (
    ["退款申請送出三週了還沒有消息，", "同一筆消費被扣了兩次，", "實際扣款比收據多了一些，",
     "付款時一直跳錯誤卻被扣了錢，", "ATM 吐鈔少了一張但全額扣款，", "分期利息跟當初說的不一樣，",
     "手續費沒有事先告知，", "優惠回饋金一直沒有入帳，", "外幣匯率跟官網公告差很多，"],
    ["客服電話一直打不通，", "已經寫信反映過一次，", "分行說要我自己處理，", "App 上查不到紀錄，", ""],
    ["請協助處理。", "請說明原因。", "希望可以退還。", "請盡快回覆。", "請重新計算。"],
)

#: Wrapped around the core so that no two texts match and reading is required.
OPENERS = ["您好，", "你好，", "", "想反映一件事：", "麻煩處理一下，", "急件！", "客服您好，"]
CLOSERS = ["", "謝謝。", "請盡快回覆。", "已經是第二次反映了。", "麻煩了。", "希望今天能處理。"]


def main() -> None:
    rng = random.Random(SEED)
    with (HERE / "txn-2024.csv").open(encoding="utf-8") as f:
        txns = list(csv.DictReader(f))
    accounts = sorted({t["account"] for t in txns})
    risk = {a: rng.choices(["low", "medium", "high"], [50, 35, 15])[0] for a in accounts}

    # Theft claims lean to high-risk accounts and to web, so the answer is
    # something the data says rather than a tie.
    def theft_weight(t: dict[str, str]) -> float:
        return (4.0 if risk[t["account"]] == "high" else 1.0) * (3.0 if t["channel"] == "web" else 1.0)
    theft_weights = [theft_weight(t) for t in txns]

    OUT.mkdir(exist_ok=True)
    with (OUT / "accounts.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["account", "kyc_risk", "region"])
        for a in accounts:
            w.writerow([a, risk[a], rng.choice(["北部", "中部", "南部", "東部"])])

    rows, labels = [], []
    for i in range(N_COMPLAINTS):
        kind = rng.choices(["unauthorized", "denied", "other"], [22, 18, 60])[0]
        txn = (rng.choices(txns, theft_weights)[0] if kind == "unauthorized"
               else rng.choice(txns))
        slots = {"unauthorized": UNAUTHORIZED, "denied": DENIED, "other": OTHER}[kind]
        core = "".join(rng.choice(slot) for slot in slots)
        text = f"{rng.choice(OPENERS)}交易 {txn['txn_id']}：{core}{rng.choice(CLOSERS)}"
        cid = f"C{i:05d}"
        rows.append([cid, txn["account"], txn["txn_id"], txn["ts"][:10], text])
        labels.append([cid, "unauthorized" if kind == "unauthorized" else "other"])

    for name, header, body in (
        ("complaints.csv", ["complaint_id", "account", "txn_id", "filed", "text"], rows),
        ("labels.csv", ["complaint_id", "label"], labels),
    ):
        with (OUT / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(body)


if __name__ == "__main__":
    main()
