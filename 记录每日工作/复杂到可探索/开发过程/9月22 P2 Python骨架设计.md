# 9月22 P2 Python 骨架设计（agent-service）

> 承接 [9月19 Agent v1设计](9月19%20Agent%20v1设计.md) §9 的 P2 执行步骤与 §10 的目录规划；
> 验收预期见 [9月20 第一阶段问答边界](9月20%20第一阶段问答边界.md) §5。
> 9/19 定「Agent 放在哪、怎么协作」；**本篇定 P2 这一段代码长什么样** —— 契约字段、目录结构、错误映射、验收步骤。
> **本篇是设计，不含代码改动。**

---

## 0. 今天拍板了什么

| # | 决定 | 依据 |
| --- | --- | --- |
| 1 | 服务落位 = **同仓库并列目录** `agent-service/` | 设计 §10 的 ⏸ 待定项，今天关掉 |
| 2 | 联调后端 = **本地 `mvn spring-boot:run`**，`JAVA_BASE_URL=http://localhost:8080` | 开发期改 Java 代码免打包 |
| 3 | 先打通 **`overview` + `call-chain`** 两个工具 | 一个不碰图、一个碰图，两条 code path 全覆盖；且 `call-chain` 是核心场景，早暴露早好 |

---

## 1. P2 的边界

**P2 = 契约全定义 + 2 个工具真跑。**

这条线要划清楚，否则很容易把 P3 的活拖进来：

| | P2 做 | 为什么这么切 |
| --- | --- | --- |
| **契约模型** | ✅ **五个工具的出入参模型全部定义好** | §4-④「Pydantic 模型 = 契约唯一真相」。契约是 Java↔Python 的传输面，跟 LLM 无关，越早钉死越好 |
| **工具执行** | ✅ 2 个（`overview` / `call-chain`）真跑通 | §9 验收：`curl /internal/agent/ask`，**不调 LLM**，只返回 tool 结果 |
| **行为** | ❌ Intent 判定 / ReAct 循环 / 四道拦截 | P3 |
| **状态** | ❌ Redis Context / viewport / ack | P4 |
| **网关** | ❌ Java 侧 `/api/agent/*` | P4 |
| **容器** | ❌ Dockerfile / compose 第三服务 | P6 |

> ⚠️ **`java_client` 要把 5 个工具全封装完**（它只是个客户端，写全成本很低），
> 但 `/internal/agent/ask` 只**接线** 2 个。别把「封装完」误解成「P2 要给 5 个工具写编排」。

---

## 2. 服务落位与目录结构

`agent-service/` 与 Java 的 `ArchMind/` 并列，**用同一个 git 仓库**。

```
D:\ArchMind\ArchMind\
├── ArchMind/              Java（Maven）
├── agent-service/         ★ 新建：Python（FastAPI）
│   ├── app/
│   │   ├── main.py              FastAPI 入口 + /health
│   │   ├── config.py            pydantic-settings 读环境变量
│   │   ├── api/agent.py         /internal/agent/* 路由
│   │   ├── models/
│   │   │   ├── tool.py          ★ 五个工具的入参/载荷/信封
│   │   │   ├── response.py      ★ AgentResponse / UICmd / Suggestion / Step
│   │   │   └── protocol.py      ★ Java ↔ Python 传输 DTO
│   │   └── tools/java_client.py httpx 封 Java 的 5 个接口
│   ├── tests/                   ★ 契约测试（A1 防线）
│   ├── requirements.txt
│   ├── .env.example
│   └── .gitignore               .venv/ __pycache__/ .env
├── deploy/
└── 记录每日工作/
```

**P2 先不建的目录**（对照设计 §10 的完整版）：

| 不建 | 留给 |
| --- | --- |
| `models/context.py`、`models/intent.py` | P3 / P4 |
| `core/`（loop / intent / prompt / guard） | P3 |
| `tools/registry.py` | P3（要 LLM 决定调什么时才需要注册表） |
| `store/context_store.py` | P4 |
| `Dockerfile` | P6 |

**理由**：目录结构是给人看的「还剩什么没做」，先把空盒子建出来会让人误以为进度到了那儿。

---

## 3. ★ 契约字段全表

