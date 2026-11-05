# R6 第二批：工具能力补齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把路线图 R6「工具契约」表里仍标 ❌ / ⚠️ 的四项补齐：循环内二次语义检索（`search`）、字段/注解/异常（`inspect` 增强）、影响面（`impact`）、分析覆盖（`coverage`）。

**Architecture:** 四个工具互相独立，按 `search → inspect → impact → coverage` 顺序交付，每个都能单独上线。`search` 只在 Python 侧（复用现有 Retriever）；其余三个各加一个 Java `/internal/tools/*` 查询接口 + Python 契约。**不改分析流水线、不重跑历史项目**：`inspect` 走按需解析源码，`impact`/`coverage` 只用已有的 CALLS/EXTENDS/IMPLEMENTS/DEPENDS 边。

**Tech Stack:** Python 3.12 / FastAPI / httpx；Java 21 / Spring Boot 4.1 / Neo4j Java Driver / JavaParser 3.25（BLEEDING_EDGE）。

**Spec:** `记录每日工作/复杂到可探索/开发路线图.md` §R6「工具契约」+ 本文件 §缺口盘点。

## Global Constraints

- 工具契约形状不变：`handler(JavaClient, ToolContext, args) -> ToolEnvelope`，加工具 = 往 `TOOLS` 加一项。
- 参数名沿用 Java 内部接口的 camelCase；`uid` 是**类**的全限定名，方法级用 `methodUid`。
- Java 内部接口只做 `checkProjectExists`（存在性），归属校验仍在网关 —— 和其它 `/internal/tools/*` 一致。
- 所有新接口失败都返回 **HTTP 200 + `ok:false` + note**，不抛 4xx/5xx。
- 不新增 Python 依赖；Java 侧不新增依赖（JavaParser 已在 `pom.xml`）。
- 预算不变：`MAX_TOOL_CALLS=8`。新增工具只改「可选工具集」，不改刹车。

---

## 缺口盘点（2026-11-05 按代码核实）

| 路线图表中的工具 | 实际实现 | 缺什么 | 本计划 |
|---|---|---|---|
| `overview` | `overview` ✅ | — | — |
| `search` | **没有**。只有轮首预检索 + `classes` 平铺清单 | 模型在循环中**无法再次语义检索**；有 focus 时预检索还会被跳过 | Task 1 |
| `trace` | `call-chain` ✅ | 节点没有方法签名；`UP` 只反查一层 | 签名并入 Task 2；多层 UP 不在本批 |
| `inspect` | `method-body` ⚠️ 部分 | 没有字段、注解、异常；字段声明不在方法切片里 | Task 2 |
| `navigate` | `navigate` ✅ | — | — |
| `answer` / `clarify` | ✅ | — | — |
| `impact` | **没有** | 反向闭包、环检测、分层 | Task 3 |
| `coverage` | **没有**（只有内部 `/internal/describe/status`） | 面向模型的聚合视图 | Task 4 |

**两条对路线图的更正，写进文档时一并改掉：**

1. 「字段类型引用被丢掉，因为图上也没有字段依赖边」—— **后半句不成立**。`RelationResolver.resolveClass` 对字段类型确实产 `DEPENDS`（`via="field"`，`RelationResolver.java:72-78`）。真正的原因是：① `method-body` 的源码切片从方法起止行开始，**字段声明在切片之外**；② 模型没主动调 `class-relations`。所以补 `inspect.fields` 比改图更直接、也更便宜。
2. `trace` 的方法签名**不需要解析源码**：`:Method` 节点已有 `signature` / `returnType` / `visibility` 属性（`ClassSourceServiceImpl.FIND_METHODS` 已在读）。只是 `CallChainResponse` 的节点是类级的，没把它们带出来。

---

## Review Focus

1. **`search` 的候选不是证据**：检索结果只能进 `known_uids`（允许后续以它为起点），**不能**进 `evidence` / `citedUids` / `uiCommands`。复用 `models/retrieval` 的既有语义，别在工具层破例。
2. **`inspect` 按需解析要抗脏源码**：占位符、`var`、语法级别不匹配都会让 JavaParser 抛异常；失败必须降级成 `ok:false`，不能 500。
3. **`impact` 的环**：A→B→A 会让 BFS 不终止或重复膨胀；visited 必须在入队前就写入，环只记录不复访。
4. **`coverage` 的计数口径**：`described` 与 `total` 必须来自同一次 rev，否则会给出「已描述 120 / 共 100」这种自相矛盾的数字。
5. **工具数从 5 涨到 8 后 prompt 变长**：`tool_definitions()` 的顺序稳定，`classes` 与新 `search` 的 description 必须互相写清「什么时候用哪个」，否则模型会在两者之间反复试。

