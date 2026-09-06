"""Spike #13: can A2A say "this is a price list, not the content"?

The harness's outermost promise is that `analysis_result` returns a summary and
a per-section token estimate -- NOT the report. A2A's artifact model assumes the
opposite: the task completes, and the artifact is the deliverable.

Change `expose-over-a2a` D7 makes this the gate for the other 25 tasks. If the
protocol has no natural way to mark an artifact as a table of contents, then
option B (dual mode) degrades into stuffing a private convention into a
free-form field -- and option A (price list only) is the honest answer instead.

Checked against the canonical proto, not against memory: a2a.proto is the source
the JSON schema is generated from, so a field that is not in it does not exist.
Re-runnable on purpose -- it fails if A2A moves the fields this design stands on.

Run: python spikes/spike13_a2a_artifact_semantics.py
     python spikes/spike13_a2a_artifact_semantics.py --proto /path/to/a2a.proto
"""

import argparse
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
    """One `message X { ... }` body. Braces do not nest in this proto."""
    m = re.search(rf"^(?:message|enum) {re.escape(name)} \{{(.*?)^\}}",
                  proto, re.M | re.S)
    return m.group(1) if m else ""


def has_field(body: str, field: str) -> bool:
    return re.search(rf"\b{re.escape(field)}\s*=\s*\d+", body) is not None


# ---- the questions ------------------------------------------------------
#
# Each is (question, verdict_fn) -> (bool, note). The note is the finding;
# a bare pass/fail would lose the reason, and the reason is the whole point.

def q_artifact_metadata(proto):
    body = block(proto, "Artifact")
    ok = has_field(body, "metadata")
    return ok, ("Artifact.metadata is a google.protobuf.Struct -- a PLACE to put "
                "it, but free-form: nothing in the protocol gives it meaning")


def q_artifact_extensions(proto):
    body = block(proto, "Artifact")
    ok = has_field(body, "extensions")
    return ok, ("Artifact.extensions is a repeated URI: 'extensions that are "
                "present or contributed to this Artifact' -- semantics with an "
                "identity, not a private key in a bag")


def q_declared_extension(proto):
    caps = block(proto, "AgentCapabilities")
    ext = block(proto, "AgentExtension")
    ok = has_field(caps, "extensions") and has_field(ext, "required")
    return ok, ("AgentExtension.required: 'If true, the client must understand "
                "and comply' -- a client that does not know the price list "
                "convention is told so, instead of silently misreading it")


def q_output_modes_are_semantic(proto):
    card = block(proto, "AgentCard")
    skill = block(proto, "AgentSkill")
    # Deliberately inverted: passing here would mean output_modes CAN carry
    # "price list vs full text". The proto says they are media types.
    media = "media types" in card.lower() or "media types" in skill.lower()
    return (not media), ("default_output_modes / AgentSkill.output_modes are "
                         "MEDIA TYPES. Declaring 'price list' as an output mode "
                         "is a misuse of the field, not a use of it")


def q_two_skills(proto):
    body = block(proto, "AgentSkill")
    ok = all(has_field(body, f) for f in ("id", "name", "description"))
    return ok, ("AgentSkill id/name/description carries the distinction "
                "instead: two skills map onto analysis_result and "
                "analysis_drill, which already exist")


def q_state_for_not_running_here(proto):
    body = block(proto, "TaskState")
    states = set(re.findall(r"(TASK_STATE_[A-Z_]+)\s*=", body))
    # D5: a job that exists on disk but whose asyncio.Task lives in a process
    # that is gone. Not failed, not cancelled, not still working.
    ok = any(s in states for s in ("TASK_STATE_DETACHED", "TASK_STATE_ORPHANED"))
    return ok, (f"TaskState has {len(states)} values and none of them mean "
                "'exists but not running in this process'. D5's three-way "
                "distinction has no native slot -- feeds spike #15")


CHECKS = [
    ("GATE  artifact can be marked as a price list", q_artifact_extensions),
    ("      ... and the mark is declarable up front", q_declared_extension),
    ("      metadata alone would be a private convention", q_artifact_metadata),
    ("      output_modes are NOT a semantic selector", q_output_modes_are_semantic),
    ("      two skills carry the mode distinction", q_two_skills),
    ("      TaskState covers 'not running here'", q_state_for_not_running_here),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", help="local a2a.proto instead of fetching")
    args = ap.parse_args()

    proto = load(args.proto)
    print(f"a2a.proto: {len(proto.splitlines())} lines\n")

    results = []
    for label, fn in CHECKS:
        ok, note = fn(proto)
        results.append((label, ok, note))
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        print(f"      {note}\n")

    gate = results[0][1] and results[1][1]
    print("-" * 72)
    print(f"D7 gate: {'B (dual mode) is expressible' if gate else 'fall back to A'}")
    # Not an assertion suite: the FAILs above are findings, and two of them are
    # expected. Exit non-zero only if the gate itself moved.
    return 0 if gate else 1


if __name__ == "__main__":
    sys.exit(main())
