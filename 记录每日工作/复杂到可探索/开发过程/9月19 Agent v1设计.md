# 9月19 Agent v1 设计（Java + Python 两服务）

> 承接 [9月16 用户体验与agent设计](9月16%20用户体验与agent设计.md)。
> 9/16 定「Agent 要做什么」，本篇定「Agent 放在哪、怎么和 Java 协作、闭环怎么落地」。
> **本篇是设计，不含代码改动。**
> 对应的路线图阶段：**P2 Agent v1**（宏观口径）；执行步骤 P0–P7 见 §10。

---

## 0. 今天定了什么

| #   | 决定                                                       |
| --- | -------------------------------------------------------- |
| 1   | **Agent 用 Python 写**，与 Java 构成两个服务（也为之后演进微服务铺路）          |
| 2   | **按数据所有权切服务边界**，不按语言切                                    |
| 3   | Agent 的边界控制（步数/预算/去重/超时）**自己实现，不引 LangChain**            |
| 4   | V1 走**同步请求 + 结构化 JSON 输出**，不用原生 function calling、不做异步状态机 |
| 5   | UICommand 从 9 个**砍到 4 个**                                |
| 6   | **V1 入口是「类」不是「模块」**（模块在 P3 之前是假节点，见 §8-B5）               |
| 7   | ⏸ **待定**：Python 服务的落位与仓库结构（同仓库 vs 独立仓库）                  |

---

## 1. 服务边界（后面所有设计都依赖它）

**判据：一个服务 = 一份数据的所有权 + 一个变更的理由。**

### 边界表

| 关注点                 | Java（code-intelligence） | Python（agent）       |
| ------------------- | ----------------------- | ------------------- |
| MySQL 业务数据          | ✅ 独占                    | ❌ 不连                |
| Neo4j 代码图           | ✅ 独占                    | ❌ 不连                |
| 源码文件磁盘              | ✅ 独占                    | ❌ 不读                |
| `task` 表（ANALYSIS）  | ✅ 独占                    | ❌                   |
| LLM 调用              | 暂留（分析流水线）               | ✅ 新增全部在此            |
| 会话 / Context / 步骤日志 | ❌                       | ✅ 独占（Redis db1）     |
| JWT / 用户 / 租户校验     | ✅ 独占                    | ❌ 只接收已校验的 projectId |
| 对外 HTTP 入口          | ✅                       | ❌ 不对公网暴露            |

### 三条硬规则

**① Python 不直连 Neo4j / MySQL**

诱惑是少一跳、开发快一倍。否决理由：

- **共享数据库 = 分布式单体**（微服务头号反模式）：库表结构变成隐式契约，改一处两边一起炸
- **图查询会分裂成两份真相**：Java 的 `CodeGraphQueryServiceImpl` 里有类级折叠逻辑，Python 再写一遍 → 前端和 Agent 看到的图**不一样**，这种 bug 极难查
- **绕过租户校验**：`CheckProjectUtil` 在 Java，Python 直接拿用户传的 projectId 查图 = **越权读别家项目代码**

**② JWT 永远不进 Python**

入口留在 Java，用户 token 在 Java 层被换成「已校验的 projectId」再下传。白拿的安全收益：

- token 不可能被拼进 LLM prompt（拼进去等于把用户凭证发给 DeepSeek）
- token 不可能出现在 Python 日志里
- Python 被攻破，攻击者也拿不到可复用凭证

**③ 鉴权必须留在数据主人那**

|         | 认证 Authentication | 鉴权 Authorization          |
| ------- | ----------------- | ------------------------- |
| 问题      | "你是谁"             | "你能不能访问这个资源"              |
| 需要什么    | 一个密钥（验 JWT 签名）    | **查数据**（这个 project 是不是你的） |
| 能前置到网关吗 | ✅ 能               | ❌ **不能**                  |

所以将来若加网关，网关只做认证，注入 `X-User-Id`；Java 从 header 取 userId 后自己查库做鉴权。

### 调用链

```
前端 → Java（JWT + 租户校验）→ Python（Intent / 工具编排 / LLM）→ Java（/internal/tools/* 取数）
```

**Java 在这里扮演两个角色**：对外的 BFF、对内的数据主人。第 2 跳和第 3 跳方向相反但语义不同，不是循环依赖。

### 工具的「两个半边」

**5 个工具，不是 10 个。**

| | Java 侧 | Python 侧 |
| --- | --- | --- |
| 它是什么 | **执行** | **契约 + 编排** |
| 内容 | Cypher、折叠、截断 | 工具名/描述/参数 schema、何时调、怎么回灌 |
| 关心 | "怎么查出来" | "LLM 该在什么时候用什么参数调" |

> **Java 的这 5 个接口对「Agent」这件事一无所知** —— 它们只是普通的内部数据接口。所以以后前端的图谱页也能复用同一批接口。

> **判据**：改 Neo4j 的 schema（比如给 `Method` 加字段），需要动几个服务？答案 1 个（Java）→ 边界对了。

---

## 2. 问题一：Java 与 Python 的通信

### 方案：就是 HTTP

两个独立进程，互相发 HTTP 请求，和「前端调后端」是同一种通信方式。

```java
// Java → Python
RestClient.create().post()
    .uri("http://localhost:8000/internal/agent/ask")
    .header("X-Internal-Token", token)
    .body(req).retrieve().body(AgentResponse.class);
```

