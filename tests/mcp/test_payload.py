"""Bounds on what crosses back to the client.

This is the outermost layer of DESIGN's recursion table and was the only one
without bounds. The tests that matter are the ones showing a payload does *not*
grow with the job: a client's context must not be a function of how long the
analysis ran.
"""

from __future__ import annotations

import json

from myharness.artifacts.tokens import estimate_tokens
from myharness.events.types import Event
from myharness.mcp.payload import (
    MAX_EVENT_CHARS,
    MAX_FINDINGS,
    MAX_PENDING_QUESTIONS,
    MAX_PROGRESS_CHARS,
    MAX_QUESTION_CHARS,
    MAX_RECENT_EVENTS,
    MAX_SECTION_TOKENS,
    MAX_SUMMARY_CHARS,
    bound_section,
    build_progress,
    build_result,
    clip,
    describe_event,
)


def event(t: str, **data) -> Event:
    return Event(t=t, seq=0, ts="2026-01-01T00:00:00Z", job_id="j", data=data)


def status(**over) -> dict:
    base = {
        "phase": "running", "dispatches": 3, "running": 1, "spent_usd": 0.12,
        "elapsed_s": 42.0, "pending_questions": [], "report": None,
    }
    base.update(over)
    return base


class TestClip:
    def test_short_text_is_untouched(self):
        assert clip("hello", 20) == "hello"

    def test_long_text_is_marked(self):
        out = clip("x" * 100, 10)
        assert len(out) == 10 and out.endswith("…")

    def test_none_and_blank_are_empty(self):
        assert clip("", 5) == "" and clip(None, 5) == ""


class TestProgressIsBounded:
    def test_recent_events_do_not_grow_with_the_job(self):
        """The claim this layer makes: a client's cost is not a function of how
        long the analysis ran."""
        few = build_progress(
            job_id="j", state="running", revision=1, status=status(),
            recent_events=[event("dispatch.start", id=f"d{i}") for i in range(3)],
        )
        many = build_progress(
            job_id="j", state="running", revision=1, status=status(),
            recent_events=[event("dispatch.start", id=f"d{i}") for i in range(5000)],
        )
        assert len(many.recent) == MAX_RECENT_EVENTS
        assert len(json.dumps(many.to_dict())) < 2 * len(json.dumps(few.to_dict()))

    def test_the_most_recent_events_are_the_ones_kept(self):
        p = build_progress(
            job_id="j", state="running", revision=1, status=status(),
            recent_events=[event("dispatch.start", id=f"d{i}") for i in range(20)],
        )
        assert "d19" in p.recent[-1]

    def test_one_enormous_event_cannot_flood_the_payload(self):
        p = build_progress(
            job_id="j", state="running", revision=1, status=status(),
            recent_events=[event("dispatch.start", id="d1", task="x" * 100_000)],
        )
        assert all(len(line) <= MAX_EVENT_CHARS for line in p.recent)

    def test_a_whole_progress_payload_stays_small(self):
        p = build_progress(
            job_id="j", state="running", revision=9,
            status=status(pending_questions=[
                {"id": f"q{i}", "text": "為什麼？" * 500, "kind": "clarify"}
                for i in range(50)
            ]),
            recent_events=[event("dispatch.end", id=f"d{i}", headline="很長" * 500)
                           for i in range(50)],
        )
        assert len(json.dumps(p.to_dict(), ensure_ascii=False)) < MAX_PROGRESS_CHARS

    def test_questions_are_capped_in_count_and_length(self):
        """MAX_PENDING_QUESTIONS is a ceiling, not a quota: the aggregate bound
        can trim further, and five 400-character questions already exceed it."""
        p = build_progress(
            job_id="j", state="running", revision=1,
            status=status(pending_questions=[
                {"id": f"q{i}", "text": "y" * 5000, "kind": "clarify"}
                for i in range(20)
            ]),
        )
        assert 1 <= len(p.questions) <= MAX_PENDING_QUESTIONS
        assert all(len(q["text"]) <= MAX_QUESTION_CHARS for q in p.questions)

    def test_short_questions_are_all_kept_up_to_the_ceiling(self):
        p = build_progress(
            job_id="j", state="running", revision=1,
            status=status(pending_questions=[
                {"id": f"q{i}", "text": "需要 2023 年的資料嗎？", "kind": "clarify"}
                for i in range(20)
            ]),
        )
        assert len(p.questions) == MAX_PENDING_QUESTIONS

    def test_question_ids_survive_clipping(self):
        """A clipped question is still answerable -- the id is what matters."""
        p = build_progress(
            job_id="j", state="running", revision=1,
            status=status(pending_questions=[{"id": "q7", "text": "z" * 9000}]),
        )
        assert p.questions[0]["id"] == "q7"

    def test_revision_is_carried_so_the_client_can_pass_it_back(self):
        p = build_progress(job_id="j", state="running", revision=17, status=status())
        assert p.to_dict()["revision"] == 17

    def test_report_ready_reflects_the_artifact(self):
        assert not build_progress(
            job_id="j", state="running", revision=1, status=status()
        ).report_ready
        assert build_progress(
            job_id="j", state="finished", revision=1,
            status=status(report="j/note/report"),
        ).report_ready