> **这些字段不是从设计文档抄的，是从 Java DTO 逐个读出来的。**
> 设计文档 §3 只定义了信封的**字段名**，五个工具的 `data` 载荷形状**只存在于 Java 代码里** ——
> 这正是 P2 存在的意义，也是 A1「契约漂移」的发生地。
>
> 字段名一律 **camelCase**，与 Java 完全一致，**不做 Python 风格转换**。

### 3.1 信封 `ToolEnvelope`（5 个工具共用）

对应 `dto/response/ToolEnvelope.java`。

```jsonc
{
  "ok": true,                 // ★ false 时 data 为 null、note 有原因；HTTP 仍是 200
  "data": { /* 工具专属载荷，见 §3.4 */ },
  "evidence": [ /* Evidence[]，见 §3.2 */ ],
  "truncated": false,         // ★ 被截断必须 true
  "note": null,               // 失败原因 / 降级说明
  "graphRev": 7               // ★ Long；0 表示「没有过带版本号的写图」，不是错误
}
```

```python
class ToolEnvelope(BaseModel, Generic[T]):
    ok: bool
    data: T | None = None
    evidence: list[Evidence] = []
    truncated: bool = False
    note: str | None = None
    graphRev: int = 0        # Java Long → Python int
```

### 3.2 `Evidence`

对应 `dto/response/Evidence.java`。**它是 grounding 的抓手**，不是给人看的注释。

```jsonc
{
  "kind": "EDGE",              // EDGE / CLASS / METHOD / FILE
  "from": "com.x.OrderService",// 类全限定名 或 方法 uid（owner#signature）
  "to": "com.x.InventoryService", // kind=CLASS/FILE 时可为空
  "rel": "CALLS",              // CALLS / EXTENDS / IMPLEMENTS / DEPENDS
  "count": 3                   // Integer，同端点之间的聚合条数
}
```

> ⚠️ **`class-relations` 的 evidence 方向会翻转**：控制器按 `upstream` 标志决定
> `from`/`to` 谁在左（`related → center` 还是 `center → related`）。
> Python 用它做 citation 校验时，**别假设 `from` 一定是中心类**。

### 3.3 五个工具的入参（`args` key 全表）

入参统一 `{"projectId": <Long>, "args": {...}}`。`projectId` 由 Java 注入，**Python 不产生、LLM 不可见**。

| 工具 | `args` key | 必填 | 默认 | 硬上限 | 合法取值 |
| --- | --- | --- | --- | --- | --- |
| `overview` | — | — | — | — | 不接受任何 args |
| `classes` | `limit` | | 40 | **200** | 正整数 |
| `class-relations` | `uid` | ✅ | | | 类全限定名 |
| | `direction` | | `BOTH` | | `UP` / `DOWN` / `BOTH` |
| | `limit` | | 40 | **200** | 正整数 |
| `call-chain` | `uid` | ✅ | | | 类全限定名 |
| | `methodUid` | | null | | `owner#signature` |
| | `direction` | | `DOWN` | | `DOWN` / `UP`（**没有 BOTH**） |
| | `maxDepth` | | 2 | **3** | 正整数 |
| | `maxNodes` | | 40 | **200** | 正整数 |
| `method-body` | `uid` | 二选一 | | | 类全限定名 |
| | `methodUid` | 二选一 | | | `owner#signature` |
| | `maxMethods` | | 3 | **3** | 正整数 |
| | `maxLines` | | 120 | **120** | 正整数 |

> ⚠️ **`call-chain` 的 `direction` 只有 DOWN / UP**，传 `BOTH` 会被拒（`ok:false`）。
> 它与 `class-relations` 的枚举**不是一个东西**，别共用类型定义 —— 见 §6-⑥。

### 3.4 五个工具的 `data` 载荷

#### ① `overview` → `ProjectOverviewResponse`

```jsonc
{
  "projectType": "...",
  "summary": "...",
  "description": "...",
  "techStack": [ { "name", "category", "version", "role" } ],
  "architecture": { "style", "layers": ["..."], "description" },
  "modules": [ { "name", "responsibility", "keyFiles": ["..."] } ]
}
```

> 数据源是 `project_overview` 表，**不碰图** —— 未落图的项目也能答（C2 的降级路径）。
> ⚠️ `modules` 是 LLM 看 pom+README 编的**假节点**（设计 §8-B5），V1 只当 prompt 背景，不给用户点。

