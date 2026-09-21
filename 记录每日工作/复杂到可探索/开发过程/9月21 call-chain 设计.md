# 9月21 P1 工具设计与打样（call-chain + method-body）

> 承接 [9月19 Agent v1设计](9月19%20Agent%20v1设计.md) §3 与 [9月20 第一阶段问答边界](9月20%20第一阶段问答边界.md) §3。
> 9/19 定了 5 个工具的分工，9/20 指出了「撞接口」这道墙并留了一条**待确认**。
> **本篇先把那条待确认打成实测数据，再据此定 P1 两个新算法。**（§1–§11 = call-chain，§12 = method-body）
> 结论先给：**墙是真的，墙后有路，但第二堵墙是死墙。变长 Cypher 要废弃，改逐层 BFS。**

---

## 0. 今天定了什么

| #   | 决定                                                           |
| --- | ------------------------------------------------------------ |
| 1   | **先打样再设计** —— 用 hmall 真实图数据验证断点形态，不靠推断                       |
| 2   | 断点形态确认：**边存在，指向接口方法节点，该节点无出边**（情况 A，非情况 B）                   |
| 3   | **IMPLEMENTS 扇出方案成立** —— 反向边在图上，墙后有路                         |
| 4   | **Mapper 是死墙** —— MyBatis 接口在项目内无实现类，链路在此真实终止                |
| 5   | ★ **废弃变长 Cypher `CALLS*1..N`，改 Java 侧逐层 BFS** —— 扇出必须中途换关系类型 |
| 6   | **扇出不计入 depth** —— 绕行只是「找门」，不产生新信息                           |
| 7   | **`direction=UP` 只做 1-hop** —— 往上追是多跳发散，信息价值低                |
| 8   | **起点支持方法级**（`methodUid` 可选）—— 整类出发会把多条链路混在一起                 |

---

## 1. 为什么先打样

9/20 §3 留了一句：

> ⏳ 待确认：具体断法（是停在接口方法节点，还是整条边被丢弃）需要 P1 打真实 hmall 数据看一次，**不要凭推断下结论**。

这个断法有两种可能，**在查询结果上完全一样**（都是「链断了」），但在代码里是两件不同的事：

|     | 断法                 | 图上表现                                            | 算法后果                              |
| --- | ------------------ | ----------------------------------------------- | --------------------------------- |
| A   | 边**建了**，指向接口方法     | `Controller → IOrderService#m` 存在，但该接口方法**无出边** | 扇出**可行** —— 断点明确，也知道往哪个接口扇        |
| B   | 边**压根没建**（定名失败被丢弃） | `Controller` 的下游列表里**根本没有**这个接口方法               | 扇出**无从下手**，得先修 `RelationResolver` |

**两种都不报错** —— `RelationResolver.resolveCall` 里 `if (targetClass == null) return;` 是静默返回，不记日志。所以只能实测。

### 打样对象

- 项目：hmall（`projectId = 2099414853440290817`）
- 入口：Neo4j Browser（`http://<服务器IP>:7474`），直接跑 Cypher，**零代码改动**

### 五段查询与各自排除的可能

| #   | 查询                               | 结果                                          | 排除了什么                   |
| --- | -------------------------------- | ------------------------------------------- | ----------------------- |
| 1   | 接口的真实全限定名                        | `com.hmall.service.IOrderService`（**I 前缀**） | 排除「用推测的名字查，后面全空」        |
| 2   | `IOrderService` 方法的出边            | **0 行**                                     | 排除「链其实是通的」→ **墙真实存在**   |
| 3   | `IOrderService` 的方法节点数           | **2**                                       | 排除「接口方法根本没进图」这个更糟的可能    |
| 4   | 谁 `IMPLEMENTS` 了 `IOrderService` | `OrderServiceImpl`                          | 排除「扇出方案作废」→ **墙后有路**    |
| 5   | `OrderController` 的下游目标类型        | `INTERFACE`                                 | 排除「接口是死代码」→ **证据链闭合**   |
| 6   | 各 `*Mapper` 接口的实现类数              | **全为 0**                                    | 排除「还能再往下挖」→ **第二堵墙是死墙** |

