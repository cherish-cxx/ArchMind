# R8 剩余项实现计划：领域/能力向量化 + 系统架构分层视图

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 R8 已产出的能力送进 Qdrant，使「业务词 → 能力入口」可检索；同时把类上的 `role` 投影成五层架构视图，让跨层直连可见。

**Architecture:** 两个互相独立的子系统。A 部分沿 R4 的「Java 出卡片 → Python 建索引 → Java 不碰 Qdrant」链路新增一个 `archmind_capabilities` 集合；B 部分只在 Java 读 Neo4j 后做纯函数聚合，前端新增一个只读视图，不改任何图数据。

**Tech Stack:** Java 17 / Spring Boot 3 / Neo4j Java Driver / JUnit 5；Python 3.12 / FastAPI / Pydantic / pytest；Qdrant + bge-m3（dense 1024 + sparse）；React 18 / Vite / node:test。

**Spec:** `记录每日工作/复杂到可探索/开发路线图.md`（R8「剩余」节）+ `docs/superpowers/plans/2026-11-23-r8-cognitive-layer.md`（R8 结构层）+ 2026-11-25 评审结论（放弃历史兼容；每能力独立画布；分层视图按 role 投影）。

## Scope Check

A（向量化）与 B（分层视图）没有共享代码，也不互相依赖。可以分两个分支、两个 PR 交付；本计划放在一份文档里只是方便排期。**先做 A 或先做 B 都不影响另一部分。**

## 改动总览

| 子系统 | 改动原因 | 改动方向 | 主要改动 | 成果验收 |
|---|---|---|---|---|
| A 领域/能力向量化 | Qdrant 只有类卡片，用户说「怎么下单」时语义入口只能落到类名，落不到能力 | Java 出能力卡片 → Python 写第二个集合 → 轮首双路检索 → 能力候选带入口类注入 | 1 个内部接口、1 个索引器、1 个集合、检索与 prompt 扩展 | 能力集合点数 = 有描述能力数；问业务问题时检索步骤出现能力命中，回答先落到能力入口 |
| B 系统架构分层视图 | `role` 只是类上的属性，分层与跨层直连没有视图，架构问题看不出来 | 不新增图数据，按 role + 业务领域 + 包名投影成六层，实时聚合跨层调用 | 1 个纯函数、1 个查询接口、1 个前端视图与切换器 | 六层齐全、类数守恒；点击边能看到具体类对；未归类不隐藏；同层调用不出现 |

## Global Constraints

- **不做历史项目兼容**：不为 R8 之前生成的分析结果补数据，不提供「只重跑 DOMAIN」入口。老项目必须重新分析。
- **Java 是图的主人**：Python 不得写 Neo4j；能力卡片只能通过 `/internal/**` 只读接口取。
- **能力 uid 是不透明字符串**：可能含空格、`/`、`#`（如 `POST /orders`、`SCHEDULED a.B#run()`）。不得 trim、不得按第一个空格切分后再拼回、不得放进 path 段；只放 query/body。
- **Qdrant 集合固定**：dense 1024 / Cosine + sparse；payload 索引 `projectId(keyword)`、`rev(integer)`；所有点带 `projectId + rev`。
- **描述是嵌入的必需品**：`description` 为空的能力不写向量，并在索引状态里体现 `partial`。
- **不改 R4 类索引行为**：`archmind_classes` 的流式、rev 闸门、覆盖率校验保持原样；能力索引失败不能让类索引失败。
- **API 大小写**：Java DTO 对外 camelCase；Python 内部 snake_case；JavaClient 请求参数必须 camelCase。
- **分层视图只读**：不新增节点、不改 `role`、不做第三方依赖解析。
- 现有测试基线不能红：Java `mvnw test`、Python `pytest tests`、前端 `node --test`。

## Review Focus

1. **能力 uid 带空格/斜杠/井号**：keyset 分页、URL、Redis 状态里都必须原样传递；任何一处 trim 或切分都会静默错页或 404。
2. **描述为空的能力**：必须跳过嵌入并在状态里报 `partial`，否则「POST /orders」这种纯名字卡片会把语义检索带偏。
3. **索引 rev 竞态与失败隔离**：能力索引失败或拉到一半 rev 变了，只能丢弃新 rev，绝不能清掉 `activeRev`，也不能影响类索引。
4. **能力检索故障**：Qdrant/embed 挂掉时，能力候选为空但类候选必须照常返回，且总耗时不超过一次超时。
5. **role 为空或同层调用**：未知 role 必须进「未归类」而不是消失；同层调用不能出现在跨层边里。

---

## File Structure

**Part A（后端 + agent-service）**

| 文件 | 责任 |
|---|---|
| `ArchMind/src/main/java/com/example/archmind/dto/response/CapabilityCardDTO.java` | 能力卡片 |
| `.../CapabilityCardPageResponse.java` | 卡片分页 + rev |
| `.../CapabilityMemberDTO.java` | 卡片里的成员类 |
| `.../service/domain/CapabilityCardQueryService.java` | 索引专用读接口 |
| `.../service/impl/CapabilityCardQueryServiceImpl.java` | Cypher + 纯函数装配 |
| `.../controller/InternalToolController.java`（改） | 暴露 `/capabilities/cards` |
| `agent-service/app/models/capability.py` | Python 契约模型 |
| `agent-service/app/tools/java_client.py`（改） | `capability_cards_page` |
| `agent-service/app/core/capability_indexer.py` | 能力索引器 |
| `agent-service/app/store/index_store.py`（改） | 状态 key 支持命名空间 |
| `agent-service/app/core/retriever.py`（改） | 类 + 能力双路检索 |
| `agent-service/app/models/retrieval.py`（改） | `CapabilityCandidate` |
| `agent-service/app/core/prompt.py`（改） | 能力候选渲染 |
| `agent-service/app/core/agent_loop.py`（改） | 注入能力块与入口类 |
| `agent-service/app/main.py`（改） | 装配第二个集合/索引器/检索器 |
| `agent-service/app/api/index.py`（改） | 同时触发两类索引 |
| `agent-service/app/api/agent.py`（改） | 删除项目时清能力索引 |

**Part B（Java + React）**

| 文件 | 责任 |
|---|---|
| `ArchMind/src/main/java/com/example/archmind/service/domain/ArchitectureLayer.java` | 六层枚举与 role 映射 |
| `.../service/domain/ArchitectureLayerResolver.java` | 纯函数聚合成层/组/边 |
| `.../dto/response/ArchitectureLayersResponse.java` 等 6 个 record | 视图契约 |
| `.../service/domain/ArchitectureQueryService.java` | 查询接口 |
| `.../service/impl/ArchitectureQueryServiceImpl.java` | 两条 Cypher + 映射 |
| `.../controller/ArchitectureController.java` | `GET /api/project/{id}/architecture-layers` |
| `ArchMind-react/src/api/domain.js`（改） | 新增接口 |
| `ArchMind-react/src/utils/architecture.js` | 纯函数 |
| `ArchMind-react/src/components/architecture/LayerBands.jsx` | 分层视图 |
| `ArchMind-react/src/components/architecture/architecture.css` | 样式 |
| `ArchMind-react/src/pages/DomainMapPage.jsx`（改） | 版图/分层切换 |

