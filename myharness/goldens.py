"""The golden job: a fixed input, a real run, and assertable bounds.

An orchestrator's planning quality cannot be asserted offline, and arguably not
at all. Its *discipline* can: how much context it used, whether it repeated
itself, what it spent, and whether a delivery came out. This module runs one
small job end to end so those numbers exist to assert against (design.md D7).

    python -m myharness.goldens --backend openrouter
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.dataflow import Anomaly, DataFlow, build_dataflow, critical, detect
from myharness.events.log import LocalEventLog
from myharness.events.query import summarize
from myharness.jobs.runner import JobRunner
from myharness.jobs.spec import JobSpec
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.local_layout import DEFAULT_ROOT
from myharness.orchestrator.delivery import Delivery, build_delivery
from myharness.orchestrator.loop import LoopOutcome, OrchestratorLoop

GOLDEN_CSV = Path("goldens/txn-2024.csv")
GOAL = (
    "分析 2024 年的交易資料，找出異常樣態並說明其特徵。\n"
    "報告中必須明確給出這兩個數字：\n"
    "(1) 資料中不重複帳戶的總數；\n"
    "(2) 平均交易金額最低的 channel 是哪一個。\n"
    "資料已上傳為 blob，欄位為 txn_id / ts / account / amount / channel。"
)


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """Facts about the fixture that can only be had by computing them.

    The fifth run delivered a report saying it could not read the data and
    passed every discipline assertion, because discipline was all they checked.
    A number the model cannot guess is the cheapest possible check that the
    harness actually analysed something -- 765 distinct accounts is not a
    plausible hallucination, and it is wrong for any other reading of the file.
    """

    rows: int
    accounts: int
    cheapest_channel: str

    def missing_from(self, report: str) -> list[str]:
        absent = []
        if str(self.accounts) not in report and f"{self.accounts:,}" not in report:
            absent.append(f"distinct accounts ({self.accounts})")
        if self.cheapest_channel not in report:
            absent.append(f"cheapest channel ({self.cheapest_channel})")
        return absent


def ground_truth(csv_path: Path = GOLDEN_CSV) -> GroundTruth:
    """Computed straight from the file, with none of the harness in the way."""
    import duckdb

    conn = duckdb.connect(":memory:")
    try:
        escaped = str(csv_path).replace("'", "''")
        conn.execute(f"CREATE TABLE t AS SELECT * FROM read_csv_auto('{escaped}')")
        (rows,), = conn.execute("SELECT count(*) FROM t").fetchall()
        (accounts,), = conn.execute("SELECT count(DISTINCT account) FROM t").fetchall()
        (channel,), = conn.execute(
            "SELECT channel FROM t GROUP BY 1 ORDER BY avg(amount) LIMIT 1"
        ).fetchall()
        return GroundTruth(rows, accounts, channel)
    finally:
        conn.close()


@dataclass(frozen=True, slots=True)
class Blob:
    name: str
    path: Path
    columns: tuple[str, ...]

    @property
    def schema(self) -> dict[str, Any]:
        return {"columns": list(self.columns), "format": "csv"}


@dataclass(frozen=True, slots=True)
class GoldenTask:
    """A question, its data, and what a report must contain to have answered it."""

    name: str
    goal: str
    blobs: tuple[Blob, ...]
    #: What the report is missing; empty means answered.
    check: Callable[[str], list[str]]


TXN_BLOB = Blob("raw/txn-2024", GOLDEN_CSV, ("txn_id", "ts", "account", "amount", "channel"))


def txn_task(csv_path: Path = GOLDEN_CSV) -> GoldenTask:
    return GoldenTask("txn", GOAL, (Blob(TXN_BLOB.name, csv_path, TXN_BLOB.columns),),
                      ground_truth(csv_path).missing_from)


COMPLAINTS_DIR = Path("goldens/complaints")
COMPLAINTS_GOAL = (
    "有三份資料：交易（txn_id / ts / account / amount / channel）、"
    "帳戶（account / kyc_risk / region）、"
    "客訴（complaint_id / account / txn_id / filed / text；text 是客戶寫的自由文字）。\n"
    "請回答：\n"
    "(1) 全部客訴中，有幾則實際上是在指控交易未經本人授權（盜刷、冒用、不認得的扣款）？"
    "有些客訴提到盜刷，但說明了其實不是，那些不算。\n"
    "(2) 依帳戶的 kyc_risk 分組，哪一組的客訴中這類指控的比例最高？\n"
    "(3) 被指控未授權的交易中，哪一個 channel 的件數最多？\n"
    "報告最後單獨一行，用資料中的原值回答：\n"
    "ANSWER: count=<則數>; riskiest=<kyc_risk>; channel=<channel>"
)

#: The count is judged by reading 2,500 texts; a few misreads are not a wrong
#: answer, a keyword count is. Counting 盜/冒用 gives 485 against 569 -- 15% off.
COUNT_TOLERANCE = 0.10

_ANSWER = re.compile(
    r"ANSWER\s*[:：].*?count\s*=\s*([\d,]+).*?riskiest\s*=\s*(\w+).*?channel\s*=\s*(\w+)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ComplaintsTruth:
    count: int
    riskiest: str
    channel: str

    def missing_from(self, report: str) -> list[str]:
        """Read off the ANSWER line, not anywhere in the text: a report that
        lists every channel would otherwise "contain" the right one."""
        found = _ANSWER.search(report)
        if found is None:
            return ["ANSWER line"]
        count, riskiest, channel = int(found[1].replace(",", "")), found[2], found[3]
        absent = []
        if abs(count - self.count) > self.count * COUNT_TOLERANCE:
            absent.append(f"count ({self.count}±{COUNT_TOLERANCE:.0%}, said {count})")
        if riskiest.lower() != self.riskiest:
            absent.append(f"riskiest ({self.riskiest}, said {riskiest})")
        if channel.lower() != self.channel:
            absent.append(f"channel ({self.channel}, said {channel})")
        return absent


def complaints_truth(data: Path = COMPLAINTS_DIR, txns: Path = GOLDEN_CSV) -> ComplaintsTruth:
    """From the answer key, which no agent is given."""
    import duckdb

    conn = duckdb.connect(":memory:")
    try:
        joined = (f"FROM read_csv_auto('{data / 'complaints.csv'}') c "
                  f"JOIN read_csv_auto('{data / 'labels.csv'}') l USING (complaint_id) "
                  f"JOIN read_csv_auto('{data / 'accounts.csv'}') a USING (account) "
                  f"JOIN read_csv_auto('{txns}') t USING (txn_id)")
        unauthorized = "l.label = 'unauthorized'"
        (count,), = conn.execute(f"SELECT count(*) {joined} WHERE {unauthorized}").fetchall()
        (riskiest,), = conn.execute(
            f"SELECT a.kyc_risk {joined} GROUP BY 1 "
            f"ORDER BY avg(({unauthorized})::INT) DESC LIMIT 1").fetchall()
        (channel,), = conn.execute(
            f"SELECT t.channel {joined} WHERE {unauthorized} "
            f"GROUP BY 1 ORDER BY count(*) DESC LIMIT 1").fetchall()
        return ComplaintsTruth(count, riskiest, channel)
    finally:
        conn.close()


def complaints_task() -> GoldenTask:
    return GoldenTask("complaints", COMPLAINTS_GOAL, (
        TXN_BLOB,
        Blob("raw/accounts", COMPLAINTS_DIR / "accounts.csv", ("account", "kyc_risk", "region")),
        Blob("raw/complaints", COMPLAINTS_DIR / "complaints.csv",
             ("complaint_id", "account", "txn_id", "filed", "text")),
    ), complaints_truth().missing_from)


TASKS: dict[str, Callable[[], GoldenTask]] = {"txn": txn_task, "complaints": complaints_task}

ANALYST_TOOLS = (
    "read_note", "write_finding", "update_state",
    "localize_blob", "inspect_blob", "duckdb_query",
)


def lane_types(
    charters: Path = Path("charters"), backend: str = "openrouter"
) -> LaneRegistry:
    """Lane types for the golden job, all on one backend.

    The first run put the lanes on the default backend while the orchestrator
    ran on OpenRouter; every dispatch then failed 401 against a stale key in the
    shell. Backend belongs to the job, not to a per-type default.
    """
    return LaneRegistry(
        LaneType(
            name="tabular-analyst",
            charter_path=charters / "tabular-analyst.md",
            tools=ANALYST_TOOLS, model_tier="strong", backend=backend,
            token_budget=150_000, max_turns=12, state_max_tokens=2_000,
            description="表格與交易資料的統計分析；可直接處理大型 CSV",
        ),
        LaneType(
            name="critic",
            charter_path=charters / "critic.md",
            # Deliberately the same two tools as the synthesizer: a critic that
            # could query the data would just redo the analysis, and its
            # findings would compete with the analyst's instead of examining
            # them. Without query tools it can only ask whether a conclusion
            # stands on the evidence it presents -- which is the job.
            tools=("read_note", "write_finding"),
            model_tier="strong", backend=backend,
            token_budget=150_000, max_turns=8, state_max_tokens=1_000,
            description=(
                "讀其他 lane 的 finding，找出沒有樣本數支撐的結論、"
                "大於證據的宣稱、未經檢驗的假設。不查資料，只檢查推論。"
                "在收斂成報告之前派它"
            ),
        ),
        LaneType(
            name="synthesizer",
            charter_path=charters / "synthesizer.md",
            tools=("read_note", "write_finding"), model_tier="strong", backend=backend,
            token_budget=150_000, max_turns=8, state_max_tokens=1_000,
            description="讀取多份 finding 並收斂成一份給人閱讀的報告",
        ),
    )


@dataclass
class GoldenResult:
    outcome: LoopOutcome
    delivery: Delivery
    summary: Any
    blob_id: str
    flow: DataFlow
    anomalies: list[Anomaly]
    report_text: str

    @property
    def critical(self) -> list[Anomaly]:
        return critical(self.anomalies)

    def report_line(self) -> str:
        s, o = self.summary, self.outcome
        anomaly_note = (
            "\nanomalies=" + ", ".join(f"{a.kind}({a.severity})" for a in self.anomalies)
            if self.anomalies else "\nanomalies=none"
        )
        return anomaly_note.lstrip("\n") + "\n" + (
            f"phase={o.phase} salvaged={o.salvaged} turns={o.turns} "
            f"handoffs={o.handoffs}\n"
            f"context_peak={o.context_peak:,} dispatches={s.dispatches} "
            f"duplicates={s.duplicates} failures={s.failures}\n"
            f"cost=${s.total_usd:.4f} peek={s.peek_tokens} "
            f"throttle={s.throttle_seconds}s cache_hit={s.cache_hit_ratio}\n"
            f"caveats={[c.kind for c in s.caveats]}"
        )


async def put_task_blobs(store: LocalArtifactStore, job_id: str, task: GoldenTask) -> list[str]:
    return [str((await store.put_blob(job_id, b.name, source=b.path, produced_by="user",
                                      schema=b.schema)).id) for b in task.blobs]


def available(ids: list[str]) -> str:
    return "可用資料：\n" + "\n".join(f"- {i}" for i in ids)


async def run_golden(
    root: Path,
    *,
    job_id: str = "golden",
    backend: str = "openrouter",
    csv_path: Path = GOLDEN_CSV,
    charters: Path = Path("charters"),
    spec_overrides: dict[str, Any] | None = None,
    task: GoldenTask | None = None,
) -> GoldenResult:
    task = task or txn_task(csv_path)
    store = LocalArtifactStore(root)
    await store.init_job(job_id)
    events = LocalEventLog(root)

    ids = await put_task_blobs(store, job_id, task)
    spec = JobSpec(
        job_id=job_id, goal=f"{task.goal}\n\n{available(ids)}",
        max_dispatches=12, max_budget_usd=1.0, max_wall_clock_s=1800.0,
        peek_budget_tokens=8_000, question_quota=2,
        **(spec_overrides or {}),
    )
    runner = JobRunner(spec, store=store, event_log=events)
    loop = OrchestratorLoop(
        runner=runner, lanes=lane_types(charters, backend=backend), backend=backend
    )

    outcome = await loop.run()
    stream = await events.read(job_id)
    delivery = await build_delivery(
        store=store, events=stream, job_id=job_id, status=str(outcome.phase),
        report_artifact=outcome.report_artifact,
    )
    flow = build_dataflow(stream, await store.list(job_id), job_id=job_id)
    report_text = ""
    if outcome.report_artifact:
        from myharness.artifacts.ids import ArtifactId

        try:
            report_text = await store.read_note(
                ArtifactId.parse(outcome.report_artifact),
                grants=GrantSet.unrestricted(job_id), max_tokens=100_000,
            )
        except Exception:  # noqa: BLE001 - a missing report is the assertion's job
            report_text = ""
    return GoldenResult(outcome, delivery, summarize(stream), ids[0],
                        flow, detect(flow), report_text)


def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT / "golden")
    parser.add_argument("--backend", default="openrouter")
    parser.add_argument("--job-id", default="golden")
    args = parser.parse_args(argv)

    result = asyncio.run(run_golden(args.root, job_id=args.job_id, backend=args.backend))
    print("\n--- golden job ---")
    print(result.report_line())
    if result.anomalies:
        print("\n--- 資料流異常 ---")
        for anomaly in result.anomalies:
            print(f"  [{anomaly.severity.upper()}] {anomaly.detail}")
    truth = ground_truth()
    missing = truth.missing_from(result.report_text)
    print("\n--- 分析是否真的發生 ---")
    print(f"  ground truth: {truth.rows:,} rows, {truth.accounts} accounts, "
          f"cheapest channel {truth.cheapest_channel}")
    print("  報告缺少：" + (", ".join(missing) if missing else "無"))

    print("\n--- delivery ---")
    print(json.dumps(result.delivery.to_dict(), ensure_ascii=False, indent=2)[:2500])
    return 0 if result.delivery.report_artifact else 1


if __name__ == "__main__":
    sys.exit(main())
