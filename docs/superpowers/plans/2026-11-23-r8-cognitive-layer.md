# R8 认知层实现计划（项目 → 业务领域 → 能力 → 代码）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把代码图升级成业务地图——每个项目自动产出「业务领域 → 能力 → 代码」三级结构，节点全部可追溯到真实类，并提供查询接口。

**Architecture:** 新增一个分析阶段 `DOMAIN`，从 Neo4j 读回基础图（类、注解、调用边、入口），用三个纯函数组件依次算出 role、业务领域、能力，写回派生节点与边，最后跑五条对账断言；描述阶段只消费已有类描述，不重读源码。

**Tech Stack:** Java 17 / Spring Boot 3 / JavaParser / Neo4j Java Driver / Spring AI ChatClient / JUnit 5。

**Spec:** `记录每日工作/复杂到可探索/开发路线图.md`（R8 节）+ 本文件 §1–§3 为已确认的方案。

## 范围

**本计划包含：** 注解抽取、入口节点、role 属性、业务领域、能力、对账、查询接口、领域与能力描述。

**不在本计划（单独立计划）：**
- 领域/能力 embedding 与 Qdrant 索引（依赖 R4 基础设施，独立可验收）
- 前端领域地图渲染（依赖本计划的 REST 契约，独立可验收）
- MCP 的 `list_domains` / `get_domain` / `list_capabilities` / `get_capability_code`（R9）

## 1. 设计思路

**一个项目 = 一个域。** 不做领域归并，树只有三级：业务领域 → 能力 → 代码。域这一层前端补一个根即可，不建节点。

**结构先算，描述后写。** role、入口、领域、能力全部由规则和调用图算出，零模型调用；描述是派生物，放在结构定型之后批量生成。结构一变描述全废，所以顺序不能倒。

**两套信息，两种形态。**
- 业务结构 = 树（领域 → 能力 → 类），类可以属于多个能力；
- 角色 = 类上的一个属性（六选一），全覆盖、互斥。

**能力 = 一个入口 + 它真正执行到的类。** 入口是 HTTP 路由、定时任务、MQ 监听等外部触发点；从入口方法沿 CALLS 向下展开，遇到另一个业务领域或外部依赖就停，并记录跨域边与断点。能力与入口 1:1。

**能在代码里对上的，就不让模型猜。** 模型只负责写描述和（必要时）起中文名。

## 2. 数据模型

### 节点

| 标签 | uid 示例 | 属性 |
|---|---|---|
| `:Endpoint:CodeNode` | `POST /orders` | `httpMethod`、`path`、`handlerUid`、`kind`（http/scheduled/mq/event/manual）、`inferred`；唯一键是 `(projectId, uid, handlerUid)`，同一路径被多个类实现时各存一份 |
| `:BusinessArea:CodeNode` | `item` / `infrastructure` | `name`、`kind`（business/infrastructure）、`description` |
| `:Capability:CodeNode` | `POST /orders` | `name`、`areaUid`、`entryUid`、`description` |
| `:Class:CodeNode`（已有，加字段） | 全限定名 | 新增 `annotationNames`、`role`、`roleBasis` |

### 边

| 边 | 方向 | 属性 |
|---|---|---|
| `HANDLED_BY` | Endpoint → Method | — |
| `BELONGS_TO` | Class → BusinessArea | — |
| `IN_AREA` | Capability → BusinessArea | — |
| `PART_OF_CAPABILITY` | Class → Capability | `depth`（从入口出发的最短跳数） |
| `AREA_CALLS` | BusinessArea → BusinessArea | `count`、`fromUid`、`toUid` |

### role 枚举

`entry` / `business` / `domain-model` / `infrastructure` / `external` / `other`。
`roleBasis`：`annotation` / `package` / `name` / `supertype` / `package-majority` / `none`。

## 3. 实现流程

```
AST 阶段（已有，增强）
  解析注解 → 计算 Endpoint → 写 Class.annotationNames + Endpoint 节点 + HANDLED_BY
        ↓
DOMAIN 阶段（新增，纯脚本）
  读快照 → role 全量分类 → 业务领域划分 → 能力生成 → 写派生节点/边 → 五条断言
        ↓
DOMAIN_DESCRIBE 阶段（新增，模型）
  读领域/能力/成员类已有描述 → 批量写 description
        ↓
查询接口（新增）
  GET /domain-map、GET /capability
```

## Global Constraints

- 只解析 Java；非 Java 文件不进图（`pom.xml`、`application.yml`、`Dockerfile` 只留在文件表）。
- 所有新增节点必须同时带 `:CodeNode` 标签，否则 rebuild 的 `MATCH (n:CodeNode {projectId})` 清不掉。
- 节点唯一键统一 `(projectId, uid)`；业务领域与能力也遵守。
- `Endpoint` 与 `Capability` 1:1；`Capability.uid == Endpoint.uid`。
- role 单值、六枚举、兜底 `other`；`roleBasis` 必填。
- 业务领域锚点顺序：顶级包 → 形态判定 → 第二段 → 单领域兜底；不做 LLM 聚类。
- 基础设施全局一个（`uid = infrastructure`），`kind = infrastructure`，不产出能力。
- 描述阶段不读源码，只消费类节点上已有的 `description`。
- 断言 1–4 任一失败即抛错、不写半成品；断言 5（业务领域没有能力）只记 warning，不阻断落库。
- 任务进度区间：SCAN 0–5、AST 5–45、OVERVIEW 45–58、DESCRIBE 58–75、DOMAIN 75–88、DOMAIN_DESCRIBE 88–100。

## Review Focus

1. **方法级注解与类级前缀的组合**：多路径数组、空路径、`/` 与 `//` 归一化、`@RequestMapping` 同时出现在类和方法上；用户期望每个真实 URL 各成一个入口。
2. **技术分层单体的退化**：包名里没有业务信息时（如 ArchMind 自己），必须稳定产出 1 个业务领域，而不是抛错或产生空领域。
3. **跨领域调用**：`OrderService → ItemService` 必须让"下单"能力在跨域处停止，下游类不能被吞进本能力，但要留一条 `AREA_CALLS`。
4. **环与重载**：互相调用的两个类不能让 BFS 死循环；方法 uid 必须用 `owner#signature`，重载不撞键。
5. **重建残留**：重建后不能留下上一次的 `BusinessArea` / `Capability` / `Endpoint` 孤儿节点。

---

## Task 1: 注解抽取

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/model/ast/ParsedAnnotation.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/model/ast/ParsedClass.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/model/ast/ParsedMethod.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/impl/AstParserServiceImpl.java:130-235`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/AstParserServiceImplTest.java`

**Interfaces:**
- Produces: `record ParsedAnnotation(String name, List<String> values)`；`ParsedClass.getAnnotations()`、`ParsedMethod.getAnnotations()`（默认空列表）。

- [ ] **Step 1: 写失败测试**