```python
# Python → Java
async with httpx.AsyncClient() as c:
    r = await c.post("http://localhost:8080/internal/tools/call-chain",
                     json=payload, timeout=8.0)
```

**地址**：本地开发用 `localhost:8000 / 8080`；compose 里**服务名即主机名**（`http://agent:8000`、`http://backend:8080`）。

### 一次完整对话的 4 跳

```
① 前端  → Java      POST /api/agent/ask        （公网入口，带 JWT）
② Java  → Python    POST /internal/agent/ask   （委托：这轮交给你）
③ Python → Java     POST /internal/tools/...   （取数：我要你手里的图数据）
④ Java  → 前端      返回 message + uiCommands
```

**`NAVIGATE` 类请求短路**：用户点「展开」「退回去」时只走 ①②④，**不调工具、不调 LLM**。这是最便宜的体验优化。

### 内部调用的三道防线

| 层 | 做法 |
| --- | --- |
| 网络层 | Python 容器**不映射 ports**，只在 compose 网络内可达；nginx **不路由 `/internal/**`** |
| 鉴权层 | `X-Internal-Token` 共享密钥；Java 的 `/internal/tools/*` 同样校验 |
| 数据层 | `/internal/tools/*` **只接受已解析的 projectId**，不接受任何用户身份信息 |

> 共享密钥对实验室规模够用。真微服务用 mTLS / 服务网格 —— 记在 §11 演进路线，V1 不做。

### 同步还是异步？→ **V1 选同步**

| 方案 | 评价 |
| --- | --- |
| **同步 JSON（选它）** | 一轮 ≈ 2 次 LLM + 2 次工具 ≈ 3–8s；响应带 `steps[]`，前端可回放出过程感 |
| 异步 + 轮询 | 要引一套 run 状态机、Java 要代轮询、前端要写轮询 |
| SSE 流式 | 体验最好，但要 Java 做响应式透传，复杂度陡增 |

**理由**：3–8s 在聊天界面里是「可以等」的量级，而异步要付一整套状态机的复杂度。`steps[]` 已解决 80% 的体验问题，成本几乎为零。

> **`task` 表不动** —— 它是 ANALYSIS 专用的，QA 链路的步骤日志由 Python 自己的会话存储保管。两者生命周期完全不同（ANALYSIS 是可重跑的重活；QA 是有状态的短会话）。SSE 放 P7。

### 内部接口清单

```
POST /internal/agent/ask                   执行一轮
POST /internal/agent/admin/evict           项目删除时清 Context
Post /internal/tools/{overview|classes|call-chain|class-relations|method-body}
POST /api/agent/ask                        对外（Java，带 JWT）
GET  /api/agent/conversations/{id}         恢复会话（刷新页面用）
POST /api/agent/conversations/{id}/viewport 前端上报屏幕状态
POST /api/agent/conversations/{id}/ack      前端回执（UICommand 已执行）
```

---

## 3. 问题二：Tool 设计

### 原则：按任务设计，不按数据库表设计

> **加新工具的判断标准：Agent 有一个新的「业务问题」要回答，而不是「Python 想查某张表」。**

### 能力式接口 vs 数据表式接口

**❌ 数据表式（等于没拆）**

```jsonc
POST /internal/db/query
{ "cypher": "MATCH (c:Class {projectId:$pid})-[:CALLS*1..2]->(x) RETURN x" }
```

看着像走了 HTTP，问题全在：Python 得懂 Neo4j schema、得会写 Cypher → **schema 变成隐式契约**；等于把数据库钥匙借给 Python；最要命的是 **projectId 是 Python 拼进 Cypher 的，Java 无从校验**。

**✅ 能力式（真拆）**

```jsonc
POST /internal/tools/call-chain
{ "projectId": 1001, "uid": "com.hmall.service.OrderService",
  "direction": "DOWN", "maxDepth": 2 }
```

Python 说的是**业务问题**（"谁调用了谁"），不是**怎么查**。用哪条 Cypher、怎么折叠、怎么限流，全在 Java 手里。

### 5 个工具

| 工具                | 回答的业务问题         | 数据源                  | 状态                                               |
| ----------------- | --------------- | -------------------- | ------------------------------------------------ |
| `overview`        | 这个项目是做什么的       | `project_overview` 表 | 包装已有                                             |
| `classes`         | 项目里有哪些类（兜底）     | Neo4j                | 包装已有                                             |
| `class-relations` | 谁依赖它 / 它继承谁     | Neo4j（1-hop）         | **包装已有**（`CodeGraphQueryService.classRelations`） |
| **`call-chain`**  | **谁调用了谁（业务流程）** | Neo4j 变长 CALLS       | **新算法**                                          |
| **`method-body`** | **这个方法怎么实现的**   | `file` 表 + 磁盘切片      | **新算法**                                          |

> **5 个里 3 个是包装已有能力** —— 不要为 Agent 复制一份查询逻辑，否则图查询有两份真相。

### 统一信封

```jsonc
{
  "ok": true,
  "data": { /* 工具专属载荷 */ },
  "evidence": [                          // ★ 真实来源凭据
    { "kind": "EDGE", "from": "com.hmall.service.OrderService#createOrder(OrderDTO)",
      "to": "com.hmall.inventory.InventoryService#deduct(Long)",
      "rel": "CALLS", "count": 3 }
  ],
  "truncated": false,                    // ★ 被截断必须为 true
  "note": null,
  "graphRev": 7                          // 图版本，用于缓存失效
}
```