class TestDescribeEvent:
    def test_unknown_kinds_render_as_their_name(self):
        """events/types.py's additive rule: readers tolerate unknown kinds."""
        assert describe_event(event("something.new", x=1)) == "something.new"

    def test_a_dispatch_end_names_the_outcome(self):
        line = describe_event(event("dispatch.end", id="d1", status="ok",
                                    headline="765 accounts"))
        assert "d1" in line and "ok" in line and "765" in line

    def test_a_malformed_event_does_not_break_the_poll(self):
        assert describe_event(object()) == "?"


class TestResultIsAMenu:
    def test_sections_are_priced_and_the_body_is_not_included(self):
        payload = build_result({
            "executive_summary": "s",
            "sections": [{"id": "a", "title": "A", "est_tokens": 900},
                         {"id": "b", "title": "B", "est_tokens": 100}],
        }).to_dict()
        assert payload["total_section_tokens"] == 1000
        assert "analysis_drill" in payload["hint"]
        assert not any("text" in s or "body" in s for s in payload["sections"])

    def test_the_summary_is_bounded(self):
        payload = build_result({"executive_summary": "長" * 50_000}).to_dict()
        assert len(payload["executive_summary"]) <= MAX_SUMMARY_CHARS

    def test_findings_are_capped_in_count_and_length(self):
        payload = build_result({
            "executive_summary": "s",
            "key_findings": ["f" * 4000 for _ in range(40)],
        }).to_dict()
        assert len(payload["key_findings"]) == MAX_FINDINGS
        assert all(len(f) <= 201 for f in payload["key_findings"])

    def test_an_empty_delivery_does_not_crash(self):
        assert build_result({}).to_dict()["total_section_tokens"] == 0


class TestBoundSection:
    def test_a_normal_section_passes_through_whole(self):
        text = "## 結論\n共 765 個帳戶。"
        out, cut = bound_section(text)
        assert out == text and not cut

    def test_an_oversized_section_is_cut_and_says_so(self):
        out, cut = bound_section("字" * 200_000, max_tokens=100)
        assert cut and estimate_tokens(out) <= 200
        assert "超出單次上限" in out

    def test_the_estimate_is_respected_for_cjk(self):
        """CJK costs ~1.5 tokens a character, so a character count would be
        wrong by a factor of six here."""
        out, cut = bound_section("交" * 10_000, max_tokens=300)
        assert cut and estimate_tokens(out) <= 300

    def test_the_default_bound_is_a_drill_budget_not_a_page_size(self):
        assert 5_000 <= MAX_SECTION_TOKENS <= 40_000


class TestTheAggregateBoundIsEnforced:
    """Per-item caps do not bound the total.

    Eight 120-character event lines plus five 400-character questions is
    already ~3,000 characters -- the same arithmetic that made the query
    result need a character gate on top of a row gate.
    """

    def _pathological(self, **over):
        base = dict(
            job_id="j", state="running", revision=9,
            status=status(pending_questions=[
                {"id": f"q{i}", "text": "為什麼要這樣做？" * 200, "kind": "clarify"}
                for i in range(50)
            ]),
            recent_events=[event("dispatch.end", id=f"d{i}", headline="很長的標題" * 200)
                           for i in range(50)],
        )
        base.update(over)
        return build_progress(**base)

    def size(self, p) -> int:
        return len(json.dumps(p.to_dict(), ensure_ascii=False))

    def test_the_worst_case_still_fits(self):
        assert self.size(self._pathological()) <= MAX_PROGRESS_CHARS

    def test_events_are_shed_before_questions(self):
        """An unanswered question blocks the job; a missed log line does not."""
        p = self._pathological()
        assert p.questions, "questions were dropped before events"
        assert len(p.recent) < MAX_RECENT_EVENTS

    def test_at_least_one_question_survives(self):
        p = self._pathological()
        assert len(p.questions) >= 1

    def test_the_last_question_is_clipped_rather_than_dropped(self):
        p = build_progress(
            job_id="j", state="running", revision=1,
            status=status(pending_questions=[{"id": "q1", "text": "長" * 5000}]),
        )
        assert len(p.questions) == 1
        assert self.size(p) <= MAX_PROGRESS_CHARS
        assert p.questions[0]["id"] == "q1", "the id must survive to be answerable"

    def test_counters_are_never_shed(self):
        p = self._pathological()
        body = p.to_dict()
        for key in ("job_id", "state", "revision", "dispatches", "spent_usd"):
            assert key in body

    def test_a_typical_payload_is_not_over_trimmed(self):
        p = build_progress(
            job_id="j", state="running", revision=4,
            status=status(pending_questions=[{"id": "q1", "text": "需要 2023 年資料嗎？"}]),
            recent_events=[event("dispatch.end", id=f"d{i}", status="ok",
                                 headline="找到 765 個帳戶") for i in range(5)],
        )
        assert len(p.recent) == 5, "trimming fired on a payload that fits"
        assert len(p.questions) == 1