```java
@Test
void extractsClassAndMethodAnnotations() {
    String src = """
        package demo;
        @RestController
        @RequestMapping({"/orders", "/o"})
        public class OrderController {
            @PostMapping("/{id}/cancel")
            public void cancel() {}
        }
        """;
    ParsedProject p = parse(src);
    ParsedClass c = p.getFiles().get(0).getClasses().get(0);
    assertEquals(List.of("RestController", "RequestMapping"),
            c.getAnnotations().stream().map(ParsedAnnotation::name).toList());
    assertEquals(List.of("/orders", "/o"), c.getAnnotations().get(1).values());
    assertEquals(List.of("PostMapping"),
            c.getMethods().get(0).getAnnotations().stream().map(ParsedAnnotation::name).toList());
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=AstParserServiceImplTest#extractsClassAndMethodAnnotations test`
Expected: 编译失败，`getAnnotations` 不存在。

- [ ] **Step 3: 实现**

`ParsedAnnotation` 用 record。`ParsedClass` / `ParsedMethod` 各加 `private List<ParsedAnnotation> annotations = new ArrayList<>();`。
在 `AstParserServiceImpl` 新增 `private List<ParsedAnnotation> annotationsOf(NodeWithAnnotations<?> node)`：遍历 `node.getAnnotations()`，`name` 取 `getNameAsString()`，`values` 取注解里所有 `StringLiteralExpr` 的值（保持源码顺序，去重）。在 `extractClass`、`extractEnum`、`extractMethod`、`extractConstructor` 里各调用一次。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=AstParserServiceImplTest#extractsClassAndMethodAnnotations test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/model/ast \
        ArchMind/src/main/java/com/example/archmind/service/impl/AstParserServiceImpl.java \
        ArchMind/src/test/java/com/example/archmind/service/impl/AstParserServiceImplTest.java
git commit -m "feat(ast): extract class and method annotations"
```

---

## Task 2: 入口解析（EndpointResolver）

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/model/ast/ParsedEndpoint.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/ast/EndpointResolver.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/ast/EndpointResolverTest.java`

**Interfaces:**
- Consumes: `ParsedAnnotation`、`ParsedProject`（Task 1）。
- Produces: `record ParsedEndpoint(String uid, String kind, String httpMethod, String path, String handlerUid, boolean inferred)`；`List<ParsedEndpoint> EndpointResolver.resolve(ParsedProject project)`。

- [ ] **Step 1: 写失败测试**

```java
@Test
void composesClassPrefixWithMethodPath() {
    ParsedProject p = project(
        "@RestController @RequestMapping(\"/orders\") class OrderController {"
        + " @PostMapping(\"/{id}/cancel\") void cancel() {}"
        + " @GetMapping void list() {} }");
    List<ParsedEndpoint> eps = new EndpointResolver().resolve(p);
    assertEquals(List.of("POST /orders/{id}/cancel", "GET /orders/list"),
            eps.stream().map(ParsedEndpoint::uid).toList());
    assertEquals(List.of("POST", "GET"), eps.stream().map(ParsedEndpoint::httpMethod).toList());
    assertTrue(eps.get(1).inferred());
}

@Test
void expandsMultiplePathsIntoMultipleEndpoints() {
    ParsedProject p = project(
        "@RestController @RequestMapping({\"/a\", \"/b\"}) class C {"
        + " @GetMapping({\"/x\", \"/y\"}) void list() {} }");
    List<String> uids = new EndpointResolver().resolve(p).stream().map(ParsedEndpoint::uid).toList();
    assertEquals(List.of("GET /a/x", "GET /a/y", "GET /b/x", "GET /b/y"), uids.stream().sorted().toList());
}

@Test
void mapsScheduledToNonHttpEndpoint() {
    ParsedProject p = project("class Job { @Scheduled(\"0 0 * * *\") void run() {} }");
    ParsedEndpoint ep = new EndpointResolver().resolve(p).get(0);
    assertEquals("scheduled", ep.kind());
    assertEquals("SCHEDULED " + ep.handlerUid(), ep.uid());
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=EndpointResolverTest test`
Expected: 编译失败，`EndpointResolver` 不存在。

- [ ] **Step 3: 实现**

规则：
- 类级 `RequestMapping` 的 values 当前缀（无值视为 `""`）。
- 方法级 `GetMapping/PostMapping/PutMapping/DeleteMapping/PatchMapping` → 对应动词；方法级 `RequestMapping` → 动词 `ANY`。
- 路径 = 前缀 + 方法路径；`//` 归一成 `/`，末尾去 `/`（根路径保留 `/`）。多个路径拆成多个 `ParsedEndpoint`，uid = `动词 + 空格 + 路径`。
- 方法级映射但没有路径值：路径取 `"/" + 方法名`，`inferred = true`。
- 非 HTTP：`@Scheduled` → `SCHEDULED {handlerUid}`；`@RabbitListener`/`@KafkaListener` → `MQ {handlerUid}`；`@EventListener` → `EVENT {handlerUid}`；两类都没有但实现 `CommandLineRunner`/`ApplicationRunner` → `MANUAL {handlerUid}`。`handlerUid` = `ParsedMethod.getOwnerQualifiedName() + "#" + getSignature()`。
- 一个方法同时有 HTTP 与调度注解时，只取 HTTP。
- **兜底**：某个类没有任何带注解的入口，但类名以 `Controller/Resource/Action` 结尾或包名含 `controller/web/rest`，则把它的 `public` 非静态方法各产一个 `MANUAL {handlerUid}` 入口（`inferred = true`）。没有这条，无 Spring 注解的项目会产出 0 个能力。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=EndpointResolverTest test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/model/ast/ParsedEndpoint.java \
        ArchMind/src/main/java/com/example/archmind/service/ast/EndpointResolver.java \
        ArchMind/src/test/java/com/example/archmind/service/ast/EndpointResolverTest.java
git commit -m "feat(ast): resolve HTTP and non-HTTP entry endpoints"
```

---

## Task 3: 写入口节点与注解名

**Files:**
- Modify: `ArchMind/src/main/java/com/example/archmind/model/ast/ParsedProject.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/impl/AstParserServiceImpl.java`（parse 末尾挂上入口）
- Modify: `ArchMind/src/main/java/com/example/archmind/dao/neo4j/CodeGraphNeo4jWriter.java:67-110,180-260`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/AstParserServiceImplTest.java`
- Test: `ArchMind/src/test/java/com/example/archmind/dao/neo4j/CodeGraphNeo4jWriterTest.java`（新建）

**Interfaces:**
- Consumes: `EndpointResolver.resolve(ParsedProject)`（Task 2）。
- Produces: `ParsedProject.getEndpoints()`；Neo4j 中出现 `:Endpoint:CodeNode` 与 `HANDLED_BY`。

- [ ] **Step 1: 写失败测试**