> **第 3 段是必需的**：第 2 段返回 0 行有两种含义 —— 「方法在图上但没下游」和「方法压根没进图」。
> 只有确认节点存在，才能把问题定位在**关系层**而不是**提取层**。

---

## 2. 三条结论

### ① 墙是真的，而且是常态

```
OrderController#createOrder
        │ CALLS
        ▼
IOrderService#createOrder(OrderFormDTO)   ⛔ 断在这里，无出边
```

`@Autowired IOrderService` 的字段类型是接口 → 边指向**接口方法** → 接口方法没有方法体 → 没有出边。

**只要项目用 `@Autowired` 注入接口（标准 Spring 项目全都是），这个断点就必然出现。** 不是偶发，是主流程常态。

### ② 墙后有路 —— IMPLEMENTS 扇出成立

`OrderServiceImpl -[:IMPLEMENTS]-> IOrderService` 这条边**已经在图上**（`RelationResolver` 写的方向是 `实现类 → 接口`）。

所以走到接口断点时，**不报 `NO_DOWNSTREAM_EDGE`，而是反向扇出到实现类**：

> 「OrderService 是接口，实现类是 OrderServiceImpl，下面讲的是它的实现。」

**死路 → 分支点。** 这是本阶段性价比最高的一次修正，**不用动 AST**。

### ③ 第二堵墙是死墙

```
OrderServiceImpl ──CALLS──▶ OrderMapper   ⛔ 到此为止
```

`*Mapper` 接口的实现类数**全是 0** —— MyBatis 的实现是**运行时动态代理生成的，源码里不存在**。

**这不是缺陷，是已知边界。** 但它决定了一件事：`gaps` 的 `reason` **不能只有一个 `NO_DOWNSTREAM_EDGE`**，否则 Agent 只能说「没找到」，而三种「没找到」对用户是完全不同的信息。

### ④ 实测链路（trade-service，2026-09-21 验收数据）

以 `com.hmall.trade.service.IOrderService` 为起点、`DOWN`、`maxDepth=2`：

```
IOrderService (INTERFACE, trade-service, depth 0)
      │ IMPLEMENTS（扇出，不计 depth）
      ▼
OrderServiceImpl (CLASS, trade-service, depth 0, viaFanout=true)
      ├─ ItemClient        (hm-api,   depth 1) ⛔ REMOTE_SERVICE
      ├─ CartClient        (hm-api,   depth 1) ⛔ REMOTE_SERVICE
      ├─ UserContext       (hm-common, depth 1)
      ├─ BadRequestException(hm-common, depth 1)
      └─ 自调用 buildDetails（不画边，但继续遍历）
```

`gaps` 两条，均为 `REMOTE_SERVICE`。**Agent 由此可以讲出**：

> 「createOrder 会**查商品、扣库存**（调用 item-service）、**清空购物车**（调用 cart-service），然后组装订单明细。」

**三次「扇出不计 depth」的验证点**：`OrderServiceImpl` 的 `depth = 0` 而非 1 —— 它和 `IOrderService` 是同一个逻辑位置。

### 附：打样中发现的仓库问题（非算法问题）

hmall 图上存在**两个 `OrderController`**：

```
com.hmall.controller.OrderController          ← 用户端
com.hmall.trade.controller.OrderController    ← trade 模块
```

两者都注入 `IOrderService`、都调 `BeanUtils.copyBean`，导致同类记录出现两遍。

**影响**：`ProjectIndex.bySimpleName` 对同名类判定为歧义并丢弃（宁可漏不要错），所以短名 `"OrderController"` 无法解析。这不影响 `@Autowired` 那类字段解析（走 `localTypes` → 全限定名），但**影响用户直接说类名时的消解** —— Agent 需要意识到「OrderController 有两个」。

**已结案（9/21 实测）**：该 zip 是**微服务版完整仓库**，共 9 个模块 181 个类：