---

## Task 1: `search` 工具（循环内语义检索）

**Files:**
- Modify: `agent-service/app/tools/registry.py`（`ToolContext` 加 `retriever`，`TOOLS` 加 `search`）
- Modify: `agent-service/app/tools/schemas.py`
- Modify: `agent-service/app/core/agent_loop.py`（`_run_call` 透传 `retriever`）
- Test: `agent-service/tests/test_search_tool.py`（新建）

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class ToolContext:
    project_id: int
    known_uids: frozenset[str] | None = None
    retriever: Retriever | None = None          # ★ 新增

# TOOLS["search"]：needs_anchor=False
# args: {"query": str, "k": int(1..20, default 10)}
# data: list[Candidate]（uid / simpleName / packageName / kind / description / rev / score / methods）
# evidence: 每个候选一条 {"kind":"CLASS","from":<uid>}
```

- [ ] **Step 1: 写失败测试**

```python
async def test_search_requires_a_query():
    env = await execute_tool("search", FakeJava(),
                             ToolContext(project_id=7, retriever=FakeRetriever()),
                             {"query": "  "})
    assert env.ok is False

async def test_search_returns_candidates_but_no_evidence_for_citations():
    env = await execute_tool("search", FakeJava(),
                             ToolContext(project_id=7, retriever=FakeRetriever()),
                             {"query": "支付", "k": 3})
    assert env.ok and env.data[0]["uid"]
    # 候选可以成为起点，但 guard 不能把它当引用依据
    from app.core.guard import evidence_uids
    assert env.evidence            # 类级 evidence 仍要给（和 classes 一致）
```

- [ ] **Step 2: 运行确认失败**

Run: `cd agent-service && python -m pytest tests/test_search_tool.py -v`
Expected: FAIL —— `search` 不在 `TOOLS`

- [ ] **Step 3: 实现**

- `schemas.TOOL_SCHEMAS["search"]`：`query` 必填、`k` 默认 10。
- `registry._search`：`ctx.retriever is None` 或 `ctx.retriever.enabled is False` → 返回 `ToolEnvelope(ok=False, note="索引未就绪", truncated=False)`；否则 `await ctx.retriever.search(ctx.project_id, args["query"], k=args["k"])`，把 `result.candidates` 转成 dict 列表（`dataclasses.asdict` 后去掉 `methods` 不需要？保留原样即可）。
- `TOOLS["search"]` 的 description 必须写：「不知道类名、或想换一个说法再找一遍时用；已经知道起点要追关系时用 call-chain。」
- `agent_loop._run_call(..., retriever)`：构造 `ToolContext(project_id, frozenset(known), retriever)`。`_prefetch_locked_method` 不需要。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_search_tool.py tests/test_tool_specs.py tests/test_agent_loop.py -v`
Expected: PASS（`test_tool_specs.py` 若断言了工具总数，同步改成 6）

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/tools app/core/agent_loop.py agent-service/tests/test_search_tool.py agent-service/tests/test_tool_specs.py
git commit -m "feat: search 工具，循环内可再次语义检索"
```

---

## Task 2: `inspect` 增强（字段 / 注解 / 异常 + trace 签名）

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/InspectResponse.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/ast/InspectService.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/impl/InspectServiceImpl.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/controller/InternalToolController.java`
- Modify: `agent-service/app/models/tool.py`（`InspectResult` / `FieldInfo` / `MethodInfo`）
- Modify: `agent-service/app/tools/java_client.py`
- Modify: `agent-service/app/tools/registry.py` + `schemas.py`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/InspectServiceImplTest.java`、`agent-service/tests/test_inspect_tool.py`

**Interfaces:**
- Produces:

```java
public record InspectResponse(
    String uid, String name, String kind,
    List<String> annotations,
    List<FieldDTO> fields,          // name, type, visibility, isStatic, annotations
    List<MethodDTO> methods,        // uid, name, signature, returnType, visibility, annotations, exceptions
    boolean truncated) {}