**为什么 `evidence` 和 `truncated` 是必需的：**

- `evidence` 让「禁止编造」从 prompt 口号变成**可校验机制** —— 后端只需检查 `citations[].uid` / `uiCommands[].target.uid` 是否出现在本轮 evidence 里
- `truncated=true` 是要让 LLM **知道**结果被截断。不说的话，它会把「只看到 3 个」讲成「这个类只调用 3 个」——这是最难发现的错误

### `call-chain` 的两个设计要点

**① 必须硬编码上限**

```cypher
MATCH path = (start:Method {projectId:$pid, owner:$uid})-[:CALLS*1..3]->(m:Method {projectId:$pid})
MATCH (c:Class {projectId:$pid, uid: m.owner})
RETURN c.uid, c.name, c.description, length(path) AS depth
ORDER BY depth, c.uid LIMIT $maxNodes
```

`depth ≤ 3`、`maxNodes ≤ 40` —— **Cypher 变长路径在稠密图上会组合爆炸，这是打爆 Neo4j 的头号风险**。

**② 类级折叠在 Java 侧做 + 返回 `gaps`**

LLM 看不了大图也不该看（500 条方法边喂进去是噪声）。折叠成「类→类」后，还要返回：

```jsonc
"gaps": [ { "at": "com.hmall.service.OrderService", "reason": "NO_DOWNSTREAM_EDGE" } ]
```

`gaps` 把「链路断在哪」结构化返回，让 Agent 能准确说出"这里没解析到下游"，而不是含糊带过或编造。

### `method-body`：讲解深度的唯一来源

链路（**今天就能做，不动 AST**）：`Method.fileId + startLine/endLine` → `file` 表拿 filePath → `FileContentService.readTextContent` → 按行号切片 → `maxLines=120 / maxMethods=3` 硬截断。

> **这个工具是回应 9/16 反馈「核心的内容实现逻辑看不到」的正面手段。** 没有它，Agent 讲业务流程只能念签名 —— 就是"太肤浅"。

### 工具实现三条铁律

1. **只读** —— 不加任何写 Cypher
2. **幂等** —— 同参数同结果（`sameCallDedup` 依赖它）
3. **必带上限与超时** —— 每个工具都有 `limit`，exec 层统一包超时

### V1 明确不提供的

| 不提供 | 理由 |
| --- | --- |
| 语义检索 / `search_by_name` | **DeepSeek 无 embedding 接口**，做不了；P3 换 Qwen/智谱 |
| 整文件读取 | Context 爆炸 |
| 任何写操作 | 边界 |
| 跨项目查询 | 租户边界 |

> ⚠️ 缺 embedding 意味着 **V1 的「语义入口」是弱的** —— 用户得说对类名。这必须写进 V1 的验收预期，别到演示时才发现。

---

## 4. 问题三：Schema 设计

三组 schema，分属三层。

### ① Tool 输入 / 输出

见 §3 的统一信封。**输入**统一 `{projectId, args:{...}}`（projectId 由 Java 注入，LLM 不可指定）。

### ② AgentResponse

```jsonc
{
  "conversationId": "c_8f3a...",
  "message": "订单创建的主流程分三步：OrderController 接单 → OrderService 校验并落库 → InventoryService 扣减库存。注意：OrderService 到支付网关的调用本项目内未解析到，可能是外部依赖。",
  "citations": [                          // ★ 必须来自本轮 tool.evidence
    { "uid": "com.hmall.service.OrderService", "kind": "CLASS" }
  ],
  "uiCommands": [ { "op": "SHOW_PATH", "target": {...}, "params": {...}, "rev": 18 } ],
  "suggestions": [                        // ★ 闭环的发动机
    { "label": "看 InventoryService 的调用方", "intent": "EXPLORE_RELATION",
      "slots": { "entityRef": "com.hmall.inventory.InventoryService", "direction": "UP" } }
  ],
  "steps": [                              // 步骤回放 + 审计
    { "n": 1, "type": "INTENT", "detail": "EXPLORE_FLOW", "ms": 820 },
    { "n": 2, "type": "TOOL",   "detail": "call_chain(OrderService, DOWN, 2)", "ms": 140 },
    { "n": 3, "type": "LLM",    "detail": "explain", "ms": 2600 }
  ],
  "meta": { "intent": "EXPLORE_FLOW", "confidence": 0.86, "graphRev": 17, "latencyMs": 3600 }
}
```

设计要点：

- **`message` 与 `uiCommands` 解耦** —— 文字是讲解，命令是可视化，独立失败互不拖累（图挂了字还在）
- **`citations` 可校验** —— grounding 的抓手
- **`suggestions` 是闭环的发动机** —— 9/16 闭环图最后一步「用户继续提问」，靠这个才转得动

### ③ UICommand：**已定，砍到 4 个**

9/16 列了 9 个，V1 只做 4 个：

| 保留 | 为什么 |
| --- | --- |
| `FOCUS_NODE` | 闭环刚需：选中 + 居中，对应「点击 InventoryService」 |
| `EXPAND_NODE` | 闭环刚需：拉出邻居，对应「看到调用链」 |
| `SHOW_PATH` | `call-chain` 的天然输出形态 |
| `CLEAR_VIEW` | 重置，成本极低 |