#### ② `classes` → `ClassNodeDTO[]`

```jsonc
[ {
  "qualifiedName": "com.x.OrderService",  // = 图中节点 uid
  "simpleName": "OrderService",
  "packageName": "com.x",
  "kind": "CLASS",                        // CLASS / INTERFACE / ENUM
  "description": "一句话简介",              // ★ 可为 null（非核心类没有）
  "filePath": "src/.../OrderService.java",
  "startLine": 12,
  "endLine": 88
} ]
```

> 控制器多取一条（`limit + 1`）来判断 `truncated`，所以实际最多返回 `limit` 条。

#### ③ `class-relations` → `ClassRelationsResponse`

```jsonc
{
  "center": ClassNodeDTO,
  "upstream":   { "callers": [RCD], "dependents": [RCD], "subclasses": [RCD] },
  "downstream": { "callees": [RCD], "dependencies": [RCD], "parents": [RCD] }
}
// RCD = RelatedClassDTO
{ "clazz": ClassNodeDTO,
  "relationType": "CALLS",     // CALLS / EXTENDS / IMPLEMENTS / DEPENDS
  "evidence": ["handleLogin(String,String) → login(String,String)"] }  // 给人看的短句
```

> ⚠️ **两套 evidence 别搞混**：`RelatedClassDTO.evidence` 是 `List<String>`（**人话短句**），
> 顶层信封的 `evidence` 是 `List[Evidence]`（**结构化凭据**）。名字一样，类型完全不同。

#### ④ `call-chain` → `CallChainResponse` ★ 核心场景

```jsonc
{
  "start": ClassNodeDTO,                    // UP / DOWN 都会填
  "nodes": [ { "uid", "name", "kind", "module", "depth", "viaFanout", "edgeCount" } ],
  "edges": [ { "from", "to", "rel", "callCount", "viaFanout" } ],
  "gaps":  [ { "at": "com.x.OrderService", "reason": "INTERFACE_NO_IMPLEMENTATION" } ],
  "truncated": false
}
```

| 字段 | 语义 |
| --- | --- |
| `nodes[].depth` | 距起点跳数，起点为 0；**扇出得到的实现类与它替代的接口同深度** |
| `nodes[].viaFanout` | true = 这个类是通过「接口 → 实现类」扇出得到的（不算一层 depth） |
| `nodes[].module` | 从 filePath 推导。**让 LLM 能看出调用是否跨了模块／跨了服务** |
| `nodes[].edgeCount` | 相关的方法级边条数（自环不计），按**方法级行**累计 |
| `edges[].rel` | `CALLS`（普通调用）或 `IMPLEMENTS`（扇出时替换了接口） |
| `edges[].callCount` | `CALLS` = 底层方法级调用边条数；`IMPLEMENTS` = **配对成功的接口方法数**（诊断信号） |
| `gaps[].reason` | 见下表 |

`gaps[].reason` 的取值（`GapReason`）：

| 值 | 含义 | V1 是否产出 |
| --- | --- | --- |
| `REMOTE_SERVICE` | 目标是 `-api` 模块／`.client` 包的接口，项目内无实现 → **调到另一个服务了** | ✅ |
| `INTERFACE_NO_IMPLEMENTATION` | 接口在项目内无实现类（典型：MyBatis Mapper，实现是运行时代理） | ✅ |
| `NO_DOWNSTREAM_EDGE` | 保留值 | ❌ **不产出** |
| `EXTERNAL_DEPENDENCY` | 保留值（项目外 jar 的边压根不建，图上无从检出） | ❌ **不产出** |

#### ⑤ `method-body` → `MethodBodyResponse` ★ 讲解深度的唯一来源

```jsonc
{
  "methods": [ {
    "uid": "com.x.OrderServiceImpl#createOrder(OrderDTO)",
    "signature": "createOrder(OrderDTO)",
    "owner": "com.x.OrderServiceImpl",
    "startLine": 45,            // ★ 从第一个注解算起（@Override/@Transactional 会被包含，javadoc 不会）
    "endLine": 78,
    "code": "...",
    "error": null,              // 源码读不到时为字符串，原因见顶层 note
    "resolvedFrom": null        // ★ 请求接口方法但自动跳了实现类时，填原来的接口方法 uid
  } ],
  "truncated": false,
  "note": null                  // 降级说明
}
```

