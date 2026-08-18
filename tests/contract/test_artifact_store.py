"""Contract suite for ArtifactStore.

Applies unmodified to every backend (spec: 後端可替換的儲存介面 / 同一組測試可驗證多個後端).
Each test names the spec scenario it covers.
"""

from __future__ import annotations

import pytest

from myharness.artifacts import errors
from myharness.artifacts.ids import KIND_BLOB, KIND_NOTE, ArtifactId, lane_namespace
from myharness.artifacts.store import ArtifactStore
from myharness.artifacts.types import GrantSet

from .conftest import JOB, StoreHarness

NOTE_TEXT = (
    "前言段落。\n\n"
    "## 方法論\n以 duckdb 掃描全表。\n\n"
    "## 異常交易明細\n夜間小額高頻，n=30412。\n"
)


@pytest.fixture
def anyjob(store: ArtifactStore):
    return store


async def _blob(store: ArtifactStore, name: str = "raw/txns-2024") -> ArtifactId:
    meta = await store.put_blob(
        JOB, name, data=b"ts,amt\n1,2\n", produced_by="user",
        schema={"columns": ["ts", "amt"]},
    )
    return meta.id


async def _note(store: ArtifactStore, name: str, text: str = NOTE_TEXT) -> ArtifactId:
    meta = await store.put_note(JOB, name, text, produced_by="lane:txn-2024")
    return meta.id


def _all(job: str = JOB) -> GrantSet:
    return GrantSet.unrestricted(job)


# --- Requirement: Blob 與 Note 的型別二分 --------------------------------


async def test_read_note_returns_text(store: ArtifactStore):
    """Scenario: 讀取 note 成功"""
    nid = await _note(store, "lanes/txn-2024/findings/001")
    assert await store.read_note(nid, grants=_all(), max_tokens=10_000) == NOTE_TEXT


async def test_read_note_on_blob_is_refused_with_access_hint(store: ArtifactStore):
    """Scenario: 拒絕把 blob 讀入 context"""
    bid = await _blob(store)
    with pytest.raises(errors.BlobNotReadable) as exc:
        await store.read_note(bid, grants=_all(), max_tokens=10_000)
    detail = exc.value.to_dict()
    assert detail["kind"] == KIND_BLOB
    assert detail["bytes"] > 0
    assert detail["schema"] == {"columns": ["ts", "amt"]}
    assert detail["suggested_access"], "must tell the caller how to reach the data"


async def test_blob_refusal_reads_no_content(harness: StoreHarness):
    """Scenario: 拒絕把 blob 讀入 context -- SHALL NOT 讀取任何內容位元組.

    With the content destroyed, only an index-driven refusal can still produce
    BlobNotReadable; a content-touching implementation would fail differently.
    """
    bid = await _blob(harness.store)
    harness.destroy_content(bid)
    with pytest.raises(errors.BlobNotReadable):
        await harness.store.read_note(bid, grants=_all(), max_tokens=10_000)


# --- Requirement: 讀取前的 token 預檢 ------------------------------------


async def test_oversized_note_is_refused_with_section_menu(store: ArtifactStore):
    """Scenario: 超出額度時在讀取前拒絕"""
    nid = await _note(store, "report")
    meta = await store.stat(nid, grants=_all())
    with pytest.raises(errors.TokenBudgetExceeded) as exc:
        await store.read_note(nid, grants=_all(), max_tokens=1)
    detail = exc.value.to_dict()
    assert detail["est_tokens"] == meta.est_tokens
    assert [s["id"] for s in detail["sections"]] == list(meta.section_ids)


async def test_budget_refusal_reads_no_content(harness: StoreHarness):
    """Scenario: 超出額度時在讀取前拒絕 -- SHALL NOT 從儲存後端讀取內容."""
    nid = await _note(harness.store, "report")
    harness.destroy_content(nid)
    with pytest.raises(errors.TokenBudgetExceeded):
        await harness.store.read_note(nid, grants=_all(), max_tokens=1)


async def test_section_read_passes_precheck(store: ArtifactStore):
    """Scenario: 分段讀取可通過預檢"""
    nid = await _note(store, "report")
    meta = await store.stat(nid, grants=_all())
    section = meta.sections[0]
    text = await store.read_note(
        nid, grants=_all(), max_tokens=section.est_tokens, section=section.id
    )
    assert text.startswith("## ")
    assert len(text) < len(NOTE_TEXT)


async def test_unknown_section_lists_available(store: ArtifactStore):
    nid = await _note(store, "report")
    with pytest.raises(errors.SectionNotFound) as exc:
        await store.read_note(nid, grants=_all(), max_tokens=10_000, section="nope")
    assert exc.value.to_dict()["sections"]


# --- Requirement: Capability-based 讀取授權 ------------------------------


def _lane_grants(lane: str, granted=()) -> GrantSet:
    return GrantSet.for_lane(JOB, lane_namespace(lane), granted)


async def test_lane_reads_own_namespace(store: ArtifactStore):
    """Scenario: 讀取自己 namespace 內的 artifact"""
    nid = await _note(store, "lanes/txn-2024/state")
    assert await store.read_note(nid, grants=_lane_grants("txn-2024"), max_tokens=10_000)


async def test_lane_reads_explicitly_granted_artifact(store: ArtifactStore):
    """Scenario: 讀取被明確授權的外部 artifact"""
    other = await _note(store, "lanes/kyc-docs/findings/001")
    grants = _lane_grants("txn-2024", [other])
    assert await store.read_note(other, grants=grants, max_tokens=10_000)


