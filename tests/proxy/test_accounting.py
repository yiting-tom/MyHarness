"""Proxy spend and the caveats that depend on routing.

events/query.py was written with the proxy in mind long before it existed --
_bucket sends proxy.route spend to "(proxy)", and derive_caveats reads
proxy.route to decide whether incoming data went unused. Those paths have never
had a real proxy.route event through them until now.
"""

from __future__ import annotations

from myharness.events.query import cost_by_lane, derive_caveats, tokens_by_lane
from myharness.events.types import (
    DISPATCH_END,
    DISPATCH_START,
    INGRESS,
    PROXY_ROUTE,
    Event,
)

PROXY = "(proxy)"


def ev(t: str, seq: int = 0, **data) -> Event:
    return Event(t=t, seq=seq, ts="2026-01-01T00:00:00Z", job_id="j", data=data)


class TestSpendIsAttributedToTheProxy:
    def test_routing_cost_does_not_land_on_the_routed_lane(self):
        """The lane did no work; it was merely named."""
        events = [
            ev(PROXY_ROUTE, 1, payload="j/blob/raw/a", lane="txn", usd=0.0004),
            ev(DISPATCH_END, 2, id="d1", lane="txn", usd=0.20),
        ]
        costs = cost_by_lane(events)
        assert costs[PROXY] == 0.0004
        assert costs["txn"] == 0.20

    def test_tokens_are_bucketed_the_same_way(self):
        events = [
            ev(PROXY_ROUTE, 1, lane="txn", tokens={"in": 420, "out": 26}),
            ev(DISPATCH_END, 2, id="d1", lane="txn", tokens={"in": 9000, "out": 800}),
        ]
        by_lane = tokens_by_lane(events)
        assert by_lane[PROXY] == {"in": 420, "out": 26}
        assert by_lane["txn"] == {"in": 9000, "out": 800}

    def test_an_unrouted_attempt_still_costs_something(self):
        """A classifier that said "no idea" was still paid for."""
        events = [ev(PROXY_ROUTE, 1, payload="j/blob/raw/a", lane=None, usd=0.0003)]
        assert cost_by_lane(events)[PROXY] == 0.0003

    def test_several_payloads_accumulate(self):
        events = [
            ev(PROXY_ROUTE, 1, lane="txn", usd=0.0004),
            ev(PROXY_ROUTE, 2, lane="kyc", usd=0.0004),
        ]
        assert cost_by_lane(events)[PROXY] == 0.0008


class TestUnprocessedPayloadCaveat:
    def test_data_that_arrived_and_was_never_used_is_declared(self):
        events = [ev(INGRESS, 1, payload="j/blob/raw/a", bytes=1472)]
        kinds = [c.kind for c in derive_caveats(events)]
        assert "unprocessed_payload" in kinds

    def test_routing_alone_clears_the_caveat(self):
        """Routing is the harness saying "this was looked at and assigned"."""
        events = [
            ev(INGRESS, 1, payload="j/blob/raw/a", bytes=1472),
            ev(PROXY_ROUTE, 2, payload="j/blob/raw/a", lane="txn"),
        ]
        assert not [c for c in derive_caveats(events)
                    if c.kind == "unprocessed_payload"]

    def test_an_unrouted_payload_still_raises_the_caveat(self):
        """A proxy.route with no lane means nobody claimed it."""
        events = [
            ev(INGRESS, 1, payload="j/blob/raw/a", bytes=1472),
            ev(PROXY_ROUTE, 2, payload="j/blob/raw/a", lane=None,
               unrouted="no_match"),
        ]
        assert [c for c in derive_caveats(events) if c.kind == "unprocessed_payload"]

    def test_being_dispatched_also_clears_it(self):
        events = [
            ev(INGRESS, 1, payload="j/blob/raw/a", bytes=1472),
            ev(DISPATCH_START, 2, id="d1", lane="txn", inputs=["j/blob/raw/a"]),
        ]
        assert not [c for c in derive_caveats(events)
                    if c.kind == "unprocessed_payload"]

    def test_the_caveat_names_the_payload(self):
        events = [ev(INGRESS, 1, payload="j/blob/raw/orphan", bytes=99)]
        caveat = next(c for c in derive_caveats(events)
                      if c.kind == "unprocessed_payload")
        assert "orphan" in caveat.detail
        assert caveat.context["bytes"] == 99