> `resolvedFrom` 是关键：给的是**接口方法 uid** 时，Java 顺 `IMPLEMENTS + signature` 自动跳到实现类。
> 这时 evidence 里**接口 uid 和实现 uid 都会收** —— Python 做 citation 校验时两个都认。

### 3.5 Python 侧对外暴露的两个接口（P2 只有桩）

```
POST /internal/agent/ask            执行一轮
POST /internal/agent/admin/evict    项目删除时清 Context
```

**`/internal/agent/ask` 的 P2 桩形状**：

```jsonc
// 请求
{ "projectId": 1001,
  "conversationId": null,               // P4 才有；P2 可不传
  "message": "看 OrderService 的下游",
  "focus": { "type": "CLASS", "uid": "com.x.OrderService" } }   // 可为 null
```

```jsonc
// 响应 —— ★ 直接就是最终 AgentResponse 的形状，P3 只换 message 的来源
{ "conversationId": null,
  "message": "【P2 桩】call-chain(OrderService, DOWN, depth=2) 返回 4 个类、3 条边、1 个 gap",
  "citations": [],
  "uiCommands": [],
  "suggestions": [],
  "steps": [ { "n": 1, "type": "TOOL", "detail": "call_chain(...)", "ms": 140 } ],
  "meta": { "intent": "P2_STUB", "confidence": 0, "graphRev": 7, "latencyMs": 160 },
  "debugToolResults": [ /* 原始信封，便于 curl 对拍 */ ] }
```

> **为什么桩也用最终形状**：P4 的 Java 网关要照这个形状写透传。P2 就把形状钉死，
> P3 只需把「拼 message」从模板换成 LLM，**不产生接口变更**。
> `debugToolResults` 是 P2 临时字段，**P3 删掉**。

---

## 4. `java_client` 设计

### 4.1 三道错误映射（必须分开处理）

| 情况 | 表现 | Python 侧处置 |
| --- | --- | --- |
| 鉴权失败 | HTTP **401** + `Result.fail` 体 | 抛 `InternalAuthError` —— 这是**配置错误**，不是业务结果，必须炸出来 |
| 超时 / 连不上 | `httpx.TimeoutException` / `ConnectError` | 抛 `ToolTransportError`；P3 由循环转成降级答复（C2） |
| **业务失败** | HTTP **200** 但 `ok:false` | **不是异常** —— 正常返回信封，由调用方按 `ok` 分支 |
| 参数非法 | HTTP 200 但 `ok:false`（如 `direction=BOTH`） | 同上；**这类错误是 Python 传参错了，应在契约测试里就拦住** |

> ⚠️ **不能只看 HTTP status**。设计里所有「工具答不了」都是 200 + `ok:false`，
> 这是 `ToolEnvelope` 的明确设计（避免把业务结果变成 500）。

### 4.2 三条硬约束

| # | 约束 | 原因 |
| --- | --- | --- |
| 1 | `args` 的 key **必须是 camelCase**，与 §3.3 全表逐个一致 | Java 用 `strArg`/`intArg` 取值，**找不到 key 就吃默认值，不报错** —— 见 §6-① |
| 2 | 数字必须序列化成 **JSON number**，不能是字符串 | `intArg` 判的是 `raw instanceof Number` —— 见 §6-② |
| 3 | 每个调用带 **8s 超时**，且要**显式传**（不依赖 Java 默认值） | 级联超时 C1；预算必须由 Python 主动执行 —— 见 §6-⑤ |

### 4.3 接口签名（建议）

```python
class JavaClient:
    async def overview(self, project_id: int) -> ToolEnvelope[ProjectOverview]: ...
    async def classes(self, project_id: int, limit: int = 40) -> ToolEnvelope[list[ClassNode]]: ...
    async def class_relations(self, project_id: int, uid: str,
                              direction: Literal["UP","DOWN","BOTH"] = "BOTH",
                              limit: int = 40) -> ToolEnvelope[ClassRelations]: ...
    async def call_chain(self, project_id: int, uid: str,
                         direction: Literal["DOWN","UP"] = "DOWN",
                         method_uid: str | None = None,
                         max_depth: int = 2, max_nodes: int = 40) -> ToolEnvelope[CallChain]: ...
    async def method_body(self, project_id: int, uid: str | None = None,
                          method_uid: str | None = None,
                          max_methods: int = 3, max_lines: int = 120) -> ToolEnvelope[MethodBody]: ...
```