// POST /internal/tools/inspect
// args: {"uid": str, "include": ["fields","annotations","exceptions"]?}
```

```python
class InspectResult(CamelModel):
    uid: str; name: str | None; kind: NodeKind | None
    annotations: list[str] = []
    fields: list[FieldInfo] = []
    methods: list[MethodInfo] = []
    truncated: bool
```

- [ ] **Step 1: 写失败测试（Java 解析层，纯字符串输入）**

```java
@Test void parses_fields_annotations_and_thrown_exceptions() {
    var svc = new InspectServiceImpl(null, null);   // 解析方法不依赖 Neo4j
    var r = svc.parse("""
        package demo;
        @Service
        public class CartServiceImpl {
            @Autowired private CartMapper cartMapper;
            @Transactional(rollbackFor = Exception.class)
            public void add(Long skuId) throws IllegalStateException { }
        }
        """);
    assertEquals(List.of("Service"), r.annotations());
    assertEquals("CartMapper", r.fields().get(0).type());
    assertEquals(List.of("Autowired"), r.fields().get(0).annotations());
    assertEquals(List.of("IllegalStateException"), r.methods().get(0).exceptions());
}
@Test void syntax_error_degrades_to_empty_result() { ... }
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=InspectServiceImplTest test`
Expected: FAIL —— 类不存在

- [ ] **Step 3: 实现**

- `InspectServiceImpl`：`SourceSliceService` 取类源码文本 → `new JavaParser(new ParserConfiguration().setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE)).parse(text)` → 取第一个 `ClassOrInterfaceDeclaration`。字段上限 100、方法上限 100，超出置 `truncated`。任何 `ParseProblemException` → 返回空结果 + `note`（`ToolEnvelope` 层包装成 `ok:false`）。**不写图**。
- `InternalToolController.inspect`：和其它工具同构（`checkProjectExists` → `clampLimit` → `ToolEnvelope.success(data, evidence, truncated)`），evidence 用类 uid 一条 `CLASS`。
- `CallChainResponse` 的 `ChainNode` 加 `methodSignature?: string`（Node 是类级，签名取该类「入口方法」的 signature；没有就 null）。**只加字段，不改查询语义。**
- Python：`JavaClient.inspect(project_id, uid, include)`；`TOOLS["inspect"]` 的 `needs_anchor=True`，schema `include` 枚举 `fields/annotations/exceptions`，默认三项全开。
- `method-body` 的 description 补一句：「要看字段、注解、异常时改用 inspect。」

- [ ] **Step 4: 运行通过**

Run: `./mvnw -q test`
Run: `cd agent-service && python -m pytest tests/test_inspect_tool.py tests/test_tool_specs.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src agent-service/app/models/tool.py agent-service/app/tools agent-service/tests/test_inspect_tool.py
git commit -m "feat: inspect 工具补齐字段/注解/异常"
```

---

## Task 3: `impact` 工具（反向闭包 + 环检测）

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/ImpactResponse.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/ast/ImpactService.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/impl/ImpactServiceImpl.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/controller/InternalToolController.java`
- Modify: `agent-service/app/models/tool.py`（`Impact` / `ImpactNode`）
- Modify: `agent-service/app/tools/java_client.py`、`registry.py`、`schemas.py`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/ImpactCycleTest.java`、`agent-service/tests/test_impact_tool.py`

**Interfaces:**
- Produces：

```java
public record ImpactResponse(
    ClassNodeDTO center,
    List<ImpactNode> affected,   // uid, name, kind, depth, via (CALLS/DEPENDS/IMPLEMENTS/EXTENDS)
    List<ImpactEdge> edges,      // from, to, rel
    List<List<String>> cycles,   // 最多 20 条，每条是闭环上的 uid 顺序
    boolean truncated) {}

// POST /internal/tools/impact  args: {"uid": str, "maxDepth": 1..4, "maxNodes": 1..200}
```

- [ ] **Step 1: 写失败测试（环检测是纯函数）**

```java
@Test void detects_cycle_without_revisiting() {
    // A→B, B→C, C→A 的邻接表
    var r = ImpactCycle.find(Map.of("A", List.of("B"), "B", List.of("C"), "C", List.of("A")), "A");
    assertEquals(List.of(List.of("A", "B", "C", "A")), r);
}
@Test void ignores_self_loop_duplicates() { ... }
```

- [ ] **Step 2: 运行确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=ImpactCycleTest test`
Expected: FAIL