# --- the data-flow projection ----------------------------------------------


class TestRoutingInTheDataFlow:
    """A suggestion is neither an authorisation nor an action, so it gets its
    own edge kind. The point of having it is being able to ask whether the
    orchestrator acted on it."""

    def _flow(self, events):
        from myharness.dataflow import build_dataflow

        return build_dataflow(events, (), job_id="j")

    def test_routing_becomes_a_suggested_edge(self):
        from myharness.dataflow import EdgeKind

        flow = self._flow([
            ev(INGRESS, 1, payload="j/blob/raw/a"),
            ev(PROXY_ROUTE, 2, payload="j/blob/raw/a", lane="txn"),
        ])
        edges = [e for e in flow.edges if e.kind is EdgeKind.SUGGESTED]
        assert len(edges) == 1
        assert edges[0].src == "j/blob/raw/a" and edges[0].dst == "lane:txn"

    def test_an_unrouted_payload_makes_no_edge(self):
        from myharness.dataflow import EdgeKind

        flow = self._flow([
            ev(INGRESS, 1, payload="j/blob/raw/a"),
            ev(PROXY_ROUTE, 2, payload="j/blob/raw/a", lane=None, unrouted="failed"),
        ])
        assert not [e for e in flow.edges if e.kind is EdgeKind.SUGGESTED]

    def test_a_suggestion_is_not_a_grant(self):
        """The distinction the whole design rests on, visible in the graph."""
        from myharness.dataflow import EdgeKind

        flow = self._flow([
            ev(INGRESS, 1, payload="j/blob/raw/a"),
            ev(PROXY_ROUTE, 2, payload="j/blob/raw/a", lane="txn"),
        ])
        assert not [e for e in flow.edges if e.kind is EdgeKind.GRANTED]

    def test_acting_on_the_suggestion_raises_no_anomaly(self):
        from myharness.dataflow import AnomalyKind, detect

        flow = self._flow([
            ev(INGRESS, 1, payload="j/blob/raw/a"),
            ev(PROXY_ROUTE, 2, payload="j/blob/raw/a", lane="txn"),
            ev(DISPATCH_START, 3, id="d1", lane="txn", inputs=["j/blob/raw/a"]),
            ev(DISPATCH_END, 4, id="d1", lane="txn", status="ok"),
        ])
        assert not [a for a in detect(flow)
                    if a.kind is AnomalyKind.SUGGESTION_IGNORED]

    def test_an_ignored_suggestion_is_surfaced(self):
        """A dropped payload and a deliberate override look identical in the
        log, so the generous reading is not assumed."""
        from myharness.dataflow import AnomalyKind, Severity, detect

        flow = self._flow([
            ev(INGRESS, 1, payload="j/blob/raw/a"),
            ev(PROXY_ROUTE, 2, payload="j/blob/raw/a", lane="txn"),
            ev(DISPATCH_START, 3, id="d1", lane="other", inputs=[]),
        ])
        found = [a for a in detect(flow) if a.kind is AnomalyKind.SUGGESTION_IGNORED]
        assert len(found) == 1
        assert found[0].severity is Severity.WARNING
        assert "沒有任何 lane 取得它" in found[0].detail

    def test_an_override_names_where_it_went_instead(self):
        from myharness.dataflow import AnomalyKind, detect

        flow = self._flow([
            ev(INGRESS, 1, payload="j/blob/raw/a"),
            ev(PROXY_ROUTE, 2, payload="j/blob/raw/a", lane="txn"),
            ev(DISPATCH_START, 3, id="d1", lane="kyc", inputs=["j/blob/raw/a"]),
        ])
        found = next(a for a in detect(flow)
                     if a.kind is AnomalyKind.SUGGESTION_IGNORED)
        assert "kyc" in found.detail
        assert found.context["granted_to"] == ["kyc"]

    def test_it_is_a_warning_not_a_critical(self):
        """The orchestrator is entitled to overrule the classifier."""
        from myharness.dataflow import AnomalyKind, critical, detect

        flow = self._flow([
            ev(INGRESS, 1, payload="j/blob/raw/a"),
            ev(PROXY_ROUTE, 2, payload="j/blob/raw/a", lane="txn"),
        ])
        assert [a for a in detect(flow) if a.kind is AnomalyKind.SUGGESTION_IGNORED]
        assert not critical(detect(flow))