```java
@Test
void populatesEndpointsOnParsedProject() {
    ParsedProject p = parse(
        "@RestController @RequestMapping(\"/orders\") class C { @PostMapping(\"/x\") void create() {} }");
    assertEquals(List.of("POST /orders/x"),
            p.getEndpoints().stream().map(ParsedEndpoint::uid).toList());
}
```

```java
@Test
void classRowsCarryAnnotationNamesAndEndpointRowsAreShaped() {
    CodeGraphNeo4jWriter writer = new CodeGraphNeo4jWriter(null);
    ParsedProject p = parse("@RestController class C { @GetMapping(\"/x\") void list() {} }");
    assertEquals(List.of("RestController"), writer.classRows(1L, p).get(0).get("annotationNames"));
    assertEquals("GET /x", writer.endpointRows(1L, p).get(0).get("uid"));
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=AstParserServiceImplTest,CodeGraphNeo4jWriterTest test`
Expected: 编译失败（`getEndpoints`、`classRows`、`endpointRows` 尚不存在）。

- [ ] **Step 3: 实现**

`ParsedProject` 加 `List<ParsedEndpoint> endpoints`，`parse()` 末尾 `project.setEndpoints(new EndpointResolver().resolve(project))`。
`CodeGraphNeo4jWriter`：
- `MERGE_CLASS` 增加 `c.annotationNames = row.annotationNames`（`List<String>`，只存注解名）。
- 新增 `MERGE_ENDPOINT`（`MERGE (e:Endpoint:CodeNode {projectId, uid}) SET e.kind, e.httpMethod, e.path, e.handlerUid, e.inferred`）与 `MERGE_HANDLED_BY`（`MATCH (e:Endpoint {projectId, uid}) MATCH (m:Method {projectId, uid: row.handlerUid}) MERGE (e)-[:HANDLED_BY]->(m)`）。
- 写入顺序：类 → 方法 → 边 → 入口（入口最后，保证 handler 已存在）。
- 为可测性，把 `classRows` 与新增的 `endpointRows` 改为包级私有（去掉 `private`），测试同包直接调用；不改公开行为。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=AstParserServiceImplTest,CodeGraphNeo4jWriterTest test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/model/ast/ParsedProject.java \
        ArchMind/src/main/java/com/example/archmind/service/impl/AstParserServiceImpl.java \
        ArchMind/src/main/java/com/example/archmind/dao/neo4j/CodeGraphNeo4jWriter.java \
        ArchMind/src/test/java/com/example/archmind/service/impl/AstParserServiceImplTest.java \
        ArchMind/src/test/java/com/example/archmind/dao/neo4j/CodeGraphNeo4jWriterTest.java
git commit -m "feat(graph): persist endpoints and annotation names"
```

---

## Task 4: 图快照读取

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/GraphSnapshot.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/GraphSnapshotDao.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/domain/GraphSnapshotDaoTest.java`

**Interfaces:**
- Produces:

```java
public record ClassSnapshot(String uid, String name, String packageName,
                            List<String> annotationNames, List<String> interfaces, String superclass) {}
public record CallEdgeSnapshot(String fromMethodUid, String toMethodUid) {}
public record EndpointSnapshot(String uid, String kind, String handlerUid) {}
public record GraphSnapshot(List<ClassSnapshot> classes,
                            List<CallEdgeSnapshot> calls,
                            List<EndpointSnapshot> endpoints) {}
GraphSnapshot GraphSnapshotDao.load(Long projectId);
static GraphSnapshot assemble(List<Map<String,Object>> classRows,
                              List<Map<String,Object>> callRows,
                              List<Map<String,Object>> endpointRows);  // 包级私有，纯函数
```

- [ ] **Step 1: 写失败测试**

```java
@Test
void assembleToleratesMissingProperties() {
    var classRows = List.of(Map.of("uid", "a.A", "name", "A", "packageName", "a"));
    var s = GraphSnapshotDao.assemble(classRows, List.of(), List.of());
    assertEquals(1, s.classes().size());
    assertEquals(List.of(), s.classes().get(0).annotationNames());
    assertEquals("", s.classes().get(0).superclass());
}

@Test
void assembleKeepsOrderAndShapesCalls() {
    var classRows = List.of(
        Map.of("uid", "a.B", "name", "B", "packageName", "a"),
        Map.of("uid", "a.A", "name", "A", "packageName", "a"));
    var callRows = List.of(Map.of("from", "a.A#x()", "to", "a.B#y()"));
    var s = GraphSnapshotDao.assemble(classRows, callRows, List.of());
    assertEquals(List.of("a.A", "a.B"), s.classes().stream().map(ClassSnapshot::uid).toList());
    assertEquals("a.B#y()", s.calls().get(0).toMethodUid());
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=GraphSnapshotDaoTest test`
Expected: 编译失败。

- [ ] **Step 3: 实现**

三条查询（都用 `projectId` 过滤）：类、CALLS（`MATCH (m:Method)-[r:CALLS]->(n:Method)`）、Endpoint。`load` 只负责跑查询并把 `record.asMap()` 交给 `assemble`；所有字段容错（缺失返回空列表/空串），按 `uid` 排序保证顺序稳定。`interfaces` / `superclass` 目前节点上没有，先返回默认值，后续任务再补。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=GraphSnapshotDaoTest test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/service/domain \
        ArchMind/src/test/java/com/example/archmind/service/domain/GraphSnapshotDaoTest.java
git commit -m "feat(domain): load graph snapshot for domain modeling"
```

---

## Task 5: role 分类

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/Role.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/RoleClassifier.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/domain/RoleClassifierTest.java`

**Interfaces:**
- Consumes: `List<ClassSnapshot>`（Task 4）。
- Produces: `enum Role { ENTRY, BUSINESS, DOMAIN_MODEL, INFRASTRUCTURE, EXTERNAL, OTHER }`；`record Classified(String uid, Role role, String basis)`；`Map<String, Classified> classify(List<ClassSnapshot> classes)`。

- [ ] **Step 1: 写失败测试**