---

# Part A：领域/能力向量化

`archmind_capabilities` 的点 = 一个能力。域不单独建点，领域文本写进能力卡片；领域命中由能力命中聚合。

### Task 1: Java 能力卡片分页接口

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/CapabilityMemberDTO.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/CapabilityCardDTO.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/CapabilityCardPageResponse.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/CapabilityCardQueryService.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/impl/CapabilityCardQueryServiceImpl.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/controller/InternalToolController.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/CapabilityCardQueryServiceImplTest.java`

**Interfaces:**
- Consumes: `Driver`, `GraphRevService.currentRev(Long)`, `CheckProjectUtil.checkProjectExists(Long)`
- Produces:
  - `CapabilityCardPageResponse listCapabilityCardsPage(Long projectId, String afterUid, int limit)`
  - `GET /internal/tools/capabilities/cards?projectId=&afterUid=&limit=`
  - `ToolEnvelope<CapabilityCardPageResponse>`，其中 `rev` 来自 `GraphMeta.rev`

- [ ] **Step 1: 写失败测试**

```java
package com.example.archmind.service.impl;

import com.example.archmind.dto.response.CapabilityCardPageResponse;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class CapabilityCardQueryServiceImplTest {

    @Test
    void assembleSortsMembersByDepthAndFlagsTruncation() {
        var caps = List.of(row("POST /orders", 10));
        var members = Map.of("POST /orders", List.of(
                member("a.OrderController", "OrderController", "ENTRY", 0),
                member("a.OrderRepo", "OrderRepo", "INFRASTRUCTURE", 2),
                member("a.OrderService", "OrderService", "BUSINESS", 1)));

        CapabilityCardPageResponse page =
                CapabilityCardQueryServiceImpl.assemble(caps, members, 7L, "next-uid");

        assertEquals(7L, page.getRev());
        assertEquals("next-uid", page.getNextCursor());
        var card = page.getItems().get(0);
        assertEquals(List.of("a.OrderController", "a.OrderService", "a.OrderRepo"),
                card.getMembers().stream().map(m -> m.getUid()).toList());
        assertTrue(card.isMembersTruncated());
        assertEquals(10, card.getMemberCount());
    }

    @Test
    void assembleKeepsOpaqueCapabilityUid() {
        var caps = List.of(row("SCHEDULED a.Job#run()", 1));
        var page = CapabilityCardQueryServiceImpl.assemble(
                caps, Map.of(), 3L, null);
        assertEquals("SCHEDULED a.Job#run()", page.getItems().get(0).getUid());
    }

    private static Map<String, Object> row(String uid, int memberCount) {
        return Map.of("uid", uid, "name", uid, "description", "d",
                "areaUid", "order", "areaName", "订单", "areaDescription", "订单域",
                "entryUid", "a.OrderController#create()", "entryKind", "HTTP",
                "memberCount", memberCount);
    }

    private static Map<String, Object> member(String uid, String name, String role, int depth) {
        return Map.of("uid", uid, "name", name, "role", role, "depth", depth);
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=CapabilityCardQueryServiceImplTest test`
Expected: 编译失败，`CapabilityCardQueryServiceImpl` 不存在

- [ ] **Step 3: 实现 DTO、查询与控制器**

DTO 字段（Lombok `@Data`，`List` 初始化为 `List.of()`）：

```java
public class CapabilityMemberDTO { String uid; String name; String role; int depth; }
public class CapabilityCardDTO {
    String uid; String name; String description;
    String areaUid; String areaName; String areaDescription;
    String entryUid; String entryKind;
    int memberCount; List<CapabilityMemberDTO> members; boolean membersTruncated;
}
public class CapabilityCardPageResponse { long rev; List<CapabilityCardDTO> items; String nextCursor; }
```

`CapabilityCardQueryServiceImpl`：

```java
private static final int MEMBER_CAP = 8;

static final String CAPABILITY_PAGE = """
        MATCH (cap:Capability:CodeNode {projectId:$pid})
        WHERE $afterUid IS NULL OR cap.uid > $afterUid
        WITH cap ORDER BY cap.uid LIMIT $limit
        MATCH (cap)-[:IN_AREA]->(a:BusinessArea {projectId:$pid})
        OPTIONAL MATCH (cap)<-[p:PART_OF_CAPABILITY]-(c:Class {projectId:$pid})
        WITH cap, a, count(c) AS memberCount
        OPTIONAL MATCH (e:Endpoint {projectId:$pid}) WHERE e.uid = cap.uid
        RETURN cap.uid AS uid, cap.name AS name, cap.description AS description,
               a.uid AS areaUid, a.name AS areaName, a.description AS areaDescription,
               cap.entryUid AS entryUid, head(collect(DISTINCT e.kind)) AS entryKind,
               memberCount
        ORDER BY uid
        """;

static final String CAPABILITY_MEMBERS = """
        MATCH (cap:Capability:CodeNode {projectId:$pid})<-[p:PART_OF_CAPABILITY]-(c:Class {projectId:$pid})
        WHERE cap.uid IN $uids
        WITH cap, c, p ORDER BY p.depth, c.uid
        RETURN cap.uid AS capUid,
               collect({uid:c.uid, name:c.name, role:c.role, depth:p.depth})[0..8] AS members,
               count(c) AS total
        """;
```

`listCapabilityCardsPage` 的行为：
- `size = Math.max(1, Math.min(limit, 200))`；
- 用 `limit = size + 1` 查，多出的一条只用来判断 `nextCursor`，然后从 items 里移除；
- 成员查询用本页 uid 列表；`membersTruncated = memberCount > members.size()`；
- `rev = graphRevService.currentRev(projectId)`；
- `assemble(List<Map<String,Object>> caps, Map<String,List<Map<String,Object>>> members, long rev, String cursor)` 为包级 `static`，按 uid 排序、成员按 depth 再 uid 排序。

`InternalToolController` 新增依赖字段 `private final CapabilityCardQueryService capabilityCardQueryService;`（类上已有 `@RequiredArgsConstructor`），再新增：

```java
@GetMapping("/capabilities/cards")
public ToolEnvelope<CapabilityCardPageResponse> capabilityCardsPage(
        @RequestParam Long projectId,
        @RequestParam(required = false) String afterUid,
        @RequestParam(defaultValue = "200") int limit) {
    try {
        checkProjectUtil.checkProjectExists(projectId);
        return stamp(ToolEnvelope.success(
                capabilityCardQueryService.listCapabilityCardsPage(projectId, afterUid, limit)), projectId);
    } catch (BusinessException e) {
        return stamp(ToolEnvelope.failure(e.getMessage()), projectId);
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=CapabilityCardQueryServiceImplTest test`
Expected: PASS（2 tests）

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/dto/response/CapabilityCard*.java \
        ArchMind/src/main/java/com/example/archmind/dto/response/CapabilityMemberDTO.java \
        ArchMind/src/main/java/com/example/archmind/service/domain/CapabilityCardQueryService.java \
        ArchMind/src/main/java/com/example/archmind/service/impl/CapabilityCardQueryServiceImpl.java \
        ArchMind/src/main/java/com/example/archmind/controller/InternalToolController.java \
        ArchMind/src/test/java/com/example/archmind/service/impl/CapabilityCardQueryServiceImplTest.java
git commit -m "feat(domain): expose capability cards for indexing"
```

---

### Task 2: Python 契约模型与 JavaClient

**Files:**
- Create: `agent-service/app/models/capability.py`
- Modify: `agent-service/app/tools/java_client.py`
- Test: `agent-service/tests/test_capability_contract.py`

**Interfaces:**
- Consumes: Task 1 的 `ToolEnvelope` JSON
- Produces:
  - `CapabilityCard`, `CapabilityCardPage`, `CapabilityMember`
  - `JavaClient.capability_cards_page(project_id, after_uid=None, limit=200) -> ToolEnvelope[CapabilityCardPage]`

- [ ] **Step 1: 写失败测试**

```python
"""能力卡片契约：路径、camelCase 参数、null 字段解析。"""

import json

import httpx
import pytest

from app.config import INTERNAL_TOKEN_HEADER
from app.tools.java_client import JavaClient

_ENVELOPE = {
    "ok": True,
    "data": {
        "rev": 7,
        "items": [{
            "uid": "POST /orders",
            "name": "POST /orders",
            "description": "下单并扣减库存",
            "areaUid": "order",
            "areaName": "订单",
            "areaDescription": None,
            "entryUid": "a.OrderController#create()",
            "entryKind": "HTTP",
            "memberCount": 2,
            "members": [
                {"uid": "a.OrderController", "name": "OrderController", "role": "ENTRY", "depth": 0},
                {"uid": "a.OrderService", "name": "OrderService", "role": "BUSINESS", "depth": 1},
            ],
            "membersTruncated": False,
        }],
        "nextCursor": None,
    },
    "evidence": [],
    "truncated": False,
    "graphRev": 7,
}


async def test_capability_cards_page_is_get_with_camel_case_params():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["params"] = dict(req.url.params)
        return httpx.Response(200, json=_ENVELOPE)

    c = JavaClient()
    c._client = httpx.AsyncClient(
        base_url="http://mock", transport=httpx.MockTransport(handler),
        headers={INTERNAL_TOKEN_HEADER: "t"}, trust_env=False)

    page = await c.capability_cards_page(7, after_uid="POST /orders", limit=50)

    assert captured["method"] == "GET"
    assert captured["path"] == "/internal/tools/capabilities/cards"
    assert captured["params"] == {"projectId": "7", "afterUid": "POST /orders", "limit": "50"}
    assert page.data.items[0].uid == "POST /orders"
    assert page.data.items[0].members[1].role == "BUSINESS"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd agent-service && ./.venv/Scripts/python.exe -m pytest tests/test_capability_contract.py -q`
Expected: FAIL / ImportError

- [ ] **Step 3: 实现模型与客户端**

`app/models/capability.py`：

```python
class CapabilityMember(CamelModel):
    uid: str
    name: str
    role: str | None = None
    depth: int = 0


class CapabilityCard(CamelModel):
    uid: str
    name: str
    description: str | None = None
    area_uid: str
    area_name: str
    area_description: str | None = None
    entry_uid: str
    entry_kind: str | None = None
    member_count: int = 0
    members: list[CapabilityMember] = []
    members_truncated: bool = False


class CapabilityCardPage(CamelModel):
    rev: int
    items: list[CapabilityCard] = []
    next_cursor: str | None = None
```

`java_client.py` 常量与 import 增加，方法照 `class_cards_page` 写：

```python
_PATH_CAPABILITY_CARDS = f"{_PREFIX}/capabilities/cards"

async def capability_cards_page(
    self, project_id: int, after_uid: str | None = None, limit: int = DEFAULT_PAGE_SIZE
) -> ToolEnvelope[CapabilityCardPage]:
    params: dict[str, Any] = {"projectId": project_id, "limit": limit}
    if after_uid:
        params["afterUid"] = after_uid
    resp = await self._send("GET", _PATH_CAPABILITY_CARDS, params=params)
    return ToolEnvelope[CapabilityCardPage].model_validate(resp.json())
```

`uid` 含空格/井号，交给 httpx 编码，不要自己 `quote`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd agent-service && ./.venv/Scripts/python.exe -m pytest tests/test_capability_contract.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/models/capability.py agent-service/app/tools/java_client.py \
        agent-service/tests/test_capability_contract.py
git commit -m "feat(agent): add capability card contract"
```

---

### Task 3: 能力索引器（Qdrant 第二个集合）

**Files:**
- Create: `agent-service/app/core/capability_indexer.py`
- Modify: `agent-service/app/config.py`
- Modify: `agent-service/app/store/index_store.py`
- Test: `agent-service/tests/test_capability_indexer.py`

**Interfaces:**
- Consumes: `JavaClient.capability_cards_page`、`EmbedClient.embed(list[str]) -> list[(dense, sparse)]`、`QdrantStore`、`IndexStore`
- Produces:
  - `capability_card_text(card: CapabilityCard) -> str`（空描述返回 `""`）
  - `CapabilityIndexer.start(project_id: int, rev: int | None = None) -> dict`
  - `CapabilityIndexer.delete_project(project_id) -> None`
  - `CapabilityIndexer.aclose() -> None`
  - Redis key：`archmind:index:capability:state:*` / `...:lock:*`

- [ ] **Step 1: 写失败测试**

```python
"""能力索引：跳过空描述、rev 竞态、未就绪不建。"""

import pytest

from app.core.capability_indexer import CapabilityIndexer, capability_card_text
from app.core.indexer import StaleGraphError
from app.models.capability import CapabilityCard, CapabilityCardPage, CapabilityMember
from app.models.tool import DescribeStatus, ToolEnvelope


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
```

索引器测试用与 `tests/test_indexer.py` 相同的替身模式。把该文件顶部的 `FakeEmbed`、`FakeQdrant`、`FakeStore` 复制过来，并把 `FakeJava.class_cards_page` 改成 `capability_cards_page`、页面类型改成 `CapabilityCardPage`。补以下断言：

```python
def _status(rev: int = 7) -> DescribeStatus:
    return DescribeStatus(project_id=1, analysis_status="COMPLETED",
                          total=2, describable=2, described=2, pending=0, rev=rev)


def _page(rev: int, items: list[CapabilityCard], cursor: str | None = None):
    return ToolEnvelope[CapabilityCardPage](
        ok=True, data=CapabilityCardPage(rev=rev, items=items, next_cursor=cursor), truncated=False)


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
```

> `FakeStore` 需要实现 `get_state / set_state / acquire_lock / release_lock`（复制 `tests/test_indexer.py` 里的那版）。`FakeQdrant` 的 `count_project` 返回 `len(self.points)`，`delete_rev` 记到 `discarded`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd agent-service && ./.venv/Scripts/python.exe -m pytest tests/test_capability_indexer.py -q`
Expected: FAIL / ImportError

- [ ] **Step 3: 实现索引器与命名空间**

`config.py` 增加：

```python
qdrant_capability_collection: str = "archmind_capabilities"
```

`IndexStore` 增加构造参数 `namespace: str = ""`；key 方法改为实例方法：

```python
def _state_key(self, project_id): return f"{self._prefix}state:{project_id}"
def _lock_key(self, project_id): return f"{self._prefix}lock:{project_id}"
# self._prefix = "archmind:index:" if not namespace else f"archmind:index:{namespace}:"
```

默认 `""` 时必须仍生成 `archmind:index:state:{pid}`，保证 R4 现有键不变。

`capability_indexer.py` 照 `Indexer` 的 rev 闸门实现，差异只有四点：
- 未就绪判定只要求 `analysis_status == "COMPLETED"`（能力描述在任务成功前已写完，不依赖类描述补齐的 `pending`）；
- 逐页 `capability_cards_page`，每页校验 `page.data.rev == rev`，不等则抛 `StaleGraphError`；
- 描述为空的能力跳过，`expected` 计全部看到的卡片，`count` 计实际写入，`partial = count < expected`；
- 覆盖数校验用 `qdrant.count_project(project_id, rev) == count`，不做百分比门槛（能力数量小，少一个就是少一个）。

`start / _run / _index / _discard_rev / delete_project / aclose` 的结构直接照 `Indexer` 抄，**不要省略失败时保留 `activeRev` 的那段**。

点构造：

```python
id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"archmind:cap:{project_id}:{card.uid}"))
payload = {
    "projectId": project_id, "uid": card.uid, "name": card.name,
    "description": card.description, "areaUid": card.area_uid,
    "areaName": card.area_name, "areaDescription": card.area_description,
    "entryUid": card.entry_uid, "entryKind": card.entry_kind,
    "memberCount": card.member_count,
    "members": [m.model_dump(by_alias=True) for m in card.members],
    "rev": rev,
}
```

`capability_card_text` 的拼法：

```python
def role_label(role: str | None) -> str:
    key = (role or "").lower().replace("_", "-")
    return {"entry": "入口", "business": "业务", "domain-model": "领域模型",
            "infrastructure": "基础设施", "external": "外部依赖"}.get(key, "其他")

def capability_card_text(card: CapabilityCard) -> str:
    desc = (card.description or "").strip()
    if not desc:
        return ""
    head = f"{card.area_name or card.area_uid} / {card.name or card.uid}"
    entry = card.entry_kind or "入口"
    member_text = "、".join(
        f"{m.uid.rsplit('.', 1)[-1]}（{role_label(m.role)}）" for m in card.members[:8])
    text = f"{head}：{desc}。入口：{entry}。"
    return text + (f"成员：{member_text}" if member_text else "")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd agent-service && ./.venv/Scripts/python.exe -m pytest tests/test_capability_indexer.py tests/test_indexer.py -q`
Expected: PASS（含 R4 索引回归）

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/core/capability_indexer.py agent-service/app/config.py \
        agent-service/app/store/index_store.py agent-service/tests/test_capability_indexer.py
git commit -m "feat(agent): index capabilities into qdrant"
```

---

### Task 4: 双路检索与候选注入

**Files:**
- Modify: `agent-service/app/models/retrieval.py`
- Modify: `agent-service/app/core/retriever.py`
- Modify: `agent-service/app/core/prompt.py`
- Modify: `agent-service/app/core/agent_loop.py`
- Modify: `agent-service/app/core/turn_support.py`
- Test: `agent-service/tests/test_capability_retrieval.py`

**Interfaces:**
- Consumes: `CapabilityIndexer` 写入的 `archmind_capabilities` 与独立状态
- Produces:
  - `CapabilityCandidate`（字段见下）
  - `Retriever.search(...) -> Retrieval`，`Retrieval.capabilities` 与 `Retrieval.capability_reason`
  - `render_capabilities(caps) -> str`
  - Agent 轮首同时注入类候选与能力候选；能力入口类进入 `_seed_known`

- [ ] **Step 1: 写失败测试**

```python
"""能力检索：故障隔离、payload 映射、prompt 空块。"""

from types import SimpleNamespace

import pytest

from app.core.prompt import render_capabilities
from app.core.retriever import Retriever, to_capability
from app.models.retrieval import CapabilityCandidate


def _class_point(uid="a.OrderService"):
    return SimpleNamespace(payload={"uid": uid, "rev": 7, "kind": "CLASS"}, score=0.03)


def _cap_point(uid="POST /orders", **payload):
    body = {
        "uid": uid, "rev": 7, "name": uid, "description": "下单",
        "areaUid": "order", "areaName": "订单", "entryUid": "a.OrderController",
        "entryKind": "HTTP", "memberCount": 3, **payload,
    }
    return SimpleNamespace(payload=body, score=0.04)


async def test_capability_failure_keeps_class_candidates():
    embed = FakeEmbed()
    class_qdrant = FakeQdrant([_class_point()])
    cap_qdrant = FakeQdrant(raises=RuntimeError("down"))
    r = Retriever(embed, class_qdrant, FakeIndexStore({"activeRev": 7}),
                  capability_qdrant=cap_qdrant,
                  capability_store=FakeIndexStore({"activeRev": 7}))
    result = await r.search(1, "怎么下单")
    assert [c.uid for c in result.candidates] == ["a.OrderService"]
    assert result.capabilities == []
    assert result.capability_reason == "qdrant_failed"


async def test_capability_payload_maps_and_drops_without_entry():
    assert to_capability(_cap_point()).entry_uid == "a.OrderController"
    assert to_capability(_cap_point(entryUid=None)) is None


def test_render_capabilities_is_empty_without_hits():
    assert render_capabilities([]) == ""
```

> `FakeEmbed`、`FakeQdrant`、`FakeIndexStore` 直接复制 `tests/test_retriever.py` 的版本（不要跨测试文件 import）。`FakeQdrant.query` 必须接受 `rev`、`limit`、`prefetch_limit` 三个关键字参数。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd agent-service && ./.venv/Scripts/python.exe -m pytest tests/test_capability_retrieval.py -q`
Expected: FAIL / ImportError

- [ ] **Step 3: 实现模型、双路查询与渲染**

`retrieval.py` 新增：

```python
@dataclass(frozen=True)
class CapabilityCandidate:
    uid: str
    name: str
    description: str | None
    area_uid: str
    area_name: str
    entry_uid: str
    entry_kind: str | None
    member_count: int
    rev: int
    score: float

# Retrieval 增加：
capabilities: list[CapabilityCandidate] = field(default_factory=list)
capability_reason: str | None = None
```

`Retriever.__init__` 增加两个仅关键字参数 `capability_qdrant=None, capability_store=None`。`search` 的顺序：
1. 读类状态与能力状态（各自 try，失败只影响自己）；
2. **两边都没有 `activeRev` 才提前返回**（类 reason=类状态，能力 reason=能力状态）；只有一边有时，仍然 embed 并只查那一边，另一边 reason 记 `absent`；
3. embed 一次；失败时两类都返回 `embed_failed`；
4. 用 `asyncio.gather` 并发跑两个 `_query(...)`，各自 `asyncio.wait_for(timeout=self._timeout)`；异常按路记为 `qdrant_failed`；
5. 类候选保持现有映射；能力候选走 `to_capability`，缺 `uid` 或 `entryUid` 的丢弃；
6. 返回 `Retrieval(candidates=..., capabilities=..., reason=..., capability_reason=...)`。

`to_capability(point) -> CapabilityCandidate | None` 只读 payload，不抛异常。

`prompt.py`：

```python
def _capabilities_block(caps: list[CapabilityCandidate] | None) -> str:
    if not caps:
        return ""
    lines = ["本项目检索到的业务能力（候选，不是证据；入口类 uid 可照抄）："]
    for i, c in enumerate(caps, 1):
        desc = (c.description or "").strip().replace("\n", " ")
        if len(desc) > 60:
            desc = desc[:60] + "…"
        lines.append(f"{i}. {c.name or c.uid}（{c.uid}）| 领域：{c.area_name or c.area_uid}"
                     f" | 入口：{c.entry_uid} | {c.member_count} 个类 | {desc}")
    return "\n".join(lines)

def render_capabilities(caps) -> str:
    return _capabilities_block(caps)
```

`agent_loop.py`（import 行同时加入 `render_capabilities`）：

```python
if retrieval.capabilities:
    ctx.append_retrieval(
        render_capabilities(retrieval.capabilities),
        uids=[c.entry_uid for c in retrieval.capabilities],
    )

def _seed_known(...):
    ...
    for cap in getattr(retrieval, "capabilities", ()) or ():
        known.add(cap.entry_uid)
```

`turn_support._retrieve_detail`：没有能力命中时**保持原字符串不变**；有能力命中时输出 `retrieve(3 classes, 2 capabilities)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd agent-service && ./.venv/Scripts/python.exe -m pytest tests/test_capability_retrieval.py tests/test_retriever.py tests/test_agent_loop.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/models/retrieval.py agent-service/app/core/retriever.py \
        agent-service/app/core/prompt.py agent-service/app/core/agent_loop.py \
        agent-service/app/core/turn_support.py agent-service/tests/test_capability_retrieval.py
git commit -m "feat(agent): retrieve capabilities alongside classes"
```

---

### Task 5: 装配、触发与部署

**Files:**
- Modify: `agent-service/app/main.py`
- Modify: `agent-service/app/api/index.py`
- Modify: `agent-service/app/api/agent.py`
- Modify: `deploy/.env.example`
- Modify: `deploy/docker-compose.yml`
- Test: `agent-service/tests/test_app_smoke.py`（现有）

**Interfaces:**
- Consumes: Task 3/4 的 `CapabilityIndexer`、`CapabilityCandidate`
- Produces: 一次 `/internal/index` 同时启动两类索引；项目删除同时清两个集合

- [ ] **Step 1: 写失败测试（smoke）**

在 `tests/test_app_smoke.py` 加一个断言：

```python
def test_app_exposes_capability_indexer():
    with TestClient(app) as c:
        assert hasattr(app.state, "capability_indexer")
        assert hasattr(app.state, "capability_qdrant")
        assert c.get("/health").status_code == 200
```

Run: `cd agent-service && ./.venv/Scripts/python.exe -m pytest tests/test_app_smoke.py -q`
Expected: FAIL（属性不存在）

- [ ] **Step 2: 装配**

`main.py` lifespan：

```python
app.state.capability_qdrant = QdrantStore(collection=settings.qdrant_capability_collection)
app.state.capability_index_store = IndexStore(namespace="capability")
app.state.capability_indexer = CapabilityIndexer(
    app.state.java_client, app.state.embed_client,
    app.state.capability_qdrant, app.state.capability_index_store,
)
app.state.retriever = Retriever(
    app.state.embed_client, app.state.qdrant, app.state.index_store,
    capability_qdrant=app.state.capability_qdrant,
    capability_store=app.state.capability_index_store,
)
```

关停时 `await app.state.capability_indexer.aclose()`、`await app.state.capability_qdrant.aclose()`、`await app.state.capability_index_store.aclose()`。

`api/index.py`：

```python
def get_capability_indexer(request: Request) -> CapabilityIndexer:
    return request.app.state.capability_indexer


@router.post("")
async def start_index(req, indexer=Depends(get_indexer),
                      capability_indexer=Depends(get_capability_indexer)):
    classes = await indexer.start(req.project_id, req.rev)
    capabilities = await capability_indexer.start(req.project_id, req.rev)
    return {**classes, "capability": capabilities}
```

`GET /internal/index/status` 增加 `kind: str = Query("classes")`，`capabilities` 时读能力状态。默认行为完全不变。

> 触发点有意与类索引共用：`DescriptionBackfillExecutor` 在类描述补齐完成后调一次 `/internal/index`，两类索引一起开始。这样「检索可用」只有一个时刻，不会出现能力能搜、类还不能搜的半开状态；代价是能力检索也要等类描述补齐。

`api/agent.py` 的 `/admin/evict` 在类索引清理后加：

```python
try:
    await request.app.state.capability_indexer.delete_project(req.project_id)
except Exception:
    logger.warning("清理能力索引失败 projectId=%s", req.project_id, exc_info=True)
```

`deploy/.env.example` 与 `deploy/docker-compose.yml`（两个服务）加：

```yaml
QDRANT_CAPABILITY_COLLECTION=${QDRANT_CAPABILITY_COLLECTION:-archmind_capabilities}
```

- [ ] **Step 3: 跑测试确认通过**

Run: `cd agent-service && ./.venv/Scripts/python.exe -m pytest tests/test_app_smoke.py tests/test_capability_indexer.py tests/test_capability_retrieval.py -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add agent-service/app/main.py agent-service/app/api/index.py agent-service/app/api/agent.py \
        agent-service/tests/test_app_smoke.py deploy/.env.example deploy/docker-compose.yml
git commit -m "feat(agent): wire capability index and retrieval"
```

---

# Part B：系统架构分层视图

数据全部来自现有图：`Class.role`、`Class.packageName`、`BELONGS_TO`、方法级 `CALLS`。不新增节点，不做外部依赖解析。

### Task 6: 层映射与聚合纯函数

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/ArchitectureLayer.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/ArchitectureLayerResolver.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/ArchitectureLayersResponse.java`
- Create: `.../ArchitectureLayerDto.java`
- Create: `.../ArchitectureGroupDto.java`
- Create: `.../ArchitectureClassDto.java`
- Create: `.../ArchitectureLayerEdgeDto.java`
- Create: `.../ArchitectureEdgeSampleDto.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/domain/ArchitectureLayerResolverTest.java`

**Interfaces:**
- Consumes: 类行 `(uid, name, role, packageName, areaUid, areaName)`，调用行 `(fromUid, toUid, count)`
- Produces:
  - `ArchitectureLayersResponse resolve(List<ClassRow>, List<PairCount>, int maxClassesPerGroup, int maxSamplesPerEdge)`
  - `ArchitectureLayer.fromRole(String)`
  - `ClassRow` / `PairCount` 嵌套 record

- [ ] **Step 1: 写失败测试**

```java
class ArchitectureLayerResolverTest {

    private final ArchitectureLayerResolver resolver = new ArchitectureLayerResolver();

    @Test
    void mapsRolesToSixLayersInFixedOrderAndKeepsUnknown() {
        var rows = List.of(
                cls("a.C", "C", "ENTRY", "a.web", null, null),
                cls("a.S", "S", "business", "a.app", "order", "订单"),
                cls("a.E", "E", null, "a.x", null, null));

        var resp = resolver.resolve(rows, List.of(), 12, 5);

        assertEquals(List.of("entry", "business", "domain-model",
                "infrastructure", "external", "other"),
                resp.layers().stream().map(ArchitectureLayerDto::key).toList());
        assertEquals(1, resp.layers().get(5).classCount());
    }

    @Test
    void groupsByAreaForBusinessLayersAndByLastPackageForInfrastructure() {
        var rows = List.of(
                cls("a.S", "S", "BUSINESS", "a.app", "order", "订单"),
                cls("b.S", "S2", "BUSINESS", "b.app", "item", "商品"),
                cls("a.R", "R", "INFRASTRUCTURE", "a.infra.mapper", null, null),
                cls("a.U", "U", "INFRASTRUCTURE", "a.infra.mapper", null, null));

        var resp = resolver.resolve(rows, List.of(), 12, 5);

        var business = resp.layers().stream()
                .filter(l -> l.key().equals("business")).findFirst().orElseThrow();
        var infrastructure = resp.layers().stream()
                .filter(l -> l.key().equals("infrastructure")).findFirst().orElseThrow();
        assertEquals(List.of("order", "item"),
                business.groups().stream().map(ArchitectureGroupDto::key).toList());
        assertEquals("mapper", infrastructure.groups().get(0).key());
    }

    @Test
    void aggregatesOnlyCrossLayerCallsAndKeepsTopSamples() {
        var rows = List.of(
                cls("a.C", "C", "ENTRY", "a.web", null, null),
                cls("a.R", "R", "INFRASTRUCTURE", "a.mapper", null, null),
                cls("a.U", "U", "INFRASTRUCTURE", "a.mapper", null, null));
        var calls = List.of(
                new ArchitectureLayerResolver.PairCount("a.C", "a.R", 12),
                new ArchitectureLayerResolver.PairCount("a.C", "a.U", 3),
                new ArchitectureLayerResolver.PairCount("a.R", "a.U", 9));

        var resp = resolver.resolve(rows, calls, 12, 1);

        assertEquals(1, resp.edges().size());
        var edge = resp.edges().get(0);
        assertEquals("entry", edge.fromLayer());
        assertEquals("infrastructure", edge.toLayer());
        assertEquals(15, edge.count());
        assertEquals("a.C", edge.samples().get(0).fromUid());
        assertEquals(12, edge.samples().get(0).count());
    }

    private static ArchitectureLayerResolver.ClassRow cls(
            String uid, String name, String role, String pkg, String areaUid, String areaName) {
        return new ArchitectureLayerResolver.ClassRow(uid, name, role, pkg, areaUid, areaName);
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=ArchitectureLayerResolverTest test`
Expected: 编译失败

- [ ] **Step 3: 实现枚举、record 与纯函数**

```java
public enum ArchitectureLayer {
    ENTRY("entry", "用户入口层"),
    BUSINESS("business", "业务层"),
    DOMAIN_MODEL("domain-model", "领域模块层"),
    INFRASTRUCTURE("infrastructure", "基础设施"),
    EXTERNAL("external", "外部依赖"),
    OTHER("other", "未归类");

    public static ArchitectureLayer fromRole(String role) {
        if (role == null || role.isBlank()) return OTHER;
        return switch (role.trim().toUpperCase().replace('-', '_')) {
            case "ENTRY" -> ENTRY;
            case "BUSINESS" -> BUSINESS;
            case "DOMAIN_MODEL" -> DOMAIN_MODEL;
            case "INFRASTRUCTURE" -> INFRASTRUCTURE;
            case "EXTERNAL" -> EXTERNAL;
            default -> OTHER;
        };
    }
}
```

`ArchitectureLayerResolver` 规则（实现时逐条落地）：
- 层固定输出 6 个，空层也输出（`classCount=0`，`groups=[]`），前端不需要自己补；
- 入口/业务/领域模型：按 `areaUid` 分组，`key=areaUid`（null → `unassigned`），`name=areaName`（null → `未归属领域`）；
- 基础设施/外部/其它：按包名最后一段分组，`key/name=lastSegment(packageName)`（null → `default`），`detail=完整包名`；
- 组按 `classCount` 降序、再按 key；组内类按 name、再 uid，最多 `maxClassesPerGroup`，超出 `truncated=true`；
- 边只看两端 role 都已知且不同的调用；按 `(fromLayer, toLayer)` 累加 `count`；
- 每个层对的样本按单条调用数降序，最多 `maxSamplesPerEdge`；边按 count 降序、再按 from/to；
- `classCount` = 全部类数（含未归类）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=ArchitectureLayerResolverTest test`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/service/domain/ArchitectureLayer*.java \
        ArchMind/src/main/java/com/example/archmind/dto/response/Architecture*.java \
        ArchMind/src/test/java/com/example/archmind/service/domain/ArchitectureLayerResolverTest.java
git commit -m "feat(domain): derive architecture layers from roles"
```

---

### Task 7: 分层查询接口

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/ArchitectureQueryService.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/impl/ArchitectureQueryServiceImpl.java`
- Create: `ArchMind/src/main/java/com/example/archmind/controller/ArchitectureController.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/ArchitectureQueryServiceImplTest.java`

**Interfaces:**
- Consumes: Task 6 的 resolver
- Produces: `GET /api/project/{projectId}/architecture-layers` → `Result<ArchitectureLayersResponse>`

- [ ] **Step 1: 写失败测试**

```java
class ArchitectureQueryServiceImplTest {

    @Test
    void assembleMapsRowsAndCallsIntoResolver() {
        var classes = List.of(
                Map.of("uid", "a.C", "name", "C", "role", "ENTRY", "packageName", "a.web"),
                Map.of("uid", "a.R", "name", "R", "role", "INFRASTRUCTURE", "packageName", "a.mapper"));
        var calls = List.of(Map.of("fromUid", "a.C", "toUid", "a.R", "count", 4));

        var resp = ArchitectureQueryServiceImpl.assemble(classes, calls);

        assertEquals(2, resp.classCount());
        assertEquals(1, resp.edges().size());
        assertEquals(4, resp.edges().get(0).count());
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=ArchitectureQueryServiceImplTest test`
Expected: 编译失败

- [ ] **Step 3: 实现 Cypher 与控制器**

```java
private static final String CLASS_ROWS = """
        MATCH (c:Class {projectId:$pid})
        OPTIONAL MATCH (c)-[:BELONGS_TO]->(a:BusinessArea {projectId:$pid})
        RETURN c.uid AS uid, c.name AS name, c.role AS role, c.packageName AS packageName,
               a.uid AS areaUid, a.name AS areaName
        """;

private static final String CALL_ROWS = """
        MATCH (m1:Method {projectId:$pid})-[:CALLS]->(m2:Method {projectId:$pid})
        WHERE m1.owner <> m2.owner
        RETURN m1.owner AS fromUid, m2.owner AS toUid, count(*) AS count
        """;
```

`assemble(classes, calls)` 为包级 `static`：把 Map 转成 `ClassRow`/`PairCount`，调用 `new ArchitectureLayerResolver().resolve(..., 12, 5)`。数值用 `((Number) v).intValue()`，字符串 null 原样保留。`resolve` 的 `classCount` 用 `classes.size()`。

`ArchitectureController`：

```java
@RestController
@RequestMapping("/api/project")
@RequiredArgsConstructor
public class ArchitectureController {
    private final ArchitectureQueryService architectureQueryService;
    private final CheckProjectUtil checkProjectUtil;

    @GetMapping("/{projectId}/architecture-layers")
    public Result<ArchitectureLayersResponse> layers(@PathVariable Long projectId) {
        checkProjectUtil.checkProject(projectId);
        return Result.success(architectureQueryService.architecture(projectId));
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=ArchitectureQueryServiceImplTest,ArchitectureLayerResolverTest test`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/service/domain/ArchitectureQueryService.java \
        ArchMind/src/main/java/com/example/archmind/service/impl/ArchitectureQueryServiceImpl.java \
        ArchMind/src/main/java/com/example/archmind/controller/ArchitectureController.java \
        ArchMind/src/test/java/com/example/archmind/service/impl/ArchitectureQueryServiceImplTest.java
git commit -m "feat(api): expose architecture layers"
```

---

### Task 8: 前端纯函数

**Files:**
- Create: `ArchMind-react/src/utils/architecture.js`
- Test: `ArchMind-react/src/utils/architecture.test.js`
- Modify: `ArchMind-react/src/api/domain.js`

**Interfaces:**
- Consumes: `GET /api/project/{id}/architecture-layers`
- Produces:
  - `architectureLayers(projectId)`
  - `LAYER_ORDER`, `LAYER_LABELS`
  - `normalizeLayers(data) -> { layers, edges, classCount }`
  - `groupPreview(group, max = 3) -> { names, more }`
  - `sortedEdges(edges) -> edges`

- [ ] **Step 1: 写失败测试**

```js
import test from 'node:test'
import assert from 'node:assert/strict'
import { LAYER_ORDER, normalizeLayers, groupPreview, sortedEdges } from './architecture.js'

test('补齐六层并按固定顺序', () => {
  const data = { layers: [{ key: 'other', classCount: 1, groups: [] }], classCount: 1 }
  assert.deepEqual(normalizeLayers(data).layers.map((l) => l.key), LAYER_ORDER)
})

test('分组预览最多 3 个类名并给出剩余数', () => {
  const group = { classes: [{ name: 'A' }, { name: 'B' }, { name: 'C' }, { name: 'D' }] }
  assert.deepEqual(groupPreview(group, 3), { names: ['A', 'B', 'C'], more: 1 })
})

test('边按次数降序且同层过滤由后端保证', () => {
  const edges = [{ count: 1, fromLayer: 'entry', toLayer: 'other' },
                 { count: 9, fromLayer: 'business', toLayer: 'other' }]
  assert.deepEqual(sortedEdges(edges).map((e) => e.count), [9, 1])
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind-react && node --test src/utils/architecture.test.js`
Expected: FAIL / 模块不存在

- [ ] **Step 3: 实现**

`api/domain.js` 增加：

```js
export const architectureLayers = (projectId) =>
  request(`/api/project/${projectId}/architecture-layers`)
```

`utils/architecture.js`：

```js
export const LAYER_ORDER = ['entry', 'business', 'domain-model', 'infrastructure', 'external', 'other']
export const LAYER_LABELS = {
  entry: '用户入口层', business: '业务层', 'domain-model': '领域模块层',
  infrastructure: '基础设施', external: '外部依赖', other: '未归类'
}

export function normalizeLayers(data) {
  const byKey = new Map((data?.layers || []).map((l) => [l.key, l]))
  return {
    layers: LAYER_ORDER.map((key) => byKey.get(key) || { key, name: LAYER_LABELS[key], classCount: 0, groups: [] }),
    edges: data?.edges || [],
    classCount: data?.classCount || 0
  }
}

export function groupPreview(group, max = 3) {
  const all = (group?.classes || []).map((c) => c.name)
  const total = group?.classCount ?? all.length
  const shown = Math.min(all.length, max)
  return { names: all.slice(0, max), more: Math.max(0, total - shown) }
}

export function sortedEdges(edges = []) {
  return [...edges].sort((a, b) => b.count - a.count
    || `${a.fromLayer}>${a.toLayer}`.localeCompare(`${b.fromLayer}>${b.toLayer}`))
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind-react && node --test src/utils/architecture.test.js`
Expected: PASS（3 tests）

- [ ] **Step 5: 提交**

```bash
git add ArchMind-react/src/utils/architecture.js ArchMind-react/src/utils/architecture.test.js \
        ArchMind-react/src/api/domain.js
git commit -m "feat(web): add architecture layer helpers"
```

---

### Task 9: 分层视图组件

**Files:**
- Create: `ArchMind-react/src/components/architecture/LayerBands.jsx`
- Create: `ArchMind-react/src/components/architecture/architecture.css`

**Interfaces:**
- Consumes: Task 8 的纯函数；`data` 为接口原样响应
- Produces: `<LayerBands data loading error onRetry onOpenCode />`

- [ ] **Step 1: 实现组件**

固定几何，不做 DOM 测量（避免隐藏面板 0 宽时坐标失效）：

```jsx
import { useState } from 'react'
import { normalizeLayers, sortedEdges, LAYER_LABELS } from '../../utils/architecture'
import './architecture.css'

const BAND_H = 132
const LABEL_W = 120
const GROUP_W = 184
const GROUP_H = 76
const GUTTER_W = 250

export default function LayerBands({ data, loading, error, onRetry, onOpenCode }) {
  const [selected, setSelected] = useState(null)
  const model = normalizeLayers(data)
  const edges = sortedEdges(model.edges)
  // bandY(i) = i * BAND_H + BAND_H / 2
  // gutter 路径：M0,y1 C80,y1 170,y2 250,y2，count 标在 x=125
  // 左侧为六行色带；每行 label + 横向滚动组盒；组盒显示名称、类数、最多 3 个类名
  // 点击类名 -> onOpenCode(uid)；点击边 -> setSelected(edge)，下方列出 samples
}
```

状态分支：
- `loading`：`正在读取架构分层…`
- `error`：标题 + 错误 + 重试按钮（`onRetry`）
- `!model.classCount`：`这次分析没有产出类角色`，不渲染空六层。

无障碍与文案：
- 每条边是 `<button>`，`aria-label` 为 `${fromName} 到 ${toName}，${count} 次调用`；
- 未归类层使用 `is-other` 样式，并显示一句 `未能归类的类不会被隐藏`；
- 页脚注明：`分层由类角色推导；同层调用不计入跨层边`。

- [ ] **Step 2: 样式**

`architecture.css` 只使用现有变量（`--ink-1/2/3/4`、`--rule`、`--sheet`、`--measure`、`--radius`）。布局：
- `.arch-body` 两列：`minmax(0,1fr) 250px`；
- `.arch-bands` 纵向；`.arch-band` 固定高 132px、`border-bottom: 1px solid var(--rule-soft)`；
- `.arch-groups` 单行 `overflow-x: auto`，不换行；
- `.arch-gutter` 固定宽 250px，SVG `width=250`、`height=层数*132`；
- 窄屏（`max-width: 860px`）改为单列，gutter 移到下方并给固定高度；
- 选中边、违规边、未归类分别用 `--measure`、警示色、灰色区分。

- [ ] **Step 3: 构建检查**

Run: `cd ArchMind-react && npx vite build`
Expected: 构建成功

- [ ] **Step 4: 提交**

```bash
git add ArchMind-react/src/components/architecture/
git commit -m "feat(web): render architecture layer bands"
```

---

### Task 10: 接入领域地图页

**Files:**
- Modify: `ArchMind-react/src/pages/DomainMapPage.jsx`
- Modify: `ArchMind-react/src/components/domain/domain.css`（只加切换器样式）

**Interfaces:**
- Consumes: `architectureLayers`、`LayerBands`
- Produces: 领域地图一级页出现「业务版图 / 架构分层」切换

- [ ] **Step 1: 加状态与取数**

顶部 import 增加：

```jsx
import { architectureLayers as fetchArchitectureLayers } from '../api/domain'
import LayerBands from '../components/architecture/LayerBands'
```

状态与取数：

```jsx
const [viewMode, setViewMode] = useState('map')
const [layerData, setLayerData] = useState({ status: 'idle', data: null, error: '' })

const loadLayers = useCallback(async (pid) => {
  setLayerData({ status: 'loading', data: null, error: '' })
  try {
    const data = await fetchArchitectureLayers(pid)
    setLayerData({ status: 'ready', data, error: '' })
  } catch (err) {
    setLayerData({ status: 'error', data: null, error: err?.message || '架构分层读取失败' })
  }
}, [])

useEffect(() => {
  if (viewMode === 'layers' && projectId) loadLayers(projectId)
}, [viewMode, projectId, loadLayers])
```

项目切换时重置：在现有 `useEffect`（第 106–115 行，`setAreas(null)` 那个）里追加：

```jsx
setViewMode('map')
setLayerData({ status: 'idle', data: null, error: '' })
```

- [ ] **Step 2: 渲染切换器**

在 `renderAreas` 的 `<header>` 之后加：

```jsx
<div className="dmap-tabs" role="tablist" aria-label="项目视图">
  <button role="tab" aria-selected={viewMode === 'map'} className={...} onClick={() => setViewMode('map')}>业务版图</button>
  <button role="tab" aria-selected={viewMode === 'layers'} className={...} onClick={() => setViewMode('layers')}>架构分层</button>
</div>
{viewMode === 'layers' ? (
  <LayerBands
    data={layerData.data}
    loading={layerData.status === 'loading'}
    error={layerData.status === 'error' ? layerData.error : ''}
    onRetry={() => loadLayers(projectId)}
    onOpenCode={onOpenCode}
  />
) : (
  <DomainMap areas={areas || []} onOpen={openArea} />
)}
```

`openArea` 里加 `setViewMode('map')`，保证进入二级总是看业务视角。

- [ ] **Step 3: 构建与手工验证**

Run: `cd ArchMind-react && npx vite build`
Expected: 构建成功

Run: `cd ArchMind-react && npm run dev`
Expected: 打开项目 → 领域地图 → 切「架构分层」→ 六条带、跨层边、点击类名打开源码抽屉；缩窄窗口横向滚动；未归类层可见。

- [ ] **Step 4: 提交**

```bash
git add ArchMind-react/src/pages/DomainMapPage.jsx ArchMind-react/src/components/domain/domain.css
git commit -m "feat(web): switch between domain map and architecture layers"
```

---

### Task 11: 端到端验收

**Files:**
- Modify: `记录每日工作/复杂到可探索/开发路线图.md`（勾掉两项剩余）

- [ ] **Step 1: 全量测试**

```bash
cd ArchMind && ./mvnw -q test
cd ../agent-service && ./.venv/Scripts/python.exe -m pytest tests -q
cd ../ArchMind-react && node --test src/utils/architecture.test.js && npx vite build
```

Expected: 全绿，无新增失败。

- [ ] **Step 2: 真实项目重建**

用 `demo-layered-service.zip` 新建一个项目并完整分析（走 UI 或现有上传接口）。分析完成后：

```bash
curl -s "http://localhost:8080/internal/index/status?projectId=<id>&kind=capabilities" \
  -H "X-Internal-Token: $INTERNAL_TOKEN"
```

Expected: `status=ready`，`rev` 与类索引一致，`count` = 有描述的能力数；若 `partial=true`，日志能说明跳过了几个空描述。

- [ ] **Step 3: 确认向量点**

```bash
curl -s -X POST http://localhost:6333/collections/archmind_capabilities/points/count \
  -H "Content-Type: application/json" \
  -d '{"filter":{"must":[{"key":"projectId","match":{"value":<id>}},{"key":"rev","match":{"value":<rev>}}]},"exact":true}'
```

Expected: `count` 与上一步 `count` 相同；随机取一个点，payload 的 `uid` 仍是 `POST /orders` 这类原样字符串。

- [ ] **Step 4: 问答验证**

在前端 Agent 问：`这个项目怎么下单？`

Expected：
- 步骤里出现 `retrieve(n classes, m capabilities)`；
- 回答首先落到「下单」能力，并引用真实入口类/方法；
- 引用白名单仍只来自工具 evidence，能力候选本身不出现在引用里。

- [ ] **Step 5: 分层视图验收**

打开「领域地图 → 架构分层」：

- 六层齐全，类数总和 = 项目类数；
- 入口层/业务层/领域模块层按业务领域分组，基础设施按包尾段分组；
- 点击入口层 → 基础设施的边，能看到具体类对；
- 点类名打开源码抽屉；
- 没有同层调用出现在边里；
- `other` 层如果非空，必须可见。

- [ ] **Step 6: 更新路线图并提交**

`记录每日工作/复杂到可探索/开发路线图.md`：
- R8「剩余」勾掉「领域/能力 embedding」「系统架构分层视图」；
- 「历史项目兼容」标为「不做（2026-11-25 决定）」；
- 在「已交付」补一行 Qdrant 集合名与接口路径。

```bash
git add 记录每日工作/复杂到可探索/开发路线图.md
git commit -m "docs: record R8 embedding and layer view delivery"
```

---

## 自检记录

- **Spec coverage**：R8 剩余三项 → Task 1–5（embedding）、Task 6–10（分层视图）、Task 11（验收）；历史兼容按约束不做。
- **类型一致性**：Python `CapabilityCard` 字段与 Java `CapabilityCardDTO` 一一对应；`ArchitectureLayerResolver.PairCount` 在 Task 6/7 同名；前端使用后端 `fromLayer/toLayer/classCount` 原字段。
- **Review Focus 测试归属**：uid 不透明（Task 1/2/3）、空描述（Task 3）、rev 竞态与隔离（Task 3）、检索故障隔离（Task 4）、role 为空/同层调用（Task 6）。
- **未覆盖项**：真实 Neo4j 上的 Cypher 正确性只能靠 Task 11 端到端验证；单测只覆盖装配与纯函数。