async def test_ungranted_cross_lane_read_is_refused(store: ArtifactStore):
    """Scenario: 拒絕未授權的跨 lane 讀取"""
    other = await _note(store, "lanes/kyc-docs/state")
    with pytest.raises(errors.NotGranted) as exc:
        await store.read_note(other, grants=_lane_grants("txn-2024"), max_tokens=10_000)
    detail = exc.value.to_dict()
    assert detail["artifact"] == str(other)
    assert "夜間" not in str(detail), "must not leak content"
    assert "est_tokens" not in detail, "must not leak metadata beyond existence"


async def test_authorization_is_checked_before_existence(store: ArtifactStore):
    """Scenario: 拒絕未授權的跨 lane 讀取 -- 不洩漏是否存在."""
    missing = ArtifactId(JOB, KIND_NOTE, "lanes/kyc-docs/never-written")
    with pytest.raises(errors.NotGranted):
        await store.read_note(missing, grants=_lane_grants("txn-2024"), max_tokens=10_000)


# --- Requirement: Blob 的本地路徑物化 -------------------------------------


async def test_localize_yields_usable_path(harness: StoreHarness):
    """Scenario: 本地後端零複製"""
    bid = await _blob(harness.store)
    async with harness.store.localize(bid, grants=_all()) as path:
        assert path.read_bytes() == b"ts,amt\n1,2\n"


async def test_localize_cleans_up_on_exception(harness: StoreHarness):
    """Scenario: 離開時清理暫存"""
    bid = await _blob(harness.store)
    seen = None
    with pytest.raises(RuntimeError):
        async with harness.store.localize(bid, grants=_all()) as path:
            seen = path
            raise RuntimeError("boom")
    # The blob itself survives; only per-materialisation scratch may disappear.
    async with harness.store.localize(bid, grants=_all()) as again:
        assert again.read_bytes()
    assert seen is not None


async def test_localize_respects_grants(store: ArtifactStore):
    bid = await _blob(store, "raw/other")
    with pytest.raises(errors.NotGranted):
        async with store.localize(bid, grants=_lane_grants("txn-2024")):
            pass


# --- Requirement: 全域唯一的 artifact id ---------------------------------


async def test_id_carries_job_scope(store: ArtifactStore):
    """Scenario: id 含 job 範圍"""
    bid = await _blob(store)
    assert str(bid) == "j7/blob/raw/txns-2024"


async def test_cross_job_access_is_refused(store: ArtifactStore):
    """Scenario: 拒絕跨 job 存取"""
    nid = await _note(store, "report")
    with pytest.raises(errors.NotGranted):
        await store.read_note(nid, grants=GrantSet.unrestricted("j8"), max_tokens=10_000)


# --- Requirement: 寫入時記錄索引中繼資料 ----------------------------------


async def test_note_metadata_recorded(store: ArtifactStore):
    """Scenario: 寫入 note 後可查得 est_tokens"""
    nid = await _note(store, "report")
    meta = await store.stat(nid, grants=_all())
    assert meta.est_tokens is not None and meta.est_tokens >= 0
    assert meta.produced_by == "lane:txn-2024"
    assert meta.kind == KIND_NOTE


async def test_blob_metadata_recorded(store: ArtifactStore):
    """Scenario: 寫入 blob 後記錄大小"""
    bid = await _blob(store)
    meta = await store.stat(bid, grants=_all())
    assert meta.kind == KIND_BLOB
    assert meta.bytes == len(b"ts,amt\n1,2\n")


async def test_est_tokens_does_not_underestimate_cjk(store: ArtifactStore):
    """Guards design.md D3: a flat chars/4 rule would underestimate CJK ~4x."""
    nid = await _note(store, "cjk", "## 標題\n" + "資" * 1000 + "\n")
    meta = await store.stat(nid, grants=_all())
    assert meta.est_tokens >= 1000


# --- compare-and-set (design.md D6 risk mitigation) ----------------------


async def test_compare_and_set_detects_lost_update(store: ArtifactStore):
    first = await store.put_note(JOB, "lanes/a/state", "v1", produced_by="w1")
    await store.compare_and_set_note(
        JOB, "lanes/a/state", "v2", produced_by="w2", expected_revision=first.revision
    )
    with pytest.raises(errors.RevisionConflict):
        await store.compare_and_set_note(
            JOB, "lanes/a/state", "v3", produced_by="w3",
            expected_revision=first.revision,
        )


async def test_compare_and_set_can_require_absence(store: ArtifactStore):
    await store.compare_and_set_note(
        JOB, "lanes/b/state", "v1", produced_by="w1", expected_revision=0
    )
    with pytest.raises(errors.RevisionConflict):
        await store.compare_and_set_note(
            JOB, "lanes/b/state", "again", produced_by="w1", expected_revision=0
        )


# --- listing --------------------------------------------------------------


async def test_list_filters_by_kind_and_namespace(store: ArtifactStore):
    await _blob(store)
    await _note(store, "lanes/txn-2024/findings/001")
    await _note(store, "lanes/kyc-docs/findings/001")

    notes = await store.list(JOB, kind=KIND_NOTE)
    assert len(notes) == 2
    scoped = await store.list(JOB, kind=KIND_NOTE, namespace=lane_namespace("txn-2024"))
    assert [str(m.id) for m in scoped] == ["j7/note/lanes/txn-2024/findings/001"]