| 砍掉 | 为什么 |
| --- | --- |
| `ADD_NODE` / `REMOVE_NODE` | 与 `EXPAND_NODE`/`COLLAPSE_NODE` 语义重叠，且要求 LLM 精确操作图结构 —— **LLM 一旦算错图就不一致** |
| `HIGHLIGHT_NODE` / `HIGHLIGHT_RELATION` | 让 `FOCUS_NODE`/`SHOW_PATH` 自带高亮即可 |

> **核心原则：LLM 声明「要看什么」，工具产「看的数据」，前端算「怎么画」。图结构绝不交给 LLM 拼。**

```jsonc
{ "op": "SHOW_PATH",
  "target": { "type": "CLASS", "uid": "com.hmall.inventory.InventoryService" },
  "params": { "direction": "DOWN", "depth": 2, "highlight": true },
  "rev": 18 }
```

### ④ Pydantic 模型 = 契约的唯一真相

选 FastAPI 最实在的收益。**同一份模型定义四处复用：**

| 用途 | 说明 |
| --- | --- |
| ① 代码类型 | 运行时校验，类型错误直接 422 |
| ② OpenAPI 文档 | `/docs` 自动生成 |
| ③ **LLM 的 JSON Schema** | `Model.model_json_schema()` 直接塞进 prompt，**LLM 看到的 schema 和代码永远一致** |
| ④ 测试断言依据 | 契约测试直接对着模型写 |

**没有第 ③ 条时最痛**：prompt 里手写的 JSON 示例会随代码演进悄悄过期，然后 LLM 开始返回旧格式——这类 bug 极难查。

```python
class UICmd(BaseModel):
    op: Literal["FOCUS_NODE", "EXPAND_NODE", "SHOW_PATH", "CLEAR_VIEW"]
    target: NodeRef | None = None
    params: dict[str, Any] = {}
    rev: int
```

> `Literal[...]` 把取值钉死 —— **LLM 想返回不存在的 op，在模型校验层就被拒了**，不用等前端。

---

## 5. 问题四：Agent 边界

分五层，**每层都要有可执行的机制，不能只是 prompt 里的一句话**。

### ① 能力边界

```
✓ 查询 / 读文件 / 解释 / 推荐下一步 / 控制可视化
✗ 写代码、改文件、改数据库、执行项目代码、做架构评分
```

定位：**「理解已有项目」，不是「改造项目」**。V1 严格只读。

### ② 知识边界（grounding）★ 最关键

> **所有实体名、关系、行为声称，必须来自本轮 tool 返回结果。tool 没给 = 说「未知」。**

用四道拦截强制执行，而不是靠 LLM 自觉 —— 见 §8-A。

### ③ 调用边界 ★ 必须落成代码

```java
maxSteps        = 4     // 一轮最多 4 个「tool → LLM」循环
maxToolCalls    = 6     // 总工具调用次数
maxDepth        = 3     // 图查询深度
maxNodesPerCall = 40    // 单次返回节点数
toolTimeout     = 8s    // 单工具超时
turnTimeout     = 60s   // 整轮超时
sameCallDedup   = true  // 同工具同参数 → 返回上次结果
```

**计数存放在 Context 的 `budget` 里**，不是局部变量 —— 因为**跨轮也要生效**，否则用户连点 10 次，第 10 次照样能触发 6 次工具调用。

**超过上限的处置不是静默截断**，而是带着已有结果收尾：

> "已探索到第 3 层，为控制范围先停在这里，可以继续问 XXX"

静默截断会让用户以为系统卡了。

### ④ 租户边界

- `projectId` 只从 `CheckProjectUtil.checkProject` 解析，**Tool 参数里不接受 projectId**
- 所有 Cypher 的 `pid` 由后端注入，**LLM 无法跨项目查询**
- 文件读取走 `code-workspace/{projectId}` 下的规范化路径（`FileContentServiceImpl` 已有 `NOFOLLOW_LINKS` 防护）

### ⑤ 拒绝模板（`OUT_OF_SCOPE`）

固定一句 + 引导回可用范围。**不做道德说教，一句话带过。**

---

## 6. 问题五：Agent 执行流程梳理

### Intent 分类（V1 六个，封闭枚举）

| Intent | 触发语义 | 动作 | 是否花 LLM |
| --- | --- | --- | --- |
| `EXPLORE_OVERVIEW` | 这个项目/模块干嘛 | 读 overview | 是 |
| `EXPLORE_FLOW` | 业务流程 / 调用链 | `call-chain` | **是（核心场景）** |
| `EXPLORE_RELATION` | 这个类干嘛 / 谁调它 | `class-relations` | 是 |
| `EXPLORE_CODE` | 具体怎么实现的 | `method-body` | 是 |
| `NAVIGATE` | 跳过去 / 展开 / 退回去 | **只产 UICommand** | **否** ← 省一半成本 |
| `OUT_OF_SCOPE` | 写代码 / 评价代码质量 | 拒绝模板 | 否 |

**判定由 LLM 结构化输出，不做关键词匹配** —— 中文口语（"它呢？""再往上看看"）上关键词必然失效。