```
hm-service 68 / hm-common 21 / trade-service 19 / user-service 18
item-service 12 / pay-service 12 / hm-api 11 / cart-service 10 / hm-gateway 10
```

`hm-service`（老单体参考实现）与 `trade-service`（微服务版）**并存是仓库本身的结构**，不是上传混了。

**但由此暴露两个真问题**（都已记待办）：

1. **同名歧义是永久性的** —— 不止 `OrderController`，`IOrderService` 也有两个（`com.hmall.service.IOrderService` / `com.hmall.trade.service.IOrderService`）。加上 `ProjectIndex.bySimpleName` 对同名类丢弃，**Agent 必须拿到全限定名才能定位**，比 9/20 预判的更严格。
2. **`classes` 工具有排序偏差** —— 默认 `limit=40` 而 Cypher 按 `packageName, name` 排序，181 个类里 LLM 只看到字母序前 40 个（`cart`/`common`/`config`），`trade` 一个都看不到。而 `classes` 的用途正是「兜底让 LLM 在清单里选」，**清单有偏差则兜底失效**。应改为按关联度排序。

---

## 3. 契约

```jsonc
// 入参 —— projectId 由 Java 注入，LLM 不可指定
{
  "projectId": 1001,
  "uid": "com.hmall.service.IOrderService",   // 起点类全限定名
  "methodUid": null,                          // 可选：方法级起点（见 §6-③）
  "direction": "DOWN",                        // DOWN | UP
  "maxDepth": 3,                              // 服务端再 clamp 一次
  "maxNodes": 40
}

// 出参 data
{
  "start": { "uid": "...", "name": "...", "kind": "CLASS" },
  "nodes": [
    { "uid": "...", "name": "...", "kind": "CLASS",
      "depth": 1, "viaFanout": false, "edgeCount": 2 }
  ],
  "edges": [
    { "from": "...", "to": "...", "callCount": 3, "viaFanout": true }
  ],
  "gaps": [
    { "at": "com.hmall.mapper.OrderMapper", "reason": "INTERFACE_NO_IMPLEMENTATION" }
  ]
}
```

统一信封（`ok` / `evidence` / `truncated` / `note` / `graphRev`）见 9/19 §3，此处不重复。

### `gaps` 的 reason（打样后从 3 种增至 4 种）

| reason                        | 含义         | Agent 该说什么              | V1 是否产出 |
| ----------------------------- | ---------- | ----------------------- | ------- |
| `REMOTE_SERVICE`              | 目标是 API 契约模块里的接口，实现在**另一个服务** | 「调用了 item-service / cart-service」 | ✅ |
| `INTERFACE_NO_IMPLEMENTATION` | 接口在项目内没有任何实现类 | 「这是接口，项目内没有实现类，后续链路未解析」 | ✅ |
| `NO_DOWNSTREAM_EDGE`          | 保留值 | — | ❌ 不产出（见下） |
| `EXTERNAL_DEPENDENCY`         | 保留值 | — | ❌ 不产出（见下） |

**为什么加 `REMOTE_SERVICE`**：打样 trade-service 时发现，`OrderServiceImpl#createOrder` 的下游是 `CartClient` / `ItemClient` —— 它们**也是接口、也没有实现类**，和 Mapper 长得一模一样，但语义完全不同：

|          | Mapper                | Feign Client              |
| -------- | --------------------- | ------------------------- |
| 实现在哪     | 运行时动态代理（SQL 未解析）      | **另一个服务里**                |
| 该说       | 「SQL 映射未解析」           | 「**这通调用跑到别的服务去了**」        |

只说 `INTERFACE_NO_IMPLEMENTATION` 会把「跨服务调用」讲成「未解析」，**丢掉微服务项目里最有价值的信息**。

> **检测规则（启发式，靠命名约定）**：目标接口无实现类，且**所在模块名以 `-api` 结尾**（或包名含 `.client` / `.feign`）→ 判为 `REMOTE_SERVICE`。
> **局限**：不按这套命名规范的项目会判错。但判错的后果是「把跨模块调用说成远程调用」，**不会编造** —— 比漏掉好。