- [ ] **Step 3: 实现**

- `ImpactServiceImpl`：逐层 BFS（与 `CallChainServiceImpl` 同构，**不用变长 Cypher**）。每层的查询是「入边」：
  - `CALLS`：`MATCH (m:Method)-[:CALLS]->(t:Method) WHERE t.owner IN $uids`
  - `DEPENDS` / `IMPLEMENTS` / `EXTENDS`：类级入边
  - 出队前 `visited.add`；命中 visited 且不是上一层父节点 → 记一条环。
  - 深度硬上限 4，节点硬上限 200，超限 `truncated=true`。
- `evidence`：每条影响边一条 `EDGE`（`from/去 to/rel`），与 call-chain 一致。
- Python：`TOOLS["impact"]` 的 description：「问『改这里会影响谁 / 谁依赖它』时用；问执行顺序用 call-chain。」

- [ ] **Step 4: 运行通过**

Run: `./mvnw -q test`
Run: `cd agent-service && python -m pytest tests/test_impact_tool.py tests/test_tool_specs.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src agent-service/app/models/tool.py agent-service/app/tools agent-service/tests/test_impact_tool.py
git commit -m "feat: impact 工具，反向闭包与环检测"
```

---

## Task 4: `coverage` 工具（分析覆盖聚合）

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/CoverageResponse.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/ast/CodeGraphQueryService.java` + `impl/CodeGraphQueryServiceImpl.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/controller/InternalToolController.java`
- Modify: `agent-service/app/models/tool.py`、`java_client.py`、`registry.py`、`schemas.py`
- Test: `agent-service/tests/test_coverage_tool.py`

**Interfaces:**
- Produces：

```java
public record CoverageResponse(
    int rev, int classes, int methods, int described, int pending,
    int isolatedClasses,                       // 无任何边（入+出）的类
    Map<String, Integer> edges) {}             // CALLS / EXTENDS / IMPLEMENTS / DEPENDS

// GET /internal/tools/coverage?projectId=
```

- [ ] **Step 1: 写失败测试（Python 契约）**

```python
async def test_coverage_renders_counts():
    env = await execute_tool("coverage", FakeJava(), ToolContext(project_id=7), {})
    assert env.ok
    assert env.data["classes"] >= env.data["described"]
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_coverage_tool.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

- 一条 Cypher 拿类/方法/孤立类计数（`COUNT { (c)--() } = 0`），一条拿边计数按类型分组；`described/pending` 复用 `ClassDescriptionService.descriptionStatus`。**两处必须同一 rev**：先读 `graphRevService`，两段查询都带 rev 过滤，rev 在两次查询之间变化就重来一次（最多 2 次），否则返回 `ok:false`。
- description 写：「问『这个项目分析到了什么程度、哪里没覆盖』时用；不要用它回答问题本身。」

- [ ] **Step 4: 运行通过**

Run: `./mvnw -q test && cd agent-service && python -m pytest tests/test_coverage_tool.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src agent-service/app agent-service/tests/test_coverage_tool.py
git commit -m "feat: coverage 工具，暴露分析覆盖"
```

---

## 验证方式

| 层 | 验证 |
|---|---|
| 单测 | 每个工具至少一条 args 校验 + 一条 happy path；`impact` 另有环检测纯函数测试 |
| 契约 | `test_tool_specs.py` 断言工具集 = `{overview, classes, class-relations, call-chain, method-body, search, inspect, impact, coverage}` |
| 端到端 | 在 archmind 项目上问三类问题各一次：「改 UserService 会影响谁」（impact）、「CartMapper 这个字段怎么注入的」（inspect）、「哪些地方没被分析到」（coverage） |
| 回归 | `run_ab_eval.py --dataset r6_golden.json` 的 citation 越界数不得上升 |

## 明确不做

- **注解 / 字段写进图**：那要改分析流水线并回填历史项目，成本远高于按需解析，收益相同。
- **`trace` 的多层 UP**：反向闭包由 `impact` 承担，`call-chain.UP` 保持单层（再往上会迅速发散到全项目）。
- **`impact` 的分层规则**（入口层/业务层/基础设施的过滤）依赖 R8 的架构分层，等 R8。
