"""索引器单测：卡片拼装、流式分页、rev 失效、未就绪跳过。

全部用假客户端，不连 Java / embed / Qdrant。
"""

import pytest

from app.core.indexer import Indexer, StaleGraphError, build_card
from app.models.tool import ClassNode, ClassPage, DescribeStatus, NodeKind, ToolEnvelope


def _node(uid: str, name: str, desc: str | None, kind: NodeKind = NodeKind.CLASS) -> ClassNode:
    return ClassNode(
        qualified_name=uid,
        simple_name=name,
        package_name="com.x",
        kind=kind,
        description=desc,
        file_path=f"com/x/{name}.java",
    )


def _page(rev: int, items: list[ClassNode], cursor: str | None) -> ToolEnvelope[ClassPage]:
    return ToolEnvelope[ClassPage](
        ok=True,
        data=ClassPage(rev=rev, items=items, next_cursor=cursor),
        truncated=False,
    )


class FakeJava:
    def __init__(self, status: DescribeStatus, pages: dict[str | None, ToolEnvelope]):
        self._status = status
        self._pages = pages
        self.calls: list[str | None] = []

    async def describe_status(self, project_id: int) -> DescribeStatus:
        return self._status

    async def classes_page(self, project_id: int, after_uid: str | None, limit: int):
        self.calls.append(after_uid)
        return self._pages[after_uid]


class FakeEmbed:
    async def embed(self, texts: list[str]):
        return [([0.1, 0.2, 0.3, 0.4], {5: 0.2, 2: 0.9}) for _ in texts]


class FakeQdrant:
    def __init__(self):
        self.points = []
        self.deleted: list[int] = []

    async def ensure_collection(self):
        pass

    async def delete_project(self, project_id: int):
        self.deleted.append(project_id)

    async def upsert(self, points):
        self.points.extend(points)

    async def count_project(self, project_id: int) -> int:
        return len(self.points)


class FakeStore:
    def __init__(self):
        self.state: dict | None = None

    async def set_state(self, project_id: int, state: dict):
        self.state = state


class FakeStoreWithLock(FakeStore):
    def __init__(self):
        super().__init__()
        self.released = False
        self.cleared = False

    async def acquire_lock(self, project_id: int) -> str:
        return "token"

    async def release_lock(self, project_id: int, token: str) -> None:
        self.released = True

    async def clear_state(self, project_id: int) -> None:
        self.cleared = True
        self.state = None


def _status(pending: int = 0, rev: int = 7, describable: int = 3) -> DescribeStatus:
    return DescribeStatus(
        project_id=1,
        analysis_status="COMPLETED",
        total=describable,
        describable=describable,
        described=describable - pending,
        pending=pending,
        rev=rev,
    )


def test_build_card_prefers_llm_description():
    text, source = build_card(_node("com.x.A", "A", "负责下单"))
    assert text == "A：负责下单"
    assert source == "LLM"


def test_build_card_falls_back_to_structure():
    text, source = build_card(_node("com.x.Mapper", "Mapper", None, NodeKind.INTERFACE))
    assert "接口 Mapper" in text
    assert "com.x" in text
    assert source == "FALLBACK"


@pytest.mark.asyncio
async def test_index_streams_pages_and_sets_ready():
    pages = {
        None: _page(7, [_node("com.x.A", "A", "A 的职责"), _node("com.x.B", "B", "B 的职责")], "com.x.B"),
        "com.x.B": _page(7, [_node("com.x.C", "C", None)], None),
    }
    java = FakeJava(_status(), pages)
    qdrant = FakeQdrant()
    store = FakeStore()
    indexer = Indexer(java, FakeEmbed(), qdrant, store)

    await indexer._index(1)

    assert java.calls == [None, "com.x.B"]
    assert qdrant.deleted == [1]
    assert len(qdrant.points) == 3
    assert store.state["status"] == "ready"
    assert store.state["count"] == 3
    assert store.state["rev"] == 7


@pytest.mark.asyncio
async def test_index_aborts_when_rev_changes_mid_pull():
    pages = {None: _page(8, [_node("com.x.A", "A", "A 的职责")], None)}
    store = FakeStore()
    indexer = Indexer(FakeJava(_status(), pages), FakeEmbed(), FakeQdrant(), store)

    with pytest.raises(StaleGraphError):
        await indexer._index(1)


@pytest.mark.asyncio
async def test_index_skips_when_descriptions_pending():
    qdrant = FakeQdrant()
    store = FakeStore()
    indexer = Indexer(FakeJava(_status(pending=2), {}), FakeEmbed(), qdrant, store)

    await indexer._index(1)

    assert store.state["status"] == "not_ready"
    assert qdrant.deleted == []
    assert qdrant.points == []


def test_point_sparse_indices_are_sorted():
    point = Indexer._point(
        1, 7, _node("com.x.A", "A", "A 的职责"), "LLM", [0.1],
        {5: 0.2, 2: 0.9, 9: 0.1},
    )
    assert point.vector["sparse"].indices == [2, 5, 9]
    assert point.vector["sparse"].values == [0.9, 0.2, 0.1]
    assert point.payload["descriptionSource"] == "LLM"


@pytest.mark.asyncio
async def test_delete_project_removes_points_and_state():
    qdrant = FakeQdrant()
    store = FakeStoreWithLock()
    indexer = Indexer(FakeJava(_status(), {}), FakeEmbed(), qdrant, store)

    await indexer.delete_project(5)

    assert qdrant.deleted == [5]
    assert store.cleared is True
    assert store.released is True