**为什么后两种不产出**：

- `NO_DOWNSTREAM_EDGE`：普通类方法没有下游说明它是**正常叶子**，从 `edges` 里本来就看得出，报成「断链」只是噪声
- `EXTERNAL_DEPENDENCY`：调项目外 jar 时 `RelationResolver.usable()` 解析失败、**边压根不会建**，图上无从检出

> **四种说法对用户是完全不同的信息。** 混成一句「没找到」等于什么都没说 —— 这正是 9/19 §8-A4（LLM 用常识补全断链）的防线。

### 实现中定下的三个细节

| 细节 | 决定 | 理由 |
| --- | --- | --- |
| **自环边** | **不画**（本类方法调本类方法） | 类级视图里它只是个圈，信息价值低。但**不能中断遍历** —— 那条调用的下游还要继续走 |
| **`ChainEdge.callCount`** | CALLS = 方法级调用边数；IMPLEMENTS = **配对成功的接口方法数** | 后者是诊断信号：接口 3 个方法而这里只有 2，说明有一个没扇出去 |
| **UP 方向反向扇出** | 起点是**实现类**时，把它实现的接口上同签名方法的调用方也算进来 | `@Autowired` 注入的是接口，直接调实现类的人极少；不反向扇出，问「谁调用 OrderServiceImpl」会得到 **0** |

---

## 4. ★ 核心决定：废弃变长 Cypher，改 Java 侧逐层 BFS

9/19 §3 原方案是一条变长查询：

```cypher
MATCH path = (start:Method {projectId:$pid, owner:$uid})-[:CALLS*1..3]->(m:Method {projectId:$pid})
```

**打样后认为应该废弃**，三个理由：

### 理由 1：扇出**根本没法**用变长模式表达

```cypher
(a)-[:CALLS*1..3]->(b)
```

这个模式只能沿**一种**关系一直往下走。真实的链路是**换关系类型**的：

```
Controller ──CALLS──▶ IOrderService#createOrder   ⛔ CALLS 到此断了
                            │ 换成 IMPLEMENTS + signature 配对
                            ▼
                    OrderServiceImpl#createOrder ──CALLS──▶ ...
```

**走到一半要换关系类型，变长模式表达不了。** 只要接受扇出方案，就必然是逐层推进。

### 理由 2：组合爆炸可控

变长路径在稠密图上是**引擎内部枚举所有路径**，外层 `LIMIT` 拦不住。逐层 BFS 是每层一条 1-hop，**每层都能在 Cypher 里 `LIMIT`，下一层根本不会被查询**。

> 这正是 9/19 §8-C3 标注的头号风险（打爆 Neo4j）的正面解法。

### 理由 3：`gaps` 在断点自然产生

逐层走时，「这一层没有下一层」是个**自然事件**，顺手记进 `gaps`。变长查询只知道「总深度是 1」，**不知道断在哪、为什么断**。

> **代价**：代码多几十行、多 2–3 次本地往返。本地 Neo4j 上无感，换掉的是一条真实的稳定性风险。

---

## 5. 算法

```
frontier = 起点类的全部方法（或 methodUid 指定的单个方法）
visited  = {}
result   = []   // 折叠后的类级节点
gaps     = []

for level in 1..maxDepth:
    # ① 整层一条 Cypher（不逐方法查，避免 N 次往返）
    callees = BATCH_1HOP(frontier)

    # ② 断点处置
    对每个无出边的方法 m：
        若 owner(m) 是 INTERFACE 且反向 IMPLEMENTS 有实现
            → 扇出：取「实现类中 signature 相同的方法」进 nextFrontier
            → **不计 depth**（见 §6-①）
        若 是 INTERFACE 且无实现          → gaps += INTERFACE_NO_IMPLEMENTATION
        若 目标在项目外                    → gaps += EXTERNAL_DEPENDENCY
        其他                              → gaps += NO_DOWNSTREAM_EDGE

    frontier = dedupe(nextFrontier) - visited
    visited += frontier
    if |frontier| > maxNodes: 截断 + truncated = true
    result += fold(frontier)          # 方法级 → 类级
    if frontier 为空: break
```