> **Python 侧形参用 snake_case、对外发的 JSON key 用 camelCase** —— 转换集中在 `java_client` 内部**一处**完成。
> 这是唯一允许发生风格转换的地方，模型层一律保持 camelCase。

---

## 5. 配置与环境变量

`config.py` 用 `pydantic-settings` 读环境变量。**P2 真正用到的只有前三个**：

```bash
AGENT_PORT=8000                                  # P2
JAVA_BASE_URL=http://localhost:8080              # P2（本地联调改这里；compose 里是 http://backend:8080）
INTERNAL_TOKEN=dev-internal-token-change-me      # P2 ★ 必须与 Java 的 application.yml 同名同值

DEEPSEEK_API_KEY=                                # 占位，P3
DEEPSEEK_BASE_URL=https://api.deepseek.com       # 占位，P3
REDIS_URL=redis://172.21.160.203:6379/1          # 占位，P4（★ db=1，与 Java 的 db0 隔离）
STEPS_LIMIT=4                                    # 占位，P3
TOOL_CALLS_LIMIT=6                               # 占位，P3
TURN_TIMEOUT_SECONDS=60                          # 占位，P3
```

**两个必须对齐的点：**

- ⚠️ Java 侧是 `internal.token: ${INTERNAL_TOKEN:dev-internal-token-change-me}`
  （`ArchMind/src/main/resources/application.yml:66`）。本地不设环境变量就用这个默认值，**Python 的 `.env` 必须一样**，否则 401。
- ⚠️ `deploy/.env` 目前**没有 `INTERNAL_TOKEN`** —— P6 容器化时要补，否则容器里两边各用各的默认值。

`.env.example` 提交进 git，`.env` 进 `.gitignore`。

---

## 6. 照代码读出来的六个坑

> 这六条**设计文档里没有**（或写错了），全部来自今天的代码核对。

### ① camelCase 写错会**静默降级**，不报错 ★最阴

Java 用 `strArg(args, "methodUid")` / `intArg(args, "maxDepth", 2)` 取值，
**key 找不到就返回 fallback**，不抛异常、不记日志。

Python 侧若顺手写 `method_uid` / `max_depth`，请求会 **200 成功返回**，但参数全被忽略。

> 这是 A1「契约漂移」最凶的形态：**编译期过、运行时也不报错，只有结果悄悄不对。**
> 唯一防线是 §7 的契约测试。

### ② 数字必须发 JSON number，不能发字符串

`intArg` 判的是 `raw instanceof Number`。Python 传 `"maxDepth": "2"` → 落回默认值 2。
**碰巧和想传的一样**，所以更难发现。传 `"maxDepth": "1"` 才会暴露。

### ③ `ok:false` 走 HTTP 200

「类不存在」「项目未分析」「direction 非法」全是 `ok:false` 的**业务结果**，HTTP 仍是 200。
`java_client` 不能只看 status code。

### ④ `graphRev = 0` 不是错误

`GraphRevService` javadoc 写死了：**0 = 从未落图，或图是本次改动之前建的**（存量图没有 `GraphMeta` 节点）。
重建一次后才会变成 1、2、3……。Python 别把它当异常，也别用它判断「图是否可用」。

### ⑤ `maxNodes` 的硬上限是 **200**，不是设计文档写的 40

| 值 | 出处 | 真实角色 |
| --- | --- | --- |
| 40 | `InternalToolController.DEFAULT_LIMIT` | **默认值**（不传时给这个） |
| **200** | `CallChainServiceImpl.MAX_NODES_HARD` | **硬上限**（真正拦住你的那个） |

设计文档 §3 / §5 / §8-C3 写的「硬上限 40 / maxNodesPerCall = 40」**与实现不符**。