```java
@Test
void prefersAnnotationOverPackageAndName() {
    var classes = List.of(
        cls("a.controller.OrderController", "controller", List.of("RestController"), List.of(), ""),
        cls("a.common.RedisUtil", "a.common", List.of(), List.of(), ""),
        cls("a.dto.Order", "a.dto", List.of(), List.of(), ""),
        cls("a.weird.Thing", "a.weird", List.of(), List.of(), "")
    );
    var r = new RoleClassifier().classify(classes);
    assertEquals(Role.ENTRY, r.get("a.controller.OrderController").role());
    assertEquals("annotation", r.get("a.controller.OrderController").basis());
    assertEquals(Role.INFRASTRUCTURE, r.get("a.common.RedisUtil").role());
    assertEquals(Role.DOMAIN_MODEL, r.get("a.dto.Order").role());
    assertEquals(Role.OTHER, r.get("a.weird.Thing").role());
}

@Test
void usesSupertypeBeforePackage() {
    var r = new RoleClassifier().classify(List.of(
        cls("a.thing.Custom", "a.thing", List.of(), List.of("BaseMapper"), "")));
    assertEquals(Role.INFRASTRUCTURE, r.get("a.thing.Custom").role());
    assertEquals("supertype", r.get("a.thing.Custom").basis());
}

@Test
void packageMajorityOnlyWhenSampleIsLargeEnough() {
    var enough = new RoleClassifier().classify(List.of(
        cls("a.x.AUserService", "a.x", List.of(), List.of(), ""),
        cls("a.x.BUserService", "a.x", List.of(), List.of(), ""),
        cls("a.x.CUserService", "a.x", List.of(), List.of(), ""),
        cls("a.x.Mystery", "a.x", List.of(), List.of(), "")));
    assertEquals(Role.BUSINESS, enough.get("a.x.Mystery").role());
    assertEquals("package-majority", enough.get("a.x.Mystery").basis());

    var tooFew = new RoleClassifier().classify(List.of(
        cls("b.y.AUserService", "b.y", List.of(), List.of(), ""),
        cls("b.y.Mystery", "b.y", List.of(), List.of(), "")));
    assertEquals(Role.OTHER, tooFew.get("b.y.Mystery").role());
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=RoleClassifierTest test`
Expected: 编译失败。

- [ ] **Step 3: 实现**

五级顺序，首个命中即停，记 `basis`：
1. 注解表：`RestController/Controller/ControllerAdvice/Scheduled/RabbitListener/KafkaListener/EventListener` → ENTRY；`Service` → BUSINESS；`Mapper/Repository/Configuration/ConfigurationProperties/Aspect` → INFRASTRUCTURE；`Entity/TableName` → DOMAIN_MODEL；`FeignClient` → EXTERNAL。
2. 父类/接口：`implements` 含 `Mapper`/`BaseMapper` → INFRASTRUCTURE；`extends` 含 `Controller` → ENTRY；`extends` 含 `Exception`/`RuntimeException` → INFRASTRUCTURE。
3. 包名段：见下表的词表。
4. 类名后缀：`*Controller` → ENTRY；`*Service/*ServiceImpl/*Manager` → BUSINESS；`*Mapper/*Dao/*Repository` → INFRASTRUCTURE；`*Config/*Util/*Utils/*Helper/*Exception/*Interceptor/*Filter/*Aspect/*Constants` → INFRASTRUCTURE；`*Client/*Feign` → EXTERNAL；`*Entity/*PO/*DO/*DTO/*VO/*Query/*Command` → DOMAIN_MODEL。
5. 同包多数派：直接包下**至少有 3 个已确定的类**，且其中 ≥70% 是同一个 role，则该包里的 `OTHER` 跟随这个 role，basis 记 `package-majority`；样本不足则保持 `OTHER`。

包名段词表与类名后缀一致（`controller/web/rest`、`service/biz/usecase/application`、`domain/model/entity/po/vo/dto/bo`、`mapper/dao/repository/infra/config/common/util/exception/aspect/filter/interceptor`、`client/feign/remote/gateway`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=RoleClassifierTest test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/service/domain/Role.java \
        ArchMind/src/main/java/com/example/archmind/service/domain/RoleClassifier.java \
        ArchMind/src/test/java/com/example/archmind/service/domain/RoleClassifierTest.java
git commit -m "feat(domain): classify class roles by annotation, package and name"
```

---

## Task 6: 业务领域划分

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/BusinessArea.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/BusinessAreaResolver.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/domain/BusinessAreaResolverTest.java`

**Interfaces:**
- Consumes: `List<ClassSnapshot>`、`Map<String, Classified>`。
- Produces:

```java
public record BusinessArea(String uid, String name, String kind) {}
public record AreaResult(List<BusinessArea> areas,
                         Map<String, String> classToArea,
                         String strategy) {}  // business-modules | technical-layered | single
AreaResult resolve(List<ClassSnapshot> classes, Map<String, Classified> roles, String projectName);
```

- [ ] **Step 1: 写失败测试**

```java
@Test
void businessPackagesBecomeAreas() {
    var classes = List.of(
        cls("com.hmall.item.ItemService", "com.hmall.item"), cls("com.hmall.item.ItemController", "com.hmall.item"),
        cls("com.hmall.trade.TradeService", "com.hmall.trade")
    );
    var r = new BusinessAreaResolver().resolve(classes, Map.of(), "hmall");
    assertEquals("business-modules", r.strategy());
    assertEquals(List.of("item", "trade"), r.areas().stream().filter(a -> a.kind().equals("business")).map(BusinessArea::uid).sorted().toList());
    assertEquals("trade", r.classToArea().get("com.hmall.trade.TradeService"));
}

@Test
void technicalLayeredProjectFallsBackToSingleArea() {
    var classes = List.of(
        cls("com.example.archmind.controller.C", "com.example.archmind.controller"),
        cls("com.example.archmind.service.S", "com.example.archmind.service")
    );
    var r = new BusinessAreaResolver().resolve(classes, Map.of(), "ArchMind");
    assertEquals("single", r.strategy());
    assertEquals(List.of("ArchMind"), r.areas().stream().filter(a -> a.kind().equals("business")).map(BusinessArea::name).toList());
}

@Test
void skipsTechnicalSegmentToFindBusinessKey() {
    var classes = List.of(
        cls("com.acme.backend.controller.OrderController", "com.acme.backend.controller"),
        cls("com.acme.backend.cart.CartService", "com.acme.backend.cart"));
    var r = new BusinessAreaResolver().resolve(classes, Map.of(), "acme");
    assertEquals("technical-layered", r.strategy());
    assertEquals(List.of("cart"), r.areas().stream()
            .filter(a -> a.kind().equals("business")).map(BusinessArea::uid).toList());
    assertEquals("infrastructure", r.classToArea().get("com.acme.backend.controller.OrderController"));
}

@Test
void infrastructureHoldsInfraRolesAndUnassigned() {
    var classes = List.of(
        cls("com.hmall.item.ItemService", "com.hmall.item"),
        cls("com.hmall.common.RedisUtil", "com.hmall.common"));
    var roles = Map.of("com.hmall.common.RedisUtil", new Classified("com.hmall.common.RedisUtil", Role.INFRASTRUCTURE, "package"));
    var r = new BusinessAreaResolver().resolve(classes, roles, "hmall");
    assertEquals("infrastructure", r.classToArea().get("com.hmall.common.RedisUtil"));
    assertEquals(classes.size(), r.classToArea().size());
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=BusinessAreaResolverTest test`
Expected: 编译失败。

- [ ] **Step 3: 实现**