### 类级折叠

LLM 看不了大图也不该看 —— 500 条方法级边喂进 prompt 是纯噪声。

折叠规则：`方法→方法` 的 N 条边 → `类→类` 的 1 个节点，带 `depth`（最短跳数）、`callCount`（边数）、`viaFanout`（是否经由扇出）。

> 实现上可复用 `CodeGraphQueryServiceImpl.aggregateCalls` 的「按目标类 uid 归并」写法（`LinkedHashMap` 保序），区别是 call-chain 要多带 `depth` 和 `viaFanout` 两个维度。

---

## 6. 三个设计决定

### ① 扇出**不计入** depth

|          | 算                    | 不算（选它）              |
| -------- | -------------------- | ------------------- |
| hmall 上有效深度 | 约减半（Controller→接口→实现 吃掉 2 层） | 3 层全是真实类跳跃          |
| 风险       | 核心场景只能讲 1–2 跳        | 需 `visited` 防「接口→实现→扇回接口」空转 |

**理由**：`IOrderService#m` 和 `OrderServiceImpl#m` 是同一个方法的两面（声明 vs 代码）。从接口绕到实现**没有看到任何新东西**，只是终于找着门了。把「找门」算成走了路，用户会觉得预算莫名其妙就没了。

> hmall 这种项目**每个 Service 都要绕一次**，差别在每一层都会吃到。

### ② `direction=UP` 只做 1-hop

**理由**：往下走是**顺藤摸瓜**（业务流程，有主线）；往上走是**捞针** —— 任何入口最终都可能调到你，多跳会迅速发散到全项目，信息量低、噪音大。

> 用户若要更深的「谁调我」，**多问几次**即可：点一下调用方，再问它上面是谁。每层由用户决定要不要继续，比一次拉出一团更可控。

### ③ 起点支持方法级（`methodUid` 可选）

**问题**：从**整个类**出发会把该类所有方法的下游**混在一起**：

```
OrderController
  createOrder          → IOrderService.createOrder
  queryOrderById       → IOrderService.queryOrderById + BeanUtils.copyBean
  markOrderPaySuccess  → IOrderService.markOrderPaySuccess
```

混起来查，LLM 讲不清「创建订单这条线是 A→B→C」—— 查询和物流的单子搅在一起了。

**方案**：支持在类的基础上再指定方法；不指定时退化为整类（兼容 9/19 原设计，不算破坏）。

> **正好对上产品流程**：用户点类 → 传类 → 「这个类大致在干嘛」；用户追问「这个方法呢」 → 传方法 → 「这条流程具体怎么走」。
> **不是给后端加负担，是把前端的交互顺序自然地支持起来。**

---

## 7. Cypher 模板

### 每层一条（整批方法一起查）

```cypher
MATCH (m:Method {projectId:$pid})-[:CALLS]->(t:Method {projectId:$pid})
WHERE m.uid IN $uids
RETURN m.uid AS fromMethod, m.owner AS fromClass,
       t.uid AS toMethod,   t.owner AS toClass
```

### 扇出（方法级配对，靠 signature 相等）

```cypher
MATCH (impl:Class {projectId:$pid})-[:IMPLEMENTS]->(iface:Class {projectId:$pid, uid:$ifaceUid})
MATCH (im:Method {projectId:$pid, owner: impl.uid, signature: $ifaceSignature})
RETURN im.uid AS implMethod, im.owner AS implClass
```

> **为什么能靠 signature 相等配对**：`AstParserServiceImpl.buildSignature` 用的是参数类型的**源码字面量**（`OrderFormDTO`，不是 `com.hmall.dto.OrderFormDTO`）。接口与实现类声明同一个方法时，这个字符串**必然相同**，且 `signature` 已经是 Method 节点上的属性 —— 直接 join，不用比参数个数、不用解析类型。
>
> 这是 AST 阶段设计留下的红利。

### 反查调用方（UP，1-hop）

