from claude_agent_sdk import AssistantMessage, TextBlock

from myharness.lanes.stream import Accumulated, RequestMeter, request_footprint


def msg(mid, inp, cached, out):
    return AssistantMessage(
        content=[TextBlock(text="x")], model="m", message_id=mid,
        usage={"input_tokens": inp, "cache_read_input_tokens": cached,
               "output_tokens": out},
    )


def test_one_response_split_into_blocks_is_one_request():
    meter = RequestMeter()
    for m in (msg("a", 100, 900, 5), msg("a", 100, 900, 5), msg("b", 50, 1_500, 7)):
        meter.add(m)
    assert meter.to_event() == {"in": 2_550, "out": 12, "peak": 1_550}


def test_a_backend_that_reports_nothing_falls_back_to_the_estimate():
    """LiteLLM streams zeroed usage; a peak of 0 would read as 'no context'."""
    acc = Accumulated(peak_estimate=5_000)
    acc.meter.add(msg("a", 0, 0, 0))
    assert request_footprint(acc) == {"in": 0, "out": 0, "peak": 5_000, "estimated": True}