> **后果**：`maxNodesPerCall = 40` 是 **Agent 自己的预算**，Java **不会**替你拦到 40。
> 想让这个预算生效，**必须由 Python 显式传 `maxNodes=40`**。
> 这正好是 §4.2 第 3 条「参数要显式传」的原因 —— **预算的执行者是 Python，不是 Java。**

### ⑥ `gaps` 只在 DOWN 方向产生；UP 的空结果是合法的

`CallChainServiceImpl` 里 `gaps` 只在 `down()` 末尾被填（`resp.getGaps().addAll(gaps.list())`），
`up()` **完全不产生 gaps** —— `targetFanout` 为空时直接 `return resp`。

所以「谁调用它」返回 **0 个上游**，是**合法的空结果**，不是错误、也不是断链。

**要和下面这个区分开**（同一个接口的两种「没结果」）：

| 情况 | 返回 | Python 该做什么 |
| --- | --- | --- |
| UP 查一个没人调用的类 | `ok:true`，`nodes=[start]`，`edges=[]` | 正常讲「没有解析到调用方」 |
| 查一个**不存在的类** | `ok:false`，`note="类不存在: xxx"` | 走业务失败分支，提示类名不对 |

> 同理 `chainEvidence` 是由 `nodes`/`edges` 拼的 —— **`gaps` 不进 evidence**。
> 所以 LLM 引用的 uid 只会出现在 evidence 里，而 gap 里的 `at` 不一定 —— citation 校验要认这个差别。

### ⑦ Jackson 发 `null` 而不是省略键 —— 可空字段**必须**写 `| None`

项目**没配** `spring.jackson.default-property-inclusion`，也没有 `@JsonInclude(NON_NULL)`（已核实），
所以 Java 把空值序列化成 **`"note": null`**，而不是把键删掉。

**后果**：Pydantic 的 `= 默认值` 只在「**键不存在**」时生效，对**显式 `null` 无效**。

```python
note: str | None = None     # ✓ null 和「键不存在」都能接
note: str = None            # ✗ Java 发 null 时直接 ValidationError
```

**判据**：Java 侧是**原始类型**（`boolean` / `int`）→ 必定有值，可以必填；
是**包装类型 / String / List**（`Boolean` / `String` / `List`）→ **必须**带 `| None`。

**例外**：用 `List.of()` / `new ArrayList<>()` 初始化的字段（`ToolEnvelope.evidence`、`CallChain.nodes`、
`MethodBody.methods`）—— Java 保证非 null，写 `= []` 是安全的。

> 这条是写 `models/tool.py` 时现查现验出来的，不是推断：`grep JsonInclude src/main/` 结果为空。

### 附：`call-chain` 的 `direction` 枚举与 `class-relations` **不是同一个**

| 工具 | 枚举 | 可选值 | Java 类型 |
| --- | --- | --- | --- |
| `class-relations` | `RelationDirection` | `UP` / `DOWN` / **`BOTH`** | `service/ast/RelationDirection.java` |
| `call-chain` | `ChainDirection` | `DOWN` / `UP`（**无 BOTH**） | `service/ast/ChainDirection.java` |

> 两个枚举分开是**故意的** —— `call-chain` 的 DOWN 是**多层 BFS**、UP 是**单层反查**，没有 BOTH 这种组合。
> Python 侧别图省事定义成一个 `Direction`，那样 `call-chain(direction="BOTH")` 会编译期通过、运行时 `ok:false`。

---

## 7. 任务拆解与验收检查点

| # | 任务 | 产出 | 验收检查点 |
| --- | --- | --- | --- |
| **T0** | 服务落位 + 骨架 | `agent-service/` 目录、`requirements.txt`、`.env.example`、`.gitignore` | `uvicorn app.main:app --port 8000` 起得来；`curl localhost:8000/health` → 200 |
| **T1** | **契约层**（P2 的核心） | `models/tool.py` + `models/response.py` + `models/protocol.py`，逐字段对齐 §3 | 用 Java 真实响应 JSON 反序列化不报错 |
| **T2** | `java_client.py` | httpx 封装 5 个接口，三道错误映射（§4.1），8s 超时 | 单独调 `overview` 拿回真实数据 |
| **T3** | FastAPI 路由 + 桩 | `app/api/agent.py`：`/internal/agent/ask` + `/internal/agent/admin/evict` | `X-Internal-Token` 错 → 401；对 → 200 |
| **T4** | **打通 2 个工具** | `overview` + `call-chain` 走完 Java→Python→Java 全链路 | **curl 拿到类级调用链 + gaps**（见下） |
| **T5** | 契约测试（A1 防线） | `tests/` 5 个测试：请求字段 Java 能接 + 响应能反序列化进模型 | `pytest` 全绿 |