1. 计算所有包名的公共前缀（按段），去掉得到 remainder。
2. 对每个包，从左往右找**第一个不在技术词表里的段**，作为业务 key；全被跳过则 key 为空。
3. 技术词表（只用于领域划分，不影响 role）：`controller web rest api service biz usecase application domain model entity po vo dto bo mapper dao repository impl infra infrastructure config common util utils exception aspect filter filters interceptor client feign remote gateway fallback core base shared support query router routers`。
   只有**最终会落到业务领域**的类才产生 key；role 为 `INFRASTRUCTURE`/`OTHER` 的类即使有 key 也不建领域，否则会出现「有 key 但一个类都没有」的空领域。
4. 所有 key 都为空 → `strategy = single`，全部非基础设施类归一个业务领域（uid = `main`，name = 项目名）。
5. 存在非空 key → 每个不同的 key 一个业务领域（uid/name = key）；至少有一个类跳过过技术段时 `strategy = technical-layered`，否则 `business-modules`。
6. `infrastructure` 领域（`kind = infrastructure`）收三类：role 为 `INFRASTRUCTURE` 或 `OTHER` 的类、key 为空的类、包名不在任何业务领域下的类；如果它一个类都没有，就不创建。
7. 业务领域按 uid 排序，`infrastructure` 放最后；`classToArea` 必须覆盖全部类。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=BusinessAreaResolverTest test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/service/domain/BusinessArea.java \
        ArchMind/src/main/java/com/example/archmind/service/domain/BusinessAreaResolver.java \
        ArchMind/src/test/java/com/example/archmind/service/domain/BusinessAreaResolverTest.java
git commit -m "feat(domain): derive business areas from package structure"
```

---

## Task 7: 能力生成

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/Capability.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/CapabilityResolver.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/domain/CapabilityResolverTest.java`

**Interfaces:**
- Consumes: `List<EndpointSnapshot>`、`Map<String,String> classToArea`、`Map<String,Classified> roles`、`List<CallEdgeSnapshot>`。
- Produces:

```java
public record Member(String classUid, int depth) {}
public record AreaCall(String fromAreaUid, String toAreaUid, String fromClassUid, String toClassUid) {}
public record Capability(String uid, String name, String areaUid, String entryUid, List<Member> members) {}
public record CapabilityResult(List<Capability> capabilities, List<AreaCall> areaCalls, List<String> gaps) {}
CapabilityResult resolve(List<EndpointSnapshot> endpoints, Map<String,String> classToArea,
                         Map<String,Classified> roles, List<CallEdgeSnapshot> calls);
```

- [ ] **Step 1: 写失败测试**

```java
@Test
void stopsAtAnotherBusinessAreaAndRecordsCrossAreaCall() {
    var calls = List.of(
        edge("demo.OrderController#create()", "demo.OrderService#create()"),
        edge("demo.OrderService#create()", "demo.ItemService#deduct()"),
        edge("demo.OrderService#create()", "demo.PayClient#pay()"));
    var areas = Map.of("demo.OrderController", "order", "demo.OrderService", "order",
                       "demo.ItemService", "item");
    var roles = Map.of("demo.PayClient", new Classified("demo.PayClient", Role.EXTERNAL, "name"));
    var eps = List.of(new EndpointSnapshot("POST /orders", "http", "demo.OrderController#create()"));

    var r = new CapabilityResolver().resolve(eps, areas, roles, calls);
    Capability cap = r.capabilities().get(0);
    assertEquals(List.of("demo.OrderService"), cap.members().stream().map(Member::classUid).toList());
    assertEquals(1, r.areaCalls().size());
    assertEquals("item", r.areaCalls().get(0).toAreaUid());
    assertEquals(1, r.gaps().size());
}

@Test
void survivesCycles() {
    var calls = List.of(edge("a.A#x()", "a.B#y()"), edge("a.B#y()", "a.A#x()"));
    var r = new CapabilityResolver().resolve(
        List.of(new EndpointSnapshot("GET /a", "http", "a.A#x()")),
        Map.of("a.A", "x", "a.B", "x"), Map.of(), calls);
    assertEquals(1, r.capabilities().size());
}

@Test
void infrastructureMembersAreIncludedButDoNotStopTraversal() {
    var calls = List.of(
        edge("o.C#create()", "o.S#create()"),
        edge("o.S#create()", "o.M#insert()"),
        edge("o.M#insert()", "o.X#more()"));
    var areas = Map.of("o.C", "order", "o.S", "order", "o.M", "infrastructure", "o.X", "order");
    var r = new CapabilityResolver().resolve(
        List.of(new EndpointSnapshot("POST /o", "http", "o.C#create()")),
        areas, Map.of(), calls);
    var members = r.capabilities().get(0).members().stream().map(Member::classUid).toList();
    assertTrue(members.contains("o.M"));
    assertTrue(members.contains("o.X"));
    assertTrue(r.areaCalls().isEmpty());
}

@Test
void infrastructureEntryProducesNoCapability() {
    var r = new CapabilityResolver().resolve(
        List.of(new EndpointSnapshot("SCHEDULED j.J#run()", "scheduled", "j.J#run()")),
        Map.of("j.J", "infrastructure"), Map.of(), List.of());
    assertTrue(r.capabilities().isEmpty());
}

@Test
void memberDepthIsShortestHop() {
    var calls = List.of(
        edge("a.A#x()", "a.B#y()"),
        edge("a.A#x()", "a.C#z()"),
        edge("a.C#z()", "a.B#y()"));
    var r = new CapabilityResolver().resolve(
        List.of(new EndpointSnapshot("GET /a", "http", "a.A#x()")),
        Map.of("a.A", "x", "a.B", "x", "a.C", "x"), Map.of(), calls);
    var b = r.capabilities().get(0).members().stream()
        .filter(m -> m.classUid().equals("a.B")).findFirst().orElseThrow();
    assertEquals(1, b.depth());
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=CapabilityResolverTest test`
Expected: 编译失败。

- [ ] **Step 3: 实现**

按 Endpoint 逐个 BFS：
- 起点 = `handlerUid`；`owner(methodUid) = uid.split("#")[0]`。
- 起点类必进成员，`depth = 0`。
- 展开 CALLS，按**先 role、后领域**的顺序判断：
  - 目标类 role 为 `EXTERNAL` → 记一条 gap 并停止展开该分支；
  - 否则目标领域是**另一个业务领域**（`classToArea` 有值、不等于入口领域、且不是 `infrastructure`）→ 记 `AreaCall` 并停止展开该分支；
  - **基础设施领域的类照常加入成员、继续展开**（`OrderService → OrderMapper → …` 不能在下沉处断掉）；
  - 其余（同领域，或不在 `classToArea` 里）→ 加入成员并继续。
