"""Lane type vs lane instance: tools are code, datasets are not."""

from __future__ import annotations

from pathlib import Path

import pytest

from myharness.backends.profile import ModelTier
from myharness.lanes.types import (
    LaneConfigError,
    LaneInstance,
    LaneRegistry,
    LaneType,
    UnknownLaneType,
)


@pytest.fixture
def charter(tmp_path: Path) -> Path:
    path = tmp_path / "analyst.md"
    path.write_text("你是一個表格資料分析 lane worker。\n", encoding="utf-8")
    return path


@pytest.fixture
def lane_type(charter: Path) -> LaneType:
    return LaneType(
        name="tabular-analyst",
        charter_path=charter,
        tools=("read_note", "write_finding"),
        model_tier=ModelTier.MID,
        backend="anthropic",
        description="表格/交易資料分析",
    )


@pytest.fixture
def registry(lane_type: LaneType) -> LaneRegistry:
    return LaneRegistry(lane_type)


def test_two_instances_of_one_type_have_disjoint_state(registry: LaneRegistry):
    """Scenario: 同型別的兩個 instance 各持有獨立 state"""
    a = registry.create("txn-2024", "tabular-analyst", scope="2024 交易明細")
    b = registry.create("txn-2023", "tabular-analyst", scope="2023 對照組")

    assert a.type is b.type
    assert a.namespace != b.namespace
    assert a.state_name != b.state_name
    assert a.finding_name("003") != b.finding_name("003")
    assert a.finding_name("003").startswith(a.namespace)


def test_unknown_lane_type_lists_what_is_registered(registry: LaneRegistry):
    """Scenario: 拒絕未註冊的 lane type"""
    with pytest.raises(UnknownLaneType) as exc:
        registry.create("x", "doc-extractor")
    assert "tabular-analyst" in str(exc.value)
    assert exc.value.available == ["tabular-analyst"]


def test_duplicate_instance_id_is_refused(registry: LaneRegistry):
    registry.create("txn-2024", "tabular-analyst")
    with pytest.raises(LaneConfigError):
        registry.create("txn-2024", "tabular-analyst")


def test_unknown_instance_lists_what_exists(registry: LaneRegistry):
    registry.create("txn-2024", "tabular-analyst")
    with pytest.raises(LaneConfigError) as exc:
        registry.get("nope")
    assert "txn-2024" in str(exc.value)


def test_charter_is_read_from_file_and_hashed(lane_type: LaneType, charter: Path):
    assert "lane worker" in lane_type.charter()
    before = lane_type.charter_hash()
    charter.write_text("修改後的 charter\n", encoding="utf-8")
    assert lane_type.charter_hash() != before, "hash must track the file, not a snapshot"


def test_missing_charter_fails_with_a_useful_message(tmp_path: Path):
    lane_type = LaneType(name="broken", charter_path=tmp_path / "gone.md")
    with pytest.raises(LaneConfigError) as exc:
        lane_type.charter()
    assert "broken" in str(exc.value) and "gone.md" in str(exc.value)


def test_model_resolves_through_the_backend(charter: Path):
    """Scenario: 同一別名在不同後端解析為不同模型"""
    direct = LaneType(name="a", charter_path=charter, backend="anthropic",
                      model_tier=ModelTier.STRONG)
    via_or = LaneType(name="b", charter_path=charter, backend="openrouter",
                      model_tier=ModelTier.STRONG)
    assert direct.model() == "opus"
    assert via_or.model().startswith("nvidia/")
    assert direct.model() != via_or.model()


def test_type_catalogue_is_what_the_orchestrator_sees(registry: LaneRegistry):
    catalogue = registry.describe_types()
    assert "tabular-analyst" in catalogue and "表格" in catalogue


def test_instances_are_listed_in_stable_order(registry: LaneRegistry):
    registry.create("b-lane", "tabular-analyst")
    registry.create("a-lane", "tabular-analyst")
    assert [i.id for i in registry.instances()] == ["a-lane", "b-lane"]