### T0 检查点

```bash
cd agent-service && python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

```bash
curl -s localhost:8000/health
```

### T4 检查点（前置：Java 后端已起）

```bash
mvn -f ArchMind/pom.xml spring-boot:run
```

```bash
curl -s localhost:8000/internal/agent/ask \
  -H "Content-Type: application/json" \
  -H "X-Internal-Token: dev-internal-token-change-me" \
  -d '{"projectId":1,"message":"看 OrderService 的下游","focus":{"type":"CLASS","uid":"com.hmall.service.OrderService"}}'
```

**通过标准**：`debugToolResults` 里有 `call-chain` 的完整信封，
`data.nodes` 里能看到沿 `IMPLEMENTS` 扇出到 `OrderServiceImpl` 的节点（`viaFanout=true`），
`data.gaps` 里的 `reason` 是 `REMOTE_SERVICE` 或 `INTERFACE_NO_IMPLEMENTATION`。

### T5 的 5 个契约测试（A1 防线，逐个对应 §6 的坑）

| # | 测试 | 拦的是 |
| --- | --- | --- |
| 1 | 五个接口的请求 key 与 §3.3 全表一致（**显式断言 camelCase**） | 坑 ① |
| 2 | `maxDepth=1` 传下去后，Java 返回的 `nodes[].depth` 最大值 ≤ 1（**不能用默认值也能过**） | 坑 ②⑤ |
| 3 | `call-chain(direction="BOTH")` → 断言返回 `ok:false`（**把非法枚举钉成测试**） | 坑 附 |
| 4 | `class-relations(uid="不存在的类")` → `ok:false` 且不抛异常 | 坑 ③⑥ |
| 5 | `overview` 在**未落图项目**上仍 `ok:true`，`graphRev=0` 被正常接受 | 坑 ④ |

> 测试 2 是**唯一能抓出「参数静默降级」的测试** —— 必须让「传了 1」和「没传（默认 2）」
> 产生**不同**的结果，否则测了等于没测。

---

## 8. P2 明确不做

| 不做 | 哪个阶段 | 留到那时的原因 |
| --- | --- | --- |
| Intent 判定 / ReAct 循环 / 讲解 | P3 | 要 LLM，且要先验证 DeepSeek 的 JSON 输出模式（F3） |
| 四道拦截（confidence / evidence 校验 / gaps 强制提及） | P3 | 没有 LLM 就没有幻觉可拦 |
| Redis Context / `models/context.py` | P4 | 存储层，P2 的桩是无状态的 |
| viewport 上报 / ack 回执 | P4 | 依赖前端，P5 才有落点 |
| Java 侧 `/api/agent/*` 网关 | P4 | 方向相反的那一跳 |
| Dockerfile / compose 第三服务 / INTERNAL_TOKEN 进 `.env` | P6 | 本地联调不需要容器 |
| 前端改造 | P5 | 工作量最大、不确定性最高，排最后 |

---

## 附：与既有文档的关系

- [9月19 Agent v1设计](9月19%20Agent%20v1设计.md) —— 服务边界 / 工具契约 / 闭环时序 / P0–P7；本篇是它 §9-P2 与 §10 的展开与**纠偏**
- [9月20 第一阶段问答边界](9月20%20第一阶段问答边界.md) —— V1 能答到什么程度；本篇 §7 的验收预期即其 §5 的落地
- [9月21 call-chain 设计](9月21%20call-chain%20设计.md) —— 本篇 §3.4-④ 那些字段（`viaFanout` / `gaps` / `resolvedFrom`）的算法来源
- [开发路线图](../开发路线图.md) —— 宏观阶段；P2 的验收一栏
- [9月22 个人理解](9月22%20个人理解.md) —— 先于本篇的记录；其中「Tool 截断 ≠ 数据库 LIMIT」已体现为本篇 §3.4-② 的 `limit+1` 判截断