- 访问集合按**方法 uid** 去重（防环）；成员按类 uid 去重，depth 取最短。
- `Capability.uid = Endpoint.uid`，`name = endpoint.uid`，`areaUid = classToArea(entryClass)`。
- 入口类若属于 `infrastructure` 领域，跳过该入口（基础设施不产能力）。
- 输出按 uid 排序。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=CapabilityResolverTest test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/service/domain/Capability.java \
        ArchMind/src/main/java/com/example/archmind/service/domain/CapabilityResolver.java \
        ArchMind/src/test/java/com/example/archmind/service/domain/CapabilityResolverTest.java
git commit -m "feat(domain): build capabilities from endpoints and call graph"
```

---

## Task 8: 对账断言与写库

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/DomainModel.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/DomainModelValidator.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/DomainModelWriter.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/domain/DomainModelValidatorTest.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/domain/DomainModelWriterTest.java`

**Interfaces:**
- Consumes: Task 5/6/7 的产物。
- Produces:

```java
public record DomainModel(List<BusinessArea> areas, List<Capability> capabilities,
                          List<AreaCall> areaCalls, Map<String,String> classToArea,
                          Map<String,Classified> roles) {}
public record ValidationReport(boolean ok, List<String> failures) {}

ValidationReport DomainModelValidator.validate(DomainModel model, int totalClasses);
record DomainWritePlan(...6 个 List<Map<String,Object>>...) {}   // 包级私有
DomainWritePlan DomainModelWriter.plan(Long projectId, DomainModel model);  // 包级私有、纯函数
void DomainModelWriter.write(Long projectId, DomainModel model);
static List<String> DomainModelWriter.WRITE_ORDER;                // 包级私有，断言先删后写
```

- [ ] **Step 1: 写失败测试**

```java
@Test
void failsWhenAClassHasNoArea() {
    var model = new DomainModel(List.of(new BusinessArea("item","item","business")),
        List.of(), List.of(), Map.of("a.A", "item"), Map.of("a.A", new Classified("a.A", Role.BUSINESS, "package")));
    var r = new DomainModelValidator().validate(model, 2);
    assertFalse(r.ok());
    assertTrue(r.failures().stream().anyMatch(f -> f.contains("BELONGS_TO")));
}

@Test
void failsWhenBusinessAreaHasNoCapability() {
    var model = new DomainModel(
        List.of(new BusinessArea("item","item","business")),
        List.of(), List.of(),
        Map.of("a.A", "item"),
        Map.of("a.A", new Classified("a.A", Role.BUSINESS, "package")));
    var r = new DomainModelValidator().validate(model, 1);
    assertFalse(r.ok());
    assertTrue(r.failures().stream().anyMatch(f -> f.contains("capability")));
}

@Test
void passesOnConsistentModel() {
    var model = new DomainModel(
        List.of(new BusinessArea("item","item","business")),
        List.of(new Capability("POST /i","POST /i","item","a.C#x()",
                List.of(new Member("a.C", 0)))),
        List.of(), Map.of("a.C", "item"),
        Map.of("a.C", new Classified("a.C", Role.ENTRY, "annotation")));
    assertTrue(new DomainModelValidator().validate(model, 1).ok());
}
```

```java
@Test
void planEmitsClassAreaRoleAndMemberRows() {
    var writer = new DomainModelWriter(null);
    var plan = writer.plan(1L, consistentModel());
    assertEquals(1, plan.areas().size());
    assertEquals(1, plan.classAreas().size());
    assertEquals("annotation", plan.roles().get(0).get("basis"));
    assertEquals(0, plan.members().get(0).get("depth"));
}

@Test
void writeOrderDeletesBeforeMerge() {
    assertTrue(DomainModelWriter.WRITE_ORDER.get(0).contains("DETACH DELETE"));
    assertTrue(DomainModelWriter.WRITE_ORDER.get(1).contains("DETACH DELETE"));
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=DomainModelValidatorTest,DomainModelWriterTest test`
Expected: 编译失败。

- [ ] **Step 3: 实现**

五条断言，失败信息带具体 uid：
1. `roles.size() == totalClasses`，且每个 role 非 null。
2. `classToArea` 覆盖全部类；`capabilities` 的 `areaUid` 必须在 `areas` 里；`entryUid` 非空。
3. 每个 capability 的 `areaUid` 恰好一个（结构上天然成立，仍校验非空）。
4. 每个 area 的类集合非空。
5. 每个 `kind=business` 的 area 至少有一个 capability——**只记 warning**：这表示该领域没有可识别的入口，不是数据不一致，不应让整个分析失败。

写库（`DomainModelWriter`）：
- 先 `delete` 旧派生节点：`MATCH (a:BusinessArea {projectId:$pid}) DETACH DELETE a` 与 `MATCH (c:Capability {projectId:$pid}) DETACH DELETE c`；`Endpoint` 由 AST 重建负责。
- 再依次：
  - `MERGE (a:BusinessArea:CodeNode {projectId, uid}) SET a.name, a.kind`
  - `MATCH (c:Class {projectId, uid}) MATCH (a:BusinessArea {projectId, uid: row.areaUid}) MERGE (c)-[:BELONGS_TO]->(a)`
  - `SET c.role = row.role, c.roleBasis = row.basis`
  - `MERGE (cap:Capability:CodeNode {projectId, uid}) SET cap.name, cap.areaUid, cap.entryUid`
  - `MERGE (cap)-[:IN_AREA]->(a)`
  - `MERGE (c)-[:PART_OF_CAPABILITY {depth}]->(cap)`
  - `MERGE (from:BusinessArea)-[e:AREA_CALLS]->(to:BusinessArea) SET e.count = coalesce(e.count,0) + 1, e.fromUid, e.toUid`
- 把上述语句按执行顺序放进包级私有的 `WRITE_ORDER`，供测试断言"先删后写"。
- 所有行构造放进包级私有纯函数 `plan(DomainModel)`，`write()` 只负责按 `WRITE_ORDER` 逐条执行 + 参数绑定；不连库也能测行内容。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=DomainModelValidatorTest,DomainModelWriterTest test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/service/domain \
        ArchMind/src/test/java/com/example/archmind/service/domain/DomainModelValidatorTest.java \
        ArchMind/src/test/java/com/example/archmind/service/domain/DomainModelWriterTest.java
git commit -m "feat(domain): validate and persist domain model"
```

---

## Task 9: 接入分析流水线

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/DomainModelService.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/impl/DomainModelServiceImpl.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/common/constant/AnalysisStage.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/AnalysisPipelineService.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/impl/AnalysisPipelineServiceImpl.java:27-51`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/task/AnalysisTaskExecutor.java:25-60`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/AnalysisPipelineServiceImplTest.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/DomainModelServiceImplTest.java`

**Interfaces:**
- Consumes: Task 4–8。
- Produces: `void DomainModelService.build(Long projectId, ProgressReporter reporter)`；`AnalysisStage.DOMAIN`。

- [ ] **Step 1: 写失败测试**

