"""Spike #22: does `revision` survive a dropped A2A stream?

Change `expose-over-a2a` D4 says every streamed event carries the job's
`revision`, and a reconnecting client sends back the last one it saw. That is
the existing long-poll mechanism, not a new one -- `JobHandle.wait_for_change`
already takes `since=` for exactly this reason:

    ``since`` is the revision the caller last saw. If the job has moved on
    already, this returns immediately rather than waiting for the *next*
    change, which would hide the one that just happened.

Two questions, and they fail in different places:

1. Does A2A give a streamed event anywhere to *put* a cursor, and does its
   resubscribe carry one back?  -- answered against the canonical proto.
2. Does the harness half actually hold when a client disconnects mid-job?
   -- answered by dropping a subscriber and reconnecting with a stale revision.

Task 1.4 is the other half of the same mechanism: an event that is not a
substantive change must not wake a subscriber at all, or the stream becomes a
per-turn heartbeat. That is `NOT_NEWS`, and it is checked here too.

(Numbered 22, not 14: spikes/spike14_turn_overhead.py already exists. The
change's tasks.md was written when 14 was free.)

Run: python spikes/spike22_a2a_stream_cursor.py
     python spikes/spike22_a2a_stream_cursor.py --proto /path/to/a2a.proto
"""

import argparse
import asyncio
import re
import sys
import urllib.request

PROTO_URL = (
    "https://raw.githubusercontent.com/a2aproject/A2A/main/specification/a2a.proto"
)


def load(source: str | None) -> str:
    if source:
        return open(source, encoding="utf-8").read()
    with urllib.request.urlopen(PROTO_URL, timeout=30) as r:
        return r.read().decode("utf-8")


def block(proto: str, name: str) -> str:
    m = re.search(rf"^(?:message|enum|service) {re.escape(name)} \{{(.*?)^\}}",
                  proto, re.M | re.S)
    return m.group(1) if m else ""


def has_field(body: str, field: str) -> bool:
    return re.search(rf"\b{re.escape(field)}\s*=\s*\d+", body) is not None


# ---- what the protocol provides -----------------------------------------


def q_status_event_has_metadata(proto):
    body = block(proto, "TaskStatusUpdateEvent")
    ok = has_field(body, "metadata")
    return ok, ("TaskStatusUpdateEvent.metadata is where a revision rides. It is "
                "free-form, which is fine here: the cursor is ours to interpret, "
                "unlike the price-list mark, which a client must understand")


def q_artifact_event_has_metadata(proto):
    body = block(proto, "TaskArtifactUpdateEvent")
    ok = has_field(body, "metadata")
    return ok, ("TaskArtifactUpdateEvent.metadata too, so both kinds of update "
                "carry the same cursor and a client never has to track two")


def q_resubscribe_exists(proto):
    body = block(proto, "A2AService")
    ok = "SubscribeToTask(" in body
    return ok, ("A2AService.SubscribeToTask returns a stream, so reconnecting is "
                "a protocol operation rather than starting a second task")


def q_resubscribe_carries_a_cursor(proto):
    body = block(proto, "SubscribeToTaskRequest")
    if not body:
        # A missing message would make the check below pass for the wrong
        # reason, which is how the first version of this spike read.
        return False, "SubscribeToTaskRequest is not in this proto -- name moved"
    fields = sorted(set(re.findall(r"\b(\w+)\s*=\s*\d+", body)))
    cursor = {"since", "cursor", "from_revision", "last_event_id"} & set(fields)
    # Inverted on purpose: passing would mean the protocol resumes for us.
    return (not cursor), (
        f"SubscribeToTaskRequest carries {fields} and nothing else -- no cursor. "
        "Resubscribing opens a NEW stream from now, so anything that happened "
        "while the client was gone is not replayed. The cursor has to be ours, "
        "and the client has to send it back somewhere else")


def q_history_is_reachable(proto):
    task = block(proto, "Task")
    req = block(proto, "GetTaskRequest")
    ok = has_field(task, "history") and has_field(req, "history_length")
    return ok, ("Task.history plus GetTaskRequest.history_length: after "
                "reconnecting, a client that finds itself behind can fetch what "
                "it missed with a normal get. That is the recovery path -- the "
                "stream does not replay, the task does")


CHECKS = [
    ("a streamed status event can carry a cursor", q_status_event_has_metadata),
    ("so can an artifact update", q_artifact_event_has_metadata),
    ("resubscribe is a protocol operation", q_resubscribe_exists),
    ("resubscribe does NOT resume from a point", q_resubscribe_carries_a_cursor),
    ("but the task's history is fetchable", q_history_is_reachable),
]


# ---- what the harness does ----------------------------------------------


async def reconnect_demo() -> list[str]:
    """Drop a subscriber mid-job, move the job on, reconnect with a stale cursor."""
    from myharness.mcp.manager import JobHandle

    notes: list[str] = []
    handle = JobHandle(job_id="j", runner=None, channel=None)  # type: ignore[arg-type]

    # A subscriber is attached and sees the job move twice.
    handle.notify()
    handle.notify()
    seen = handle.revision
    notes.append(f"subscriber disconnects having seen revision {seen}")

    # It is gone. The job carries on.
    handle.notify()
    handle.notify()
    handle.notify()
    notes.append(f"job moves on to revision {handle.revision} with nobody listening")

    # It comes back and says where it was. This must not block.
    returned = await asyncio.wait_for(
        handle.wait_for_change(timeout=5.0, since=seen), timeout=1.0
    )
    notes.append(f"reconnect with since={seen} returns {returned} immediately "
                 f"-- the client is told it is behind, not made to wait for a "
                 f"change that already happened")

    # A client that is current still waits, which is what makes it a long poll.
    current = handle.revision
    timed_out = await handle.wait_for_change(timeout=0.05, since=current)
    notes.append(f"reconnect with since={current} returns {timed_out} after the "
                 f"timeout -- up to date means keep waiting")
    return notes


def not_news_demo() -> list[str]:
    """The per-turn event must not wake a subscriber."""
    from myharness.events.types import CTX, DISPATCH_END
    from myharness.mcp.manager import MEANINGFUL, NOT_NEWS

    notes = [
        f"MEANINGFUL has {len(MEANINGFUL)} kinds; NOT_NEWS has {len(NOT_NEWS)}",
        f"ctx in MEANINGFUL: {CTX in MEANINGFUL} -- an orchestrator turn is not news",
        f"dispatch.end in MEANINGFUL: {DISPATCH_END in MEANINGFUL}",
    ]
    return notes


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", help="local a2a.proto instead of fetching")
    args = ap.parse_args()

    proto = load(args.proto)
    print(f"a2a.proto: {len(proto.splitlines())} lines\n")
    print("--- what A2A provides " + "-" * 50)
    results = []
    for label, fn in CHECKS:
        ok, note = fn(proto)
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        print(f"      {note}\n")

    print("--- what the harness already does " + "-" * 38)
    for note in await reconnect_demo():
        print(f"      {note}")
    print()
    for note in not_news_demo():
        print(f"      {note}")

    print()
    print("-" * 72)
    print("Spec 「重連時不漏事件」 is satisfiable, but NOT by resubscribing alone:")
    print("  the stream carries `revision` in metadata; a reconnecting client")
    print("  sends its last one back and is told immediately that it is behind;")
    print("  what it missed comes from Task.history, not from the new stream.")
    print("Spec 「逐輪的內部事件不觸發推送」 is already true: ctx is not MEANINGFUL,")
    print("  so it bumps no revision and wakes no subscriber.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