```cypher
MATCH (c:Method {projectId:$pid})-[:CALLS]->(t:Method {projectId:$pid, owner:$uid})
RETURN c.uid, c.owner, t.uid
```

---

## 8. 上限、截断与 evidence

| 参数              | 值    | 落点                        |
| --------------- | ---- | ------------------------- |
| `maxDepth`      | 3    | 服务端 clamp（不信调用方传值）        |
| `maxNodes`      | 40   | 每层截断，超出即 `truncated=true` |
| 工具级超时           | 8s   | exec 层统一包                  |

**截断排序**：按 `(depth, class uid)` 排序后取前 N。

**`truncated=true` 是强制项** —— 不说的话，LLM 会把「只看到 3 个」讲成「这个类只调用 3 个」。这是最难发现的错误。

**`evidence` 逐条保留方法级边**，即使返回给 LLM 的是折叠后的类级视图 —— 因为后续 Java 侧要用它校验 `uiCommands[].target.uid`（9/19 §8-A2）。

---

## 9. P1 落点

| 文件                                              | 内容                                       |
| ----------------------------------------------- | ---------------------------------------- |
| `service/ast/CodeGraphQueryService`             | 加 `callChain(...)` 接口方法                  |
| `service/impl/CodeGraphQueryServiceImpl`        | 实现逐层 BFS（复用 `aggregateCalls` 折叠写法）       |
| `dto/response/CallChainResponse`                | 新增：`nodes` / `edges` / `gaps` / `truncated` |
| `controller/`（新 `/internal/tools/*`）            | `POST /internal/tools/call-chain` 转发     |

**P1 验收动作**：curl 直接打，hmall 上以 `IOrderService` 为起点、`DOWN` 方向 2 层，**人工对拍**是否与 §2 的链路一致。

---

## 10. 未决与风险

| # | 项                                    | 说明                                                        |
| - | ------------------------------------ | --------------------------------------------------------- |
| 1 | **两个 `OrderController`**              | 待确认 zip 是否装了两套 hmall。若是，属上传源头问题，需在 `classes` 层做去重或标注 |
| 2 | **`graphRev` 尚无生成方式**                | 全项目无图版本号概念。建议在写图同事务内 `MERGE (:GraphMeta)` 递增（详见会话讨论），**注意该节点不能带 `:CodeNode` label**，否则每次重写图会被 `CLEAR` 清零 |
| 3 | **`method-body` 尚未设计**                | P1 第二个新算法。链路（Method 行号 → `file` 表路径 → 磁盘切片）已明确，但切片粒度、`startLine` 是否含 javadoc、重载如何挑，**都要打样后定** |
| 4 | **多实现类的扇出歧义**                        | 本次 hmall 上 `IOrderService` 只有 1 个实现，无歧义。但**设计上不能假设总是 1 个** —— 一个接口多实现是常态，此时应全部返回并标注歧义，让 Agent 说「这里有两个实现，不确定走哪个」，而不是随便选一个 |
| 5 | **`RelationResolver` 的静默丢弃无日志**       | 定名失败时 `if (targetClass == null) return;` 不记日志。情况 B 类问题**在图上不可见**，排查困难。建议加 warn 日志（低成本） |

---

## 12. P1 第二个新算法：method-body（打样后定稿）

> 9/19 §3 只写了「`Method.fileId + 行号` → `file` 表 → 磁盘切片」这条链路，**没有打样**。
> 按 call-chain 的教训**先打样再设计**，结论如下。

### 打样对象与结论

对 `trade-service` 的 `OrderServiceImpl` 逐行比对真实源码：