```java
@Test
void domainStageRunsBuildAndReportsFull() {
    var reporter = new RecordingReporter();
    pipeline.domainStage(1L, reporter);
    verify(domainModelService).build(eq(1L), any());
    assertEquals(100, reporter.last());
}
```

```java
@Test
void buildWritesModelWhenValidationPasses() {
    stubSnapshotAndResolvers();
    when(validator.validate(any(), eq(1))).thenReturn(new ValidationReport(true, List.of()));
    service.build(1L, pct -> {});
    verify(writer).write(eq(1L), any());
}

@Test
void buildThrowsWhenValidationFails() {
    stubSnapshotAndResolvers();
    when(validator.validate(any(), eq(1))).thenReturn(new ValidationReport(false, List.of("BELONGS_TO: a.A")));
    assertThrows(BusinessException.class, () -> service.build(1L, pct -> {}));
    verify(writer, never()).write(anyLong(), any());
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=AnalysisPipelineServiceImplTest,DomainModelServiceImplTest test`
Expected: 编译失败，`domainStage` 不存在。

- [ ] **Step 3: 实现**

- `AnalysisStage` 重排进度区间并新增 `DOMAIN("业务建模", 75, 100)`（Task 11 会拆成 75–88 / 88–100）；同步修改现有断言旧区间的测试。
- `DomainModelServiceImpl.build`：`load → classify → resolveArea → resolveCapability → validate → write`；每步 `reporter.report(...)` 按 0/20/45/70/90/100 上报；校验失败抛 `BusinessException`（已有）。
- `AnalysisPipelineService` 增加 `domainStage`；实现类委托。
- `AnalysisTaskExecutor` 在 `DESCRIBE` 之后插入 `DOMAIN`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=AnalysisPipelineServiceImplTest test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind \
        ArchMind/src/test/java/com/example/archmind/service/impl/AnalysisPipelineServiceImplTest.java
git commit -m "feat(pipeline): add DOMAIN analysis stage"
```

---

## Task 10: 领域与能力查询接口

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/DomainMapResponse.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/CapabilityDetailResponse.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/DomainQueryService.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/impl/DomainQueryServiceImpl.java`
- Create: `ArchMind/src/main/java/com/example/archmind/controller/DomainMapController.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/DomainQueryServiceImplTest.java`

**Interfaces:**
- Produces:

```java
record DomainMapResponse(List<AreaDto> areas) {}
record AreaDto(String uid, String name, String kind, String description, List<CapabilityDto> capabilities) {}
record CapabilityDto(String uid, String name, String entryUid, int memberCount) {}
record CapabilityDetailResponse(CapabilityDto capability, List<MemberDto> members, List<AreaCallDto> areaCalls) {}
record MemberDto(String uid, String name, int depth, String role) {}
record AreaCallDto(String toAreaUid, String toAreaName, int count) {}

DomainMapResponse domainMap(Long projectId);
CapabilityDetailResponse capability(Long projectId, String uid);
static DomainMapResponse assembleMap(List<Map<String,Object>> rows);                        // 包级私有
static CapabilityDetailResponse assembleDetail(Map<String,Object> capRow,
        List<Map<String,Object>> memberRows, List<Map<String,Object>> areaCallRows);        // 包级私有

GET /api/project/{projectId}/domain-map  -> Result<DomainMapResponse>
GET /api/project/{projectId}/capability?uid=POST%20/orders -> Result<CapabilityDetailResponse>
```

> 断点（gaps）暂不进响应：它目前只在能力生成阶段产出、没有挂到 Capability 节点，接口给不出。等需要时再加"每能力的断点"字段。

- [ ] **Step 1: 写失败测试**

```java
@Test
void assembleMapGroupsCapabilitiesUnderArea() {
    List<Map<String, Object>> rows = List.of(
        Map.of("areaUid", "item", "areaName", "item", "kind", "business", "description", "商品管理",
               "capUid", "GET /items", "capName", "GET /items", "entryUid", "a.C#l()", "memberCount", 2L),
        Map.of("areaUid", "item", "areaName", "item", "kind", "business", "description", "商品管理",
               "capUid", "POST /items", "capName", "POST /items", "entryUid", "a.C#c()", "memberCount", 3L));
    var resp = DomainQueryServiceImpl.assembleMap(rows);
    assertEquals(1, resp.areas().size());
    assertEquals(2, resp.areas().get(0).capabilities().size());
    assertEquals("GET /items", resp.areas().get(0).capabilities().get(0).uid());
}

@Test
void assembleMapKeepsAreaWithoutCapability() {
    List<Map<String, Object>> rows = List.of(
        Map.of("areaUid", "infrastructure", "areaName", "infrastructure", "kind", "infrastructure", "description", ""));
    var resp = DomainQueryServiceImpl.assembleMap(rows);
    assertEquals(1, resp.areas().size());
    assertTrue(resp.areas().get(0).capabilities().isEmpty());
}

@Test
void assembleDetailSortsMembersByDepth() {
    Map<String, Object> cap = Map.of("uid", "POST /orders", "name", "POST /orders", "entryUid", "o.C#c()");
    List<Map<String, Object>> members = List.of(
        Map.of("uid", "o.S", "name", "S", "depth", 1L, "role", "business"),
        Map.of("uid", "o.C", "name", "C", "depth", 0L, "role", "entry"));
    var resp = DomainQueryServiceImpl.assembleDetail(cap, members, List.of());
    assertEquals(List.of("o.C", "o.S"), resp.members().stream().map(MemberDto::uid).toList());
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=DomainQueryServiceImplTest test`
Expected: 编译失败。

- [ ] **Step 3: 实现**

`load` 只跑查询、把 `record.asMap()` 交给纯函数；行映射逻辑全部在 `assembleMap` / `assembleDetail` 里，可以脱库测试。
`domainMap` 查询（一条 Cypher）：
```cypher
MATCH (a:BusinessArea {projectId:$pid})
OPTIONAL MATCH (cap:Capability {projectId:$pid})-[:IN_AREA]->(a)
OPTIONAL MATCH (c:Class {projectId:$pid})-[:PART_OF_CAPABILITY]->(cap)
RETURN a.uid AS areaUid, a.name AS areaName, a.kind AS kind, a.description AS description,
       cap.uid AS capUid, cap.name AS capName, cap.entryUid AS entryUid,
       count(c) AS memberCount
ORDER BY a.uid, cap.uid
```
`assembleMap`：按 areaUid 分组；`capUid` 为空的行只产出区域、不产出能力；能力按 uid 排序。
`capability` 分两段：能力节点 + 成员（`(c:Class)-[r:PART_OF_CAPABILITY]->(cap)`，取 `r.depth`、`c.role`），以及本区域出去的 `AREA_CALLS`（`to.uid`、`to.name`、`e.count`）。`assembleDetail` 按 depth、uid 排序成员。
控制器沿用 `CheckProjectUtil.checkProject(projectId)`；`uid` 用 query param，避免路径里的空格与斜杠。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=DomainQueryServiceImplTest test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/dto/response \
        ArchMind/src/main/java/com/example/archmind/service/domain/DomainQueryService.java \
        ArchMind/src/main/java/com/example/archmind/service/impl/DomainQueryServiceImpl.java \
        ArchMind/src/main/java/com/example/archmind/controller/DomainMapController.java \
        ArchMind/src/test/java/com/example/archmind/service/impl/DomainQueryServiceImplTest.java