```jsonc
{ "intent": "EXPLORE_FLOW", "confidence": 0.86,
  "slots": { "entityRef": "IT", "direction": "DOWN", "depth": 2 },
  "rewrittenQuery": "OrderService 的下游调用链" }
```

**代词消解规则表（进 prompt，不让 LLM 自由发挥）：**

| 用户表述 | 消解为 |
| --- | --- |
| 它 / 这个 / 该类 / 上面那个 | `focus.uid` |
| 它的调用方 / 谁调它 | `focus` + `direction=UP` |
| 回到上一个 / 退回去 | `focusStack.pop()` → 纯 NAVIGATE |
| 属于哪个模块 | `anchor.target`（V1 降级回答） |

**低置信度处置**：`confidence < 0.6` → **不执行工具**，回一句澄清 + 两个可点选项。
**宁可多问一句，也不要基于猜错的 Intent 拉出一张错的图** —— 错的图比不回答伤害大得多。

### 完整时序（带服务归属）

```
用户点击 OrderService
  │ POST /api/agent/ask {projectId, focus, viewport}
  ↓
┌─ Java ─────────────────────────────────┐
│ 1. JWT 校验                             │
│ 2. CheckProjectUtil.checkProject ← 租户边界在这
│ 3. 补 X-Internal-Token，转发 → Python   │
└────────────────┬───────────────────────┘
                 ↓
┌─ Python ───────────────────────────────┐
│ 4. 加载/新建 Context（Redis db1）        │
│ 5. Intent 判定（LLM #1，结构化输出）      │
│ 6. 预算检查：steps=0/4, toolCalls=0/6    │
│ 7. httpx → POST /internal/tools/call-chain
└────────────────┬───────────────────────┘
                 ↓
┌─ Java ─────────────────────────────────┐
│ 8. Cypher: CALLS*1..2 → 类级折叠 + gaps  │
│    （Neo4j 的唯一读者）                   │
└────────────────┬───────────────────────┘
                 ↓ {ok, data, evidence[], truncated, gaps}
┌─ Python ───────────────────────────────┐
│ 9. evidence 注入 prompt → LLM #2 讲解     │
│    · 有 gaps 时强制说「未解析到下游」      │
│    · 不允许出现 evidence 之外的类名       │
│ 10. 生成 uiCommands: [SHOW_PATH]         │
│ 11. 落 steps[] + 更新 Context.history    │
└────────────────┬───────────────────────┘
                 ↓ AgentResponse
┌─ Java ─────────────────────────────────┐
│ 12. ★ 校验 uiCommands[].target.uid ∈ evidence
│     未命中 → 丢弃该命令 + 记 warn        │
│ 13. 原样回传前端                          │
└────────────────┬───────────────────────┘
                 ↓
前端：执行 SHOW_PATH → 图画出来
  │ POST /conversations/{id}/viewport {rev:18, ...}
  ↓ Python：Context.viewport ← 仅接受 rev 递增
  │
  ↓ 用户点击图上的 InventoryService
  → 回到第 4 步，focus 更新，focusStack 压栈
```

### 循环伪代码

```python
async def run_turn(ctx, user_msg) -> AgentResponse:
    steps = []
    ctx.budget.tool_calls_this_turn = 0

    intent = await classify_intent(ctx, user_msg)        # LLM #1
    steps.append(Step(type="INTENT", detail=intent.intent))

    if intent.confidence < 0.6:
        return clarify(ctx, intent)                      # 不执行工具

    if intent.intent == "NAVIGATE":                      # 短路：不花 LLM #2
        return navigate_response(ctx, intent)

    evidence, gaps = [], []
    while ctx.budget.steps_used < STEPS_LIMIT:           # 默认 4
        plan = await plan_tool(ctx, intent, evidence)    # LLM 决定调什么
        if plan is None: break                           # LLM 说信息够了
        if ctx.budget.tool_calls_this_turn >= TOOL_LIMIT: break
        if is_duplicate(plan, evidence):
            evidence.append(cached(plan)); continue
        result = await java_client.call(plan, timeout=8)
        evidence += result.evidence
        gaps += result.get("gaps", [])
        steps.append(Step(type="TOOL", detail=plan.summary(), ms=result.ms))
        ctx.budget.tool_calls_this_turn += 1

    text = await explain(ctx, evidence, gaps)            # LLM #2
    ui = await build_ui_commands(ctx, intent, evidence)
    ui = guard.filter_ui_commands(ui, evidence)          # ★ 幻觉拦截

    ctx.append_history(intent, user_msg, summarize(text))
    await store.save(ctx)
    return AgentResponse(..., steps=steps)
```

---

## 7. 问题六：AgentContext 数据结构

**第一原则：Context 存状态与引用，不存知识。** 所有实体引用一律用**图的 uid 字符串**（Class = 全限定名 / Method = `owner#signature`）。