| # | 结论 | 证据 |
| - | --- | --- |
| 1 | **`startLine` 从第一个注解算起，不含 javadoc** | 类 `startLine=37` → 第 37 行是 `@Service`（类 javadoc 在 33–36，未计入）；`createOrder` `startLine=46` → 46=`@Override`、47=`@GlobalTransactional`、48 才是签名 |
| 2 | `endLine` 是方法闭合 `}`，方法体完整 | `buildDetails` 118–132、`cancelOrder` 111–116 全部对齐 |
| 3 | **接口方法是单行**（无方法体） | `IOrderService` 三个方法 `17-17` / `19-19` / `21-21`，源码确认为单行声明 |
| 4 | `fileId` 一致，路径可 resolve | 类与其 4 个方法同一 `fileId`；`project_source.content` + `filePath` 能定位到磁盘文件 |
| 5 | 重载真实存在但**天然不撞键** | `PageDTO.of` 有 5 个重载；`methodUid = owner#signature` 含参数类型，精确匹配即可 |

> **结论 1 是好消息**：`@GlobalTransactional`（Seata 分布式事务）会被自动切进去。若只从签名行开始切，这类关键语义就丢了 —— **不用额外写代码，AST 阶段的设计白送了这个红利**。
>
> **结论 3 是坏消息**：call-chain 的扇出会让 Agent 手里**全是接口方法的 uid**。直接切只能得到一行声明，等于什么都没讲。

### 两个设计决定

**① 只给类、不给方法时：public 优先，再按行号**

用户问「这个类怎么实现的」，想看的是**对外方法**而不是私有辅助。
Cypher 里 `ORDER BY CASE WHEN m.visibility = 'public' THEN 0 ELSE 1 END, m.startLine`。

**② 撞到接口方法时：自动跳到实现类**

顺 `IMPLEMENTS + signature 相等` 找到实现类方法，切它的代码，响应里用 `resolvedFrom` 标明原接口方法 uid。

> 复用 call-chain 里那条现成的扇出查询，**代码几乎是白捡的**。
> 也符合「图结构不交给 LLM 拼」的原则 —— 让工具悄悄替它走这步，比让 LLM 自己意识到「该去查实现类」可靠得多。
> 实践中 Agent 手里大部分 method uid 都来自 call-chain 的 evidence，**恰好全是要跳的那种**。

### 契约与硬约束

```jsonc
// 入参
{ "projectId": 1001, "uid": "com.…OrderServiceImpl", "methodUid": null,
  "maxMethods": 3, "maxLines": 120 }

// 出参 data.methods[]
{ "uid": "…OrderServiceImpl#createOrder(OrderFormDTO)", "owner": "…OrderServiceImpl",
  "signature": "createOrder(OrderFormDTO)", "startLine": 46, "endLine": 100,
  "code": "@Override\n@GlobalTransactional\npublic Long createOrder(…) {…}",
  "error": null,                // 源码读不到时填原因，其余方法照常返回
  "resolvedFrom": null }        // 自动跳实现类时填原接口方法 uid
```

`maxMethods ≤ 3` / `maxLines ≤ 120` 服务端再 clamp，超限一律 `truncated = true`。

### 三个防坑点

| 坑 | 防法 |
| --- | --- |
| **路径穿越** | `resolve(filePath).normalize()` 后**强制校验仍在 extractDir 之下**；读盘复用 `FileContentService` 已有的 `NOFOLLOW_LINKS` 与 10MB 上限 |
| **同文件重复读盘** | 一次请求内按 `fileId` 缓存文件行数组（同类多方法常在同一文件） |
| **单方法失败拖垮整批** | 单个方法源码读不到 → 记进该方法自己的 `error` + 顶层 `note`，**其余照常返回** |

---

## 附：与既有文档的关系

- [9月19 Agent v1设计](9月19%20Agent%20v1设计.md) —— 服务边界 / 工具契约 / 闭环时序 / P0–P7 执行步骤；本篇是其 P1 中 call-chain 的展开
- [9月20 第一阶段问答边界](9月20%20第一阶段问答边界.md) —— 定 Agent 能答到哪；§3 的待确认项由本篇打样回答
- [9月12AST提取](9月12AST提取.md) —— 图模型（`Method` 节点属性、`methodUid` 格式）与社区版限制的来源
- [9月16 用户体验与agent设计](9月16%20用户体验与agent设计.md) —— Agent 要做什么（Context / Tool / UICommand / 边界四问）
