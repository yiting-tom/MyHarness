"""Spike #23: which of the harness's answers has no A2A task state?

Change `expose-over-a2a` D5 says a job lives in one process: `JobManager` holds
an `asyncio.Task` in memory, `result` and `drill_section` read only the event log
and the store, and a job in flight cannot be picked up by a second process.
`AnalysisService._poll_finished_elsewhere` already splits the answer in two:

    A job this process did not run is not the same as no job at all.
    The two need different handling by the client.

Spike #13 found that TaskState has nine values and none of them says "exists but
is not running here", and left the mapping to this spike. Doing the mapping
properly splits that case again, and only one half is actually homeless.

The JSON-RPC binding's error codes are NOT checked here: the canonical proto
carries no error enum, and specification/json/a2a.json is not at the path this
repo's other A2A spikes fetch from. So "what a not-found is called on the wire"
is recorded as unverified rather than guessed.

(Numbered 23, not 15: spikes/spike15_token_rates.py already exists.)

Run: python spikes/spike23_a2a_task_states.py
     python spikes/spike23_a2a_task_states.py --proto /path/to/a2a.proto
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

PROTO_URL = (
    "https://raw.githubusercontent.com/a2aproject/A2A/main/specification/a2a.proto"
)


def load(source: str | None) -> str:
    if source:
        return open(source, encoding="utf-8").read()
    with urllib.request.urlopen(PROTO_URL, timeout=30) as r:
        return r.read().decode("utf-8")


def task_states(proto: str) -> list[str]:
    m = re.search(r"^enum TaskState \{(.*?)^\}", proto, re.M | re.S)
    return re.findall(r"(TASK_STATE_[A-Z_]+)\s*=", m.group(1) if m else "")


def terminal(proto: str) -> set[str]:
    """Which states the proto's own comments call terminal or interrupted."""
    m = re.search(r"^enum TaskState \{(.*?)^\}", proto, re.M | re.S)
    body = m.group(1) if m else ""
    out = set()
    for chunk in body.split(";"):
        name = re.search(r"(TASK_STATE_[A-Z_]+)\s*=", chunk)
        if name and ("terminal state" in chunk or "interrupted state" in chunk):
            out.add(name.group(1))
    return out


# ---- the harness's answers, read off real event logs ---------------------


def harness_situations() -> list[tuple[str, str, str]]:
    """(situation, how the harness knows, what it can still answer)."""
    from myharness.events.types import JOB_FINISH

    def ends_finished(path: Path) -> bool:
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        return bool(lines) and json.loads(lines[-1])["t"] == JOB_FINISH

    found = sorted(Path("jobs-scratch").glob("*/jobs/*/events.jsonl"))
    complete = [p for p in found if ends_finished(p)]
    partial = [p for p in found if not ends_finished(p)]

    return [
        ("no such analysis",
         "the id is in neither the manager nor the store",
         "nothing -- and that is the correct answer"),
        ("running in this process",
         "JobManager.get returns a handle whose state is RUNNING",
         "progress, revision, pending questions"),
        ("finished, this process or another",
         f"the event log ends with job.finish "
         f"({len(complete)} of {len(found)} logs in jobs-scratch do)",
         "the summary and the section price list, in full"),
        ("abandoned in flight",
         f"no handle, and the event log does NOT end with job.finish "
         f"({len(partial)} of {len(found)})",
         "whatever was written before the process went away"),
    ]


MAPPING = [
    ("running in this process", "TASK_STATE_WORKING",
     "and TASK_STATE_INPUT_REQUIRED while analysis_answer is outstanding, which "
     "is the state A2A already has for exactly this"),
    ("finished, this process or another", "TASK_STATE_COMPLETED",
     "a terminal state, and the harness can still answer in full -- reading a "
     "finished job never needed the process that ran it"),
    ("no such analysis", "(not a state at all)",
     "there is no task to carry a state. This is a not-found on the transport, "
     "and the proto carries no error enum -- see the header note"),
    ("abandoned in flight", "(nothing fits)",
     "not WORKING, because nothing is processing it. Not COMPLETED, because it "
     "did not finish. Not FAILED or REJECTED, because the agent never decided "
     "anything -- the process went away. Not CANCELED, because nobody asked"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", help="local a2a.proto instead of fetching")
    args = ap.parse_args()

    proto = load(args.proto)
    states = task_states(proto)
    ends = terminal(proto)
    print(f"a2a.proto: TaskState has {len(states)} values, "
          f"{len(ends)} of them terminal or interrupted\n")
    for state in states:
        print(f"      {state}{'  (terminal/interrupted)' if state in ends else ''}")

    print("\n--- what the harness can be asked " + "-" * 38)
    for situation, how, answerable in harness_situations():
        print(f"  {situation}")
        print(f"      knows by:   {how}")
        print(f"      can answer: {answerable}")

    print("\n--- mapped onto A2A " + "-" * 52)
    homeless = []
    for situation, state, why in MAPPING:
        print(f"  {situation:36} -> {state}")
        print(f"      {why}")
        if state.startswith("("):
            homeless.append(situation)

    print("\n" + "-" * 72)
    print("D5 said 「存在但不在此程序執行中」 has no slot. Mapped properly it")
    print("splits, and only one half is homeless:")
    print("  finished elsewhere  -> COMPLETED. The event log says so, and the")
    print("                         harness answers from disk. No gap.")
    print("  abandoned in flight -> nothing fits. A task that stopped being")
    print("                         worked on without reaching a conclusion has")
    print("                         no vocabulary in TaskState.")
    print()
    print("The honest encoding is TASK_STATE_FAILED with a status message that")
    print("says the process went away and the partial result is still readable --")
    print("wrong in kind but terminal, which is what a client needs to stop")
    print("waiting. Calling it WORKING would be a lie a client cannot detect:")
    print("it would wait forever for a stream nothing will ever write to.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