```jsonc
// Redis: agent:ctx:{projectId}:{conversationId}   TTL 2h   由 Python 独占（db1）
{
  "version": 1,
  "conversationId": "c_8f3a...",
  "projectId": 1001,
  "graphReady": true,              // 未落图时只能答 overview，不能答关系

  "anchor": {
    "target": { "type": "CLASS", "uid": "com.hmall.service.OrderService",
                "name": "OrderService", "origin": "USER_PICK" },
    "focus":  { "type": "CLASS", "uid": "com.hmall.service.OrderService",
                "name": "OrderService" },
    "focusStack": [ { "uid": "...", "at": "2026-09-19T10:21:00" } ]
  },

  "viewport": {
    "rev": 17,                     // 单调递增，前端上报
    "visibleNodes": [ { "type": "CLASS", "uid": "..." } ],   // 硬上限 60
    "visibleRelations": [ { "from": "...", "to": "...", "type": "CALLS" } ],
    "lastUiCommands": [],          // 刷新后重放
    "truncated": false             // 超 60 置 true，提示 LLM 屏幕上不止这些
  },

  "history": [
    { "turn": 1, "intent": "EXPLORE_FLOW",
      "q": "订单创建的业务流程是什么",
      "a": "OrderController→OrderService→InventoryService（已高亮）",
      "focusAfter": "com.hmall.service.OrderService" }
  ],

  "budget": { "turns": 1, "toolCallsThisTurn": 0, "toolCallsLimit": 6, "stepsLimit": 4 }
}
```

### 五个设计决定

**① 只用 uid 引用，不存对象** —— uid 是图的唯一键，天然稳定、天然可校验。

**② `viewport` 的真相在前端，不在后端** —— Context 里的 `visibleNodes` 是「前端最后一次上报的屏幕状态」，不是后端推断的。所以必须有反向接口 + `rev` 单调递增，后端只接受更新的 rev。

**③ UICommand 必须有 ack** —— 前端执行完回执，后端才写入 `viewport.lastUiCommands`。**没有 ack，Context 必然和屏幕漂移。** `lastUiCommands` 顺带解决刷新后的图重放。

**④ `focusStack` 用栈不用数组** —— 9/16 里 `explorationHistory` 的诉求是「回到前一个流程」，栈是天然匹配的结构。「退回去」= `pop()`，纯 NAVIGATE，不花 LLM。

**⑤ `history` 存摘要不存原文** —— 每条回答压到一句。否则十轮之后 Context 就爆了。

### 存储边界

| 层 | 存什么 | 说明 |
| --- | --- | --- |
| Redis db1 | Context 热状态 | TTL 2h。**这是主存储不是缓存** —— 会话本就有生命周期，丢了等于这次对话结束 |
| Python 会话存储（待建） | 历史会话列表 | 一旦要做「翻看历史对话」，就需要 Python 自己的持久化库。**注意是 Python 自己建库，不是同步进 Java 的库** |

---

## 8. T12 视角：这个阶段还会遇到的问题 ★

> 下面这些是**今天的问题清单里没列的**，但按企业工程标准一定会撞上。按性质分五类。

### A. 一致性与幻觉

| # | 问题 | 后果 | 防线 |
| --- | --- | --- | --- |
| **A1** | **契约漂移** —— Python 声明 `call-chain(maxDepth)`，Java 实现写的是 `depth` | 编译期都过，跑起来才炸 | **契约测试**：Python 侧写 5 个测试，断言请求 Java 能接、返回信封形状符合预期 |
| **A2** | **UICommand 幻觉** —— LLM 在一个不存在的 uid 上生成 `FOCUS_NODE` | 前端要么静默失败要么画空节点，**用户以为系统坏了** | Java 侧校验 `target.uid ∈ evidence`，未命中**直接丢弃** |
| **A3** | **Intent 误判** —— 代词消解错，拉出一张错的图 | 用户看到错误信息却不知错 | `confidence < 0.6` → 不执行工具，回澄清 + 可点选项 |
| **A4** | **LLM 用常识补全断链** —— 图里没有 OrderService→支付网关，但 LLM「知道」应该有 | 编造出不存在的关系 | 返回 `gaps` + prompt 强制要求提及「未解析到」 |

### B. 数据与状态

| # | 问题 | 后果 | 防线 |
| --- | --- | --- | --- |
| **B1** | **viewport 与屏幕漂移** —— 若 `visibleNodes` 由后端推断 | Context 会说「点你看到的 X」，而屏幕上根本没有 X | 前端上报 + `rev` 单调递增 + ack 回执 |
| **B2** | **Context 膨胀** —— 十轮之后塞入全部对话原文 | token 爆炸、成本失控、幻觉上升 | 只存 uid + 一句摘要，硬上限 60 节点 |
| **B3** | **图的覆盖边界** —— AST 不解析 DI/多态，`@Autowired` 接口调用这条边可能是断的 | Agent 讲流程讲一半断掉 | 显式返回 `gaps`，**禁止 LLM 补全**。这不是 bug 是已知边界 |
| **B4** | **⚠️ 图重建不是原子的** —— `replaceProjectGraph` 先 `DETACH DELETE` 再写 | **写到一半失败 = 旧图已经删了，新图没写完，这个项目的图就空了** | **异名写入 + 校验完整 + 原子切换**；至少加「图是否完整」标记位 |
| **B5** | **模块是假节点** —— `modules_json` 由 LLM 只看 pom + README 生成，无 ID、无 `module→class` 边、keyFiles 未对账 | Agent 点进模块**拿不到任何真实关系**，第一步走死 | **V1 入口用「类」**，模块降级为 prompt 背景。（P3 领域拆分落地后再换回） |

> **B4 是本次核查中在现有代码里发现的最实在的工程缺陷**，不是理论问题。建议单独排期修。

### C. 可靠性与失败