git commit -m "feat(api): expose domain map and capability detail"
```

---

## Task 11: 领域与能力描述

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/domain/DomainDescriptionService.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/impl/DomainDescriptionServiceImpl.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/AnalysisPipelineService.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/impl/AnalysisPipelineServiceImpl.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/task/AnalysisTaskExecutor.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/DomainDescriptionServiceImplTest.java`

**Interfaces:**
- Consumes: `ChatModel`（同 `ClassDescriptionServiceImpl`）、领域/能力/成员类与 `c.description`。
- Produces:

```java
public interface DomainDescriptionService {
    void describe(Long projectId, ProgressReporter reporter);
}
// impl 内部的可测缝隙（包级私有）：
String callModel(String prompt);                              // 唯一的网络出口，测试可替换
DomainDescriptionBatch parse(String json);                    // 纯函数，解析失败返回空
List<Map<String,Object>> areaRows(DomainDescriptionBatch b, Set<String> knownAreaUids);
List<Map<String,Object>> capabilityRows(DomainDescriptionBatch b, Set<String> knownCapUids);

record DomainDescriptionBatch(List<AreaDesc> areas, List<CapDesc> capabilities) {}
record AreaDesc(String uid, String description) {}
record CapDesc(String uid, String description) {}
```

- [ ] **Step 1: 写失败测试**

```java
@Test
void parseDropsBlankDescriptions() {
    String json = """
        {"areas":[{"uid":"item","description":"负责商品查询与上架。"},
                  {"uid":"cart","description":"  "}],
         "capabilities":[{"uid":"GET /items","description":"按分类查商品。"}]}
        """;
    var b = service.parse(json);
    assertEquals(List.of("item"), b.areas().stream().map(AreaDesc::uid).toList());
    assertEquals(1, b.capabilities().size());
}

@Test
void rowsOnlyKeepKnownUids() {
    var b = new DomainDescriptionBatch(
        List.of(new AreaDesc("item", "商品"), new AreaDesc("ghost", "编的")),
        List.of(new CapDesc("GET /items", "查商品"), new CapDesc("GET /nope", "编的")));
    var areaRows = service.areaRows(b, Set.of("item"));
    var capRows = service.capabilityRows(b, Set.of("GET /items"));
    assertEquals(1, areaRows.size());
    assertEquals("商品", areaRows.get(0).get("description"));
    assertEquals(1, capRows.size());
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=DomainDescriptionServiceImplTest test`
Expected: 编译失败。

- [ ] **Step 3: 实现**

用一条 Cypher 取 `BusinessArea → Capability → 成员类(name, description)`，按领域分批（每批 ≤8 个领域）拼提示词：领域名、kind、能力列表（name + entryUid）、每个能力成员类的 name 与 description（缺失写"无描述"）。
提示词约束：2–3 句、中文、不得发明代码里没有的能力、不得复述类名清单。
输出 JSON 用上面的 `DomainDescriptionBatch` 结构（显式 uid，比序号稳）。`parse` 用 Jackson，异常返回空批次；只写回 uid 在库里的行；`SET a.description = row.description` / `SET cap.description = row.description`。
`callModel` = `ChatClient.create(chatModel).prompt(prompt).call().content()`。
进度按 0/50/100 上报。`AnalysisStage` 拆成 `DOMAIN("业务建模", 75, 88)` 与 `DOMAIN_DESCRIBE("领域描述", 88, 100)`；执行器在 DOMAIN 后调用 `domainDescriptionStage`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=DomainDescriptionServiceImplTest test`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind \
        ArchMind/src/test/java/com/example/archmind/service/impl/DomainDescriptionServiceImplTest.java
git commit -m "feat(domain): generate area and capability descriptions"
```

---

## 改动总览

| 类型 | 文件 | 说明 |
|---|---|---|
| 新增 | `model/ast/ParsedAnnotation.java` | 注解 record |
| 新增 | `model/ast/ParsedEndpoint.java` | 入口 record |
| 新增 | `service/ast/EndpointResolver.java` | 类前缀 + 方法路径拼装 |
| 新增 | `service/domain/{Role,RoleClassifier}.java` | 六格角色分类 |
| 新增 | `service/domain/{BusinessArea,BusinessAreaResolver}.java` | 业务领域划分 |
| 新增 | `service/domain/{Capability,CapabilityResolver}.java` | 能力生成 |
| 新增 | `service/domain/{DomainModel,DomainModelValidator,DomainModelWriter}.java` | 校验与写库 |
| 新增 | `service/domain/{GraphSnapshot,GraphSnapshotDao}.java` | 图快照 |
| 新增 | `service/domain/{DomainModelService,DomainQueryService,DomainDescriptionService}.java` + impl | 编排与查询 |
| 新增 | `controller/DomainMapController.java` | 领域地图接口 |
| 新增 | `dto/response/{DomainMapResponse,CapabilityDetailResponse}.java` | 接口契约 |
| 修改 | `model/ast/{ParsedClass,ParsedMethod,ParsedProject}.java` | 注解与入口字段 |
| 修改 | `service/impl/AstParserServiceImpl.java` | 抽注解 |
| 修改 | `dao/neo4j/CodeGraphNeo4jWriter.java` | 注解名、入口节点、派生节点与边 |
| 修改 | `service/impl/CodeGraphServiceImpl.java` | 写入口 |
| 修改 | `common/constant/AnalysisStage.java` | 新增两级阶段、重排进度 |
| 修改 | `service/AnalysisPipelineService.java` + impl | 新增阶段方法 |
| 修改 | `service/task/AnalysisTaskExecutor.java` | 插入 DOMAIN / DOMAIN_DESCRIBE |

## 验收

1. `./mvnw test` 全绿。
2. 在 hmall 上跑完整分析：`domain-map` 返回 5 个左右业务领域（item/cart/trade/pay/user + infrastructure），每个有能力的领域至少 3 个能力。
3. 在 ArchMind 自己上跑：返回 1 个业务领域 + 1 个基础设施领域，不抛错、不产生空领域。
4. 随机抽 20 个类，人工核对 `role` 与所属领域，错分 ≤ 2。
5. 任取一个能力，`capability` 接口返回的每个成员 uid 都能在 `:Class` 中查到；跨域调用出现在 `areaCalls` 里。
6. 重建两次，`:BusinessArea` / `:Capability` 数量不变（无残留）。
