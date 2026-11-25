"""能力索引：跳过空描述、rev 竞态、未就绪不建。

全部用假客户端，不连 Java / embed / Qdrant。替身与 `tests/test_indexer.py`
同款，只是分页接口换成 `capability_cards_page`。
"""

import uuid

from app.core.capability_indexer import CapabilityIndexer, capability_card_text
from app.core.indexer import StaleGraphError
from app.models.capability import CapabilityCard, CapabilityCardPage, CapabilityMember
from app.models.tool import DescribeStatus, ToolEnvelope
from app.store.index_store import IndexStore


def _card(uid="POST /orders", desc="下单并扣减库存", members=None):
    return CapabilityCard(
        uid=uid, name=uid, description=desc, area_uid="order", area_name="订单",
        area_description="订单域", entry_uid="a.OrderController#create()",
        entry_kind="HTTP", member_count=3,
        members=members or [
            CapabilityMember(uid="a.OrderController", name="OrderController", role="ENTRY", depth=0),
            CapabilityMember(uid="a.OrderService", name="OrderService", role="BUSINESS", depth=1),
            CapabilityMember(uid="a.OrderRepo", name="OrderRepo", role="INFRASTRUCTURE", depth=2),
        ],
    )


def test_card_text_includes_area_entry_and_members():
    assert capability_card_text(_card()) == (
        "订单 / POST /orders：下单并扣减库存。入口：HTTP。"
        "成员：OrderController（入口）、OrderService（业务）、OrderRepo（基础设施）"
    )


def test_blank_description_has_empty_text():
    assert capability_card_text(_card(desc="  ")) == ""


# ==================== 替身（与 tests/test_indexer.py 同款） ====================


class FakeJava:
    def __init__(self, status: DescribeStatus, pages: dict[str | None, ToolEnvelope]):
        self._status = status
        self._pages = pages
        self.calls: list[str | None] = []

    async def describe_status(self, project_id: int) -> DescribeStatus:
        return self._status

    async def capability_cards_page(
        self, project_id: int, after_uid: str | None, limit: int
    ):
        self.calls.append(after_uid)
        return self._pages[after_uid]


class FakeEmbed:
    async def embed(self, texts: list[str]):
        return [([0.1, 0.2, 0.3, 0.4], {5: 0.2, 2: 0.9}) for _ in texts]


class FakeQdrant:
    def __init__(self):
        self.points = []
        self.deleted: list[int] = []
        #: 失败版本的清点调用 / 切换成功后的旧版本清理调用
        self.discarded: list[tuple[int, int]] = []
        self.cleaned: list[tuple[int, int]] = []

    async def ensure_collection(self):
        pass

    async def delete_project(self, project_id: int):
        self.deleted.append(project_id)

    async def delete_rev(self, project_id: int, rev: int):
        self.discarded.append((project_id, rev))

    async def delete_other_revs(self, project_id: int, active_rev: int):
        self.cleaned.append((project_id, active_rev))

    async def upsert(self, points):
        self.points.extend(points)

    async def count_project(self, project_id: int, rev: int | None = None) -> int:
        return len(self.points)


class FakeStore:
    def __init__(self):
        self.state: dict | None = None

    async def get_state(self, project_id: int) -> dict | None:
        return self.state

    async def set_state(self, project_id: int, state: dict):
        self.state = state

    async def acquire_lock(self, project_id: int) -> str:
        return "token"

    async def release_lock(self, project_id: int, token: str) -> None:
        pass


def _status(rev: int = 7) -> DescribeStatus:
    return DescribeStatus(project_id=1, analysis_status="COMPLETED",
                          total=2, describable=2, described=2, pending=0, rev=rev)


def _page(rev: int, items: list[CapabilityCard], cursor: str | None = None):
    return ToolEnvelope[CapabilityCardPage](
        ok=True, data=CapabilityCardPage(rev=rev, items=items, next_cursor=cursor),
        truncated=False)


async def test_skips_blank_description_and_reports_partial():
    java = FakeJava(_status(7), {None: _page(7, [_card("POST /orders"), _card("GET /orders", desc="")])})
    qdrant, store = FakeQdrant(), FakeStore()
    indexer = CapabilityIndexer(java, FakeEmbed(), qdrant, store)

    await indexer._run(1, "token")

    assert len(qdrant.points) == 1
    assert store.state["partial"] is True
    assert store.state["expected"] == 2
    assert store.state["count"] == 1


async def test_rev_change_discards_new_rev():
    java = FakeJava(_status(7), {None: _page(8, [_card()])})
    qdrant, store = FakeQdrant(), FakeStore()
    indexer = CapabilityIndexer(java, FakeEmbed(), qdrant, store)

    await indexer._run(1, "token")

    assert qdrant.discarded == [(1, 7)]
    assert store.state["status"] == "failed"


async def test_not_ready_when_analysis_incomplete():
    java = FakeJava(
        DescribeStatus(project_id=1, analysis_status="RUNNING",
                       total=0, describable=0, described=0, pending=0, rev=7),
        {})
    store = FakeStore()
    await CapabilityIndexer(java, FakeEmbed(), FakeQdrant(), store)._run(1, "token")

    assert store.state["status"] == "not_ready"


# ==================== 补充断言（与 plan 一致的语义） ====================


async def test_failure_keeps_previous_active_rev():
    """失败的是这一版，不是整个项目：activeRev 必须保留。"""
    store = FakeStore()
    store.state = {"status": "ready", "activeRev": 6, "rev": 6}
    java = FakeJava(_status(7), {None: _page(8, [_card()])})
    indexer = CapabilityIndexer(java, FakeEmbed(), FakeQdrant(), store)

    await indexer._run(1, "token")

    assert store.state["status"] == "failed"
    assert store.state["activeRev"] == 6


async def test_point_id_payload_and_count_check():
    """点 id 用 uid 原样拼；payload 字段与计划完全一致。"""
    java = FakeJava(_status(7), {None: _page(7, [_card()])})
    qdrant, store = FakeQdrant(), FakeStore()
    indexer = CapabilityIndexer(java, FakeEmbed(), qdrant, store)

    await indexer._run(1, "token")

    point = qdrant.points[0]
    assert point.id == str(uuid.uuid5(
        uuid.NAMESPACE_URL, "archmind:cap:1:POST /orders"))
    payload = point.payload
    assert payload["uid"] == "POST /orders"
    assert payload["entryUid"] == "a.OrderController#create()"
    assert payload["areaUid"] == "order"
    assert payload["rev"] == 7
    assert payload["memberCount"] == 3
    assert payload["members"][0]["uid"] == "a.OrderController"
    assert payload["members"][0]["role"] == "ENTRY"
    assert store.state["count"] == 1
    assert store.state["activeRev"] == 7


def test_namespaced_index_keys_are_isolated():
    """能力索引的 Redis 键必须与类索引隔离；空命名空间保持 R4 原键。"""
    plain = IndexStore(client=object())
    scoped = IndexStore(client=object(), namespace="capability")
    assert plain._state_key(7) == "archmind:index:state:7"
    assert plain._lock_key(7) == "archmind:index:lock:7"
    assert scoped._state_key(7) == "archmind:index:capability:state:7"
    assert scoped._lock_key(7) == "archmind:index:capability:lock:7"