| # | 问题 | 后果 | 防线 |
| --- | --- | --- | --- |
| **C1** | **级联超时** —— 只有一端设超时 | Python 卡住 → Java 线程池打满 → 整个系统挂 | **两端都要设**：Java 代理 Python 8s、Python 调 Java 8s、整轮 60s |
| **C2** | **LLM 无降级路径** —— LLM 不可用时整条线不通（已实际发生过） | 系统完全不可用 | 降级：**图查询照常工作，只有「讲解」退化为模板化输出** |
| **C3** | **工具无上限会打爆 Neo4j** —— 变长 CALLS 在稠密图组合爆炸 | Neo4j CPU 打满，全站查询变慢 | 硬编码 `depth ≤ 3` / `maxNodes ≤ 40` / 工具级 8s 超时 |
| **C4** | **观测缺失** —— 工具耗时、LLM token、失败率无埋点 | 出问题定位不到，也无法回答「运行成本多少」 | 埋四个数：token in/out、LLM 成功率、工具耗时、图规模 |

### D. 安全

| # | 问题 | 后果 | 防线 |
| --- | --- | --- | --- |
| **D1** | **`/internal/**` 暴露面** | 无鉴权读全量项目数据 | 三道：nginx 不路由 + 容器不映射 ports + `X-Internal-Token` |
| **D2** | **projectId 由 Python/LLM 指定** | 越权读别家项目代码 | projectId 只由 Java 注入，tool 接口不接受身份信息 |
| **D3** | **文件读取路径穿越** | 读到工作区之外 | 已有 `NOFOLLOW_LINKS` + 路径规范化，复用即可 |

### E. 成本

| # | 问题 | 后果 | 防线 |
| --- | --- | --- | --- |
| **E1** | **无 token 计量 / 配额** | 成本无上限 | 至少先埋点。配额/BYOK 排 P4 |
| **E2** | **NAVIGATE 也花 LLM 调用** | 用户点「展开」就烧一次 token | Intent 短路：NAVIGATE 不调 LLM |
| **E3** | **无缓存** | 同一问题重复查图/重复调 LLM | Java 侧缓存工具结果，**按 `graphRev` 做 key**（图一变 rev 变，旧缓存自然失效） |

> ⚠️ **Python 侧不要缓存图数据** —— 那等于又造一份副本（B3 的变种）。要缓存就缓在 Java 这边，因为**只有 Java 知道图什么时候变**。

### F. 产品与数据现实

| # | 问题 | 后果 | 防线 |
| --- | --- | --- | --- |
| **F1** | **没有 embedding → 没有语义入口** | 用户必须说对类名，否则 Agent 找不到目标 | **写进 V1 验收预期**，别到演示才发现。兜底用 `classes` 让 LLM 在类清单里选 |
| **F2** | **前端图谱不存在** | 闭环的最后一段没有落点 | 现状：`ClassExplorer` 是**列表**不是图；`MapCanvas` 渲染的是 `mapData.js` **静态演示数据**；`AIPanel` 是**硬编码假回复**（必须先删）。这是真实的前端改造，不是"加个接口" |
| **F3** | **DeepSeek 的 JSON 输出模式未验证** | 结构化输出不稳 → Intent 判定失败 | P0 阶段先单独验证一次 |
| **F4** | **两处持有 LLM key** | 轮换要改两处 | 演进目标：LLM 收拢到 Python 成为能力服务（Java 分析流水线改调 `/internal/llm/complete`） |

---

## 9. 执行步骤（P0–P7）

| 步骤 | 内容 | 验收动作 |
| --- | --- | --- |
| **P0 清障** | 建 `task` 表；验证 DeepSeek JSON 输出模式 | 能跑通一次完整 analyze |
| **P1 Java Tool API** | 5 个 `/internal/tools/*` + token 校验 + 2 个新算法 | **curl 直接打**，hmall 上 `OrderService` 下游 2 层人工对拍 |
| **P2 Python 骨架** | FastAPI + Pydantic 契约 + java_client + 2 个工具打通 | curl `/internal/agent/ask`，**不调 LLM**，只返回 tool 结果 |
| **P3 Intent + 循环 + LLM** | Intent 判定 + 预算 + 循环 + 讲解 + 四道拦截 | curl 问「业务流程」，拿到 `message + uiCommands + steps` |
| **P4 Java 网关 + Context** | `/api/agent/*` 代理 + uiCommands 校验 + viewport/ack | 打 Java 接口能通到 Python；rev 乱序被拒 |
| **P5 前端接线** | MapCanvas store 化 + AIPanel 接真接口 + 删假回复 + UICommand 执行 + viewport 上报 | 端到端跑通 |
| **P6 容器化** | Python Dockerfile + compose 加第三个服务 + nginx 路由 + 健康检查 | 三容器互通，`/internal/**` 公网 404 |
| **P7（可选）** | SSE 流式输出 | — |

> **P1–P4 全部可以脱离前端验收。** 前端改造（P5，工作量最大且不确定性最高）排最后 —— P4 做完就已经有一个能用 curl 演示的完整 Agent。

---

## 10. Python 服务结构（规划）

> ⏸ **待定（§0 第 7 条）**：服务代码的落位与仓库结构（同仓库并列目录 vs 独立仓库）暂未拍板。
> 倾向同仓库（`agent-service/` 与 Java 后端并列）——契约改动一次提交、本地起服务方便、面试时一个链接看完。**但 P2 真正动工时再定，现在不阻塞设计。**

```
agent-service/
├── app/
│   ├── main.py                    FastAPI 入口
│   ├── config.py                  pydantic-settings 读环境变量
│   ├── api/agent.py               /internal/agent/* 路由
│   ├── models/                    ★ Pydantic 模型 = 契约唯一真相
│   │   ├── context.py             AgentContext / Viewport / Budget
│   │   ├── intent.py              IntentResult / Intent 枚举 / Slots
│   │   ├── tool.py                ToolRequest / ToolResult / Evidence
│   │   ├── response.py            AgentResponse / UICmd / Suggestion / Step
│   │   └── protocol.py            Java ↔ Python 传输 DTO
│   ├── core/
│   │   ├── loop.py                ReAct 循环：预算 + 去重 + 超时
│   │   ├── intent.py              Intent 判定
│   │   ├── prompt.py              system prompt 组装
│   │   └── guard.py               evidence 校验 / 幻觉拦截
│   ├── tools/
│   │   ├── java_client.py         httpx 封 Java 的 5 个 tool 接口
│   │   └── registry.py            工具注册表：name → schema + handler
│   └── store/context_store.py     Redis 读写 + rev 乐观锁
├── tests/                         ★ 契约测试在这
├── requirements.txt
├── Dockerfile
└── .env.example
```

**环境变量：**

```bash
AGENT_PORT=8000
JAVA_BASE_URL=http://backend:8080          # compose 内部服务名
INTERNAL_TOKEN=<与 Java 共享>
DEEPSEEK_API_KEY=<两处各一份，见 F4>
DEEPSEEK_BASE_URL=https://api.deepseek.com
REDIS_URL=redis://172.21.160.203:6379/1    # ★ db=1，与 Java 的 db0 隔离
STEPS_LIMIT=4
TOOL_CALLS_LIMIT=6
TURN_TIMEOUT_SECONDS=60
```

---

## 11. 演进到微服务的路线（学习地图）

| 阶段 | 引入 | 学到什么 | 触发条件 |
| --- | --- | --- | --- |
| **A（现在）** | 2 服务 + nginx ingress | 服务边界怎么划、内部 API 怎么鉴权 | — |
| B | 配置外置（Nacos/Apollo） | 配置为什么不能写死 | 配置项 > 15 个 |
| C | 服务发现 | 服务怎么找到对方 | 服务 > 3 个或 IP 会变 |
| D | Spring Cloud Gateway | 网关该做什么、不该做什么 | 路由规则变复杂 |
| E | 拆 Java 单体 | **拆之前先问「数据能否分家」** | 代码分析该独立（按 Neo4j 跟谁走） |
| F | 链路追踪 + 熔断限流 | 跨服务问题怎么定位 | 有真实用户 |

**V1 刻意不做服务发现**：用固定 host（compose 服务名）足够。过早引入注册中心，出故障时**你会分不清「是配错了还是服务真没起来」**——一次只引入一个新变量。

> **解耦和分家是两个独立步骤，别捆在一起做。** 现在 `service/ast/` 这个包结构就是模块边界，守住它，将来分家成本极低。

---

## 12. 反模式清单（照着躲）

| # | 反模式 | 后果 | 防线 |
| --- | --- | --- | --- |
| 1 | Python 直连 Neo4j/MySQL | 分布式单体；图查询两份真相；绕过租户校验 | §1 硬规则① |
| 2 | Python 信任前端传的 projectId | 越权读别家代码 | projectId 只由 Java 注入 |
| 3 | `/internal/**` 暴露公网 | 无鉴权读全量数据 | nginx 不路由 + 不映射端口 + token |
| 4 | 同步调用无超时 | 级联挂起 | 两端都设超时 |
| 5 | Context 存全文 | 十轮后爆炸 | 只存 uid + 一句摘要 |
| 6 | `visibleNodes` 由后端推断 | Context 与屏幕漂移 | 前端上报 + rev + ack |
| 7 | UICommand 的 uid 不校验 | 图上出现不存在的节点 | Java 侧 evidence 校验 |
| 8 | 为 Agent 复制一份图查询逻辑 | 两份真相 | 复用 `CodeGraphQueryService`，只加适配层 |
| 9 | 用 `task` 表跑 QA | 污染幂等重跑逻辑 | QA 步骤日志由 Python 保管 |
| 10 | 图重建先删后写 | 失败即图空 | 异名写入 + 原子切换（**B4**） |

> **注意**：这 10 条里只有第 3 条是**网关**能解决的。反模式大多发生在东西向（服务间）或服务内部，**网关在物理上看不见**。加中间件堵设计漏洞，是学微服务最常见的弯路。

---

## 附：与既有文档的关系

- [9月16 用户体验与agent设计](9月16%20用户体验与agent设计.md) —— 定「Agent 要做什么」（Context / Tool / UICommand / 边界四问）
- [开发路线图](../开发路线图.md) —— 宏观阶段 P0–P5；本篇 P0–P7 是执行粒度
- [实习准备节奏表](../实习准备节奏表.md) —— 什么时候做什么（**项目不是唯一的事**）
- [9月12AST提取](9月12AST提取.md) —— 图模型与社区版限制的来源
