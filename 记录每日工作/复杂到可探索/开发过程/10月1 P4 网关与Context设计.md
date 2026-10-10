# 10月1 P4 网关与 Context 设计

> 承接 [9月19 Agent v1设计](9月19%20Agent%20v1设计.md) §9 的 P4 执行步骤、§7 的 Context 结构、§5 的接口清单。
> P3 已于 2026-10-01 交付（commit `4ad142f`；与其配套的 [9月22 P2 Python骨架设计](9月22%20P2%20Python骨架设计.md) 是前一段）。
> **P3 让 Agent 有了脑子，但它是没装门的**——只能从 Python 的 8000 端口直接 curl，无鉴权、无租户隔离、重启即丢会话。
> **P4 就是把门装上。**
> **本篇是设计，不含代码改动。**

---

## 0. P4 的边界

**P4 = Java 公网网关 + Context 从内存搬进 Redis + viewport 反向通道。**
（原计划的 ack 已于 2026-10-10 砍掉 —— 改走快照驱动，见 §3.5。）

验收（设计 §9 原文）：**打 Java 接口能通到 Python；rev 乱序被拒**。

不做：前端改造（P5）、容器化（P6）、SSE（P7）、token 配额/BYOK（先不做，不影响 Tier 1 落地）。

---

## 1. 现状与目标

```
现在（P3 结束）：
   curl ──► Python :8000 ──► Java :8080/internal/tools ──► Neo4j
            ✗ 无鉴权   ✗ 无租户校验   ✗ Context 重启即丢

P4 之后：
   前端 ──JWT──► Java /api/agent/ask ──内部token──► Python ──► Java /internal/tools ──► Neo4j
                    │                                  │
                    │ ① JWT 校验                        ├─ Redis db1  Context (TTL 2h)
                    │ ② CheckProjectUtil 租户校验        └─ DeepSeek
                    │ ③ uiCommands 二次校验
                    └─ ④ 超时预算 ← ⚠️ §3.2，设计原文自相矛盾，必须改
```

**P4 存在的真正理由不是「加个代理」**，而是补上**租户边界**：
今天 Python 拿到的 `projectId` 是调用方给的，**没有任何东西保证它属于当前用户**。
`CheckProjectUtil.checkProject` 必须补在 Java 这一层 —— 这才是「Python 不碰数据库」那条边界的完整闭环。

---

## 2. 交付物

### 2.1 Java 侧新增：`/api/agent/*`（公网入口，带 JWT）

| 接口 | 用途 | 关键动作 |
| --- | --- | --- |
| `POST /api/agent/ask` | 一轮问答 | JWT → **租户校验** → 补 `X-Internal-Token` → 转发 Python |
| `POST /api/agent/navigate` | 点按钮导航（展开 / 退回去） | JWT → **租户校验** → 转发；**不调 LLM**（§3.3 决策） |
| `GET /api/agent/conversations/{id}` | 恢复会话（刷新页面用） | 从 Python 读 Context 还原 |
| `POST /api/agent/conversations/{id}/viewport` | 前端上报屏幕状态 | 转发 → Python 只接受 rev 递增 |
| ~~`POST /api/agent/conversations/{id}/ack`~~ | ~~UICommand 执行回执~~ | ❌ **已砍**（2026-10-10，见 §3.5：改走快照驱动，不再重放命令） |

### 2.2 Python 侧新增

| 文件 | 内容 |
| --- | --- |
| `app/store/context_store.py` | ✅ **P4.1**：Redis db1 读写；key `agent:ctx:{projectId}:{conversationId}`；TTL 2h。**P4.2 补 `load_or_none()`** |
| `app/api/agent.py` | ✅ **P4.1**：内存 dict → store。**P4.2 补两个内部路由**：viewport 上报 + 会话读回（ack 已砍） |
| `app/models/context.py` | **P4.2 补 `Viewport` / `RelationRef` 模型**（P3 故意没定义，避免留下没人用的死字段） |

### 2.3 Context 的最终形状（设计 §7，**加粗**的是 P3 已实现）

```jsonc
{
  "version": 1, "conversationId": "...", "projectId": 1001,
  "focus": {...}, "focusStack": [...],          // ← P3 已有
  "history": [...], "budget": {...},            // ← P3 已有
  "viewport": {                                 // ← ★ P4.2 新增（P4.1 故意留空）
    "rev": 17,                                  // 单调递增，前端上报
    "visibleNodes": [...],                      // 硬上限 60
    "visibleRelations": [{from, to, rel}, ...],
    "focusedNode": {...} | null,                // ← ★ 补 FOCUS_NODE 的「选中」语义
    "truncated": false                          // 超 60 置 true
  }
}
```

> **相对原设计的改动（2026-10-10，理由见 §3.5）**：删掉 `lastUiCommands`、加 `focusedNode`。
> `graphReady` **暂不定义** —— 通篇找不到消费者，按 P3 那条「没有读取方的字段就是死字段」的原则，等真要用再加。

### 2.4 三条设计决定（设计 §7 原文，P4 是它们的落地时刻）

1. **`viewport` 的真相在前端，不在后端** —— Context 里的 `visibleNodes` 是「前端最后一次上报的屏幕状态」，不是后端推断的。所以必须有反向接口 + `rev` 单调递增，后端只接受更新的 rev。
2. ~~**UICommand 必须有 ack**~~ —— ~~前端执行完回执，后端才写入 `viewport.lastUiCommands`。**没有 ack，Context 必然和屏幕漂移。** 顺带解决刷新后的图重放。~~
   > ❌ **已作废（2026-10-10）** —— 改走**快照驱动**，不再重放命令，因此不需要 ack。
   > 理由见 §3.5。**决定① 不但保留，而且是这次改动的依据。**
3. **Redis db1 是主存储不是缓存** —— 会话本就有生命周期，丢了等于这次对话结束。TTL 2h。

---

## 3. ✅ 三个决策（P4.0 已拍板，2026-10-09）

### 3.1 Java 拿不到 evidence，怎么校验 `uiCommands`？

设计 §8-A2 写的是「**Java 侧**校验 `target.uid ∈ evidence`，未命中直接丢弃」。
但 evidence 是 Python 调 Java 工具时拿到的——**响应回到 Java 手上时，evidence 在 Python 里，Java 看不见**。

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| **A**（倾向） | Java 直接查图：uid 是不是这个项目的节点 | 无状态、最简单；校验弱（只验"存在"，不验"本轮查过"） |
| B | Java 缓存本轮发出去的 evidence | 校验强；要存状态，与「Java 无状态网关」冲突 |
| C | Python 在响应里带上 evidence | 校验最强；契约要加字段，且字段会被 P5 前端看到 |

**倾向 A 的理由**：P3 的 `guard.filter_ui_commands` 已在 Python 侧做过强校验，Java 这一层是**防御 Python 有 bug** 的第二道，「节点存在性」够用。

> **✅ 决策（2026-10-09）：采 A —— 弱校验，只验「uid 是这个项目的节点」。**
>
> **一处措辞纠正**：这不是「先弱、以后再补强」——**A 就是终态**。
> 强校验的真相源是「**本轮查过什么**」（evidence），而这个信息只在 Python 手里：
> 它是 Python 调 Java 工具时拿到的。Java 想复刻就得缓存每轮发出去的 evidence（方案 B），
> 那等于把刚立起来的「Java 无状态网关」拆掉。**强校验的位置本来就在 Python**，
> Java 这层是第二道，不存在「以后升级」。
>
> **落地提醒**：一条响应可能带 N 条 uiCommand，**别逐条查 Neo4j**，
> batch 成一次 `WHERE uid IN [...]`。

### 3.2 ⚠️ 超时预算自相矛盾（必须改）

设计 §8-C1 原文：

> **两端都要设**：Java 代理 Python **8s**、Python 调 Java 8s、**整轮 60s**

**这三条合起来是矛盾的**：Java 代理 Python 只给 8s，但 Python 一轮要调**两次 LLM**。
实测（2026-10-01，hmall）一轮 = Intent 620ms + 工具 120ms + 讲解 1815ms ≈ **2.5s**，8s 够；
但 LLM 一旦慢到 10s，**Java 网关会在 Python 还没答完时断开**，用户看到网关超时而不是一个慢答案。

**建议改成**：Java 代理 Python **45–60s**（对齐整轮预算）；Python 调 Java 工具保持 **8s**；Python 单次 LLM 保持 **25s**。
这样 8s 只用来保护「工具不该慢」，不误伤「LLM 本来就慢」。

> **✅ 决策（2026-10-09）：按建议执行。**
> Java 代理 Python **45–60s**；Python 调 Java 工具 **8s**（`TOOL_TIMEOUT_SECONDS`，已在 `java_client.py`）；
> Python 单次 LLM **25s**（`settings.llm_timeout`，已配置）。
>
> **点明性质：这是「配置问题」，不是「拓扑问题」。** 很容易想到「让前端直连 Python 绕开网关」——
> **那是错的**：绕开之后浏览器 / nginx 照样有超时，Python 照样要等 LLM 25s，
> 只是把超时点从 Java 挪到了 nginx。**超时预算没配对，换拓扑不解决任何事。**
> 何况绕开会丢掉整个 P4 的租户边界（前端直接给 Python 一个没人校验过的 `projectId`）。
>
> **流式（SSE）不在这条线上**：流式解决的是「用户干等 25s 觉得卡死」这个**感知延迟**，
> 不是「连接被网关掐断」。而且流式连接活得更久，**会让超时问题更难**（要管 chunk 间隔而非总耗时）。
> 归 P7（§0 已划），P4 不碰。

### 3.3 NAVIGATE 能不能做到零 LLM

设计 §5：「用户点『展开』『退回去』时只走 ①②④，**不调工具、不调 LLM**」。
但 P3 实测 NAVIGATE 仍调了 1 次 LLM（575ms）—— **因为要先判出这是 NAVIGATE，就得调 LLM #1**。

要做到零 LLM，只能让前端在点按钮时**直接声明意图**（如 `POST /api/agent/navigate {op: "BACK"}`），跳过判定。
**现在拍板，P5 才不用改接口。**

> **✅ 决策（2026-10-09）：采「前端声明意图」—— 点按钮直接声明，跳过 LLM #1。**
>
> P4.3 新增 `POST /api/agent/navigate {op: "BACK" | "EXPAND", target?}`，前端点「退回去 / 展开」时
> **直接调它、不经过 `/ask`**，因此不调任何 LLM。
>
> **代价**：前端多一个接口要接（P5）。换来的正是设计 **E2**「用户点一下展开就烧一次 token」
> 要避免的那条成本决策 —— 点击变成**零 token**。
>
> ⚠️ **`/ask` 那条路仍然保留 NAVIGATE 判定，别删。** 用户完全可能在输入框里打字说「退回去」，
> 那就还是得靠 LLM #1 判出来。所以 `loop.py` 的 `_navigate` 分支**不动**——
> 两条路殊途同归（都落到同一个焦点栈出栈逻辑）。

---

## 3.5 ✅ P4.2 方案重做（2026-10-10：**快照驱动** + 过期即结束）

> **为什么重做**：原 §2.4 决定②（命令必须 ack）想解决「刷新后图重放」，用的是「前端执行完**回执**，后端才写 `lastUiCommands`」——
> 即**重放命令**。讨论后改走**快照驱动**：前端直接上报屏幕状态，刷新后**照快照画**，不重放任何命令。
> 因此 **ack 与 `lastUiCommands` 全部砍掉**。

### 为什么快照赢

| | 状态驱动（快照） | 命令驱动（重演） |
| --- | --- | --- |
| 语义完整度 | ⚠️ 取决于字段够不够全 | ✅ 天然带「选中/强调」语义 |
| **结果一致性** | ✅ 画的就是当时的屏幕 | ⚠️ **重新执行**，图数据一变结果就变 |
| 多轮累积 | ✅ 固定大小，永远完整 | ⚠️ 只存最后一批会丢；存全部会无限涨 |
| 幂等 | ✅ 画几次都一样 | ❌ 顺序/次数敏感 |
| 失败模式 | 要么画出，要么画不出 | **可能停在中间状态** |

**一句话**：命令驱动「语义更全」，快照驱动「结果更准」。
快照的短板（语义不全）**能靠补字段补掉**（`focusedNode`）；重演的短板（结果会漂）**补不掉**。

**你的项目里重演会漂的两个场景**：
1. **多轮累积** —— `lastUiCommands` 只存最后一批，重放会丢掉前几轮画出来的东西
2. **图数据会变** —— 项目可**重新分析**，重放 `SHOW_PATH` 会拿到「今天的邻居」，和刷新前不一样

### 决策清单

| 项 | 决定 | 理由 |
| --- | --- | --- |
| 刷新技术路线 | **快照驱动** | 见上 |
| ack / `lastUiCommands` | ❌ 砍掉 | 快照路线下后端**不假设**命令被执行，不需要确认 |
| **上报不许创建会话** | ✅ **用 `load_or_none`** | 「会话不存在」在 `/viewport` 是**故障信号**，不是正常业务；静默创建会把「会话已过期」埋掉 |
| 拒绝的表达 | **200 + 显式字段**，不用 4xx | 与 `ToolEnvelope` 同一条原则：业务状态不做成 HTTP 错误 |
| **持久化数据库** | ❌ **V1 不做** | 过期即结束（见下） |
| 乐观锁 | rev 比较，**不做真 CAS** | 够验收；记为已知债 |
| `graphReady` | ❌ 不加 | 无消费者 |
| `highlightedPath` | ❌ 暂不加 | 路径的边已在 `visibleRelations` 里，丢的只是高亮样式；前端确认需要再加 |

### 产品承诺（**要写下来，它决定 P5 的 UX**）

> **对话在最后一次活动后存续 2 小时；之后对话内容不再存在。**

**不做持久库的直接后果**：跨 2h 回来继续、历史会话列表、跨设备 —— 都做不到。
**前端因此必须自己维护一份会话列表，且只能存浏览器本地（localStorage）。**
换浏览器 / 换设备 / 清缓存 → 列表就没了。**这和「不持久化」自洽，V1 接受。**

**「过期」是正常状态，不是故障** —— 所以必须**如实告知**，不能静默抹平。
（与 P4.1「Redis 挂了要报错别降级」是同一条原则。）

### 模型

```python
class RelationRef(CamelModel):
    from_: str
    to: str
    rel: RelationType

class Viewport(CamelModel):
    rev: int = 0                            # 0 = 从未上报
    visible_nodes: list[str] = []           # 硬上限 60
    visible_relations: list[RelationRef] = []
    focused_node: NodeRef | None = None     # ★ 补 FOCUS_NODE 的「选中」语义
    truncated: bool = False

# AgentContext 加一个字段
viewport: Viewport = Viewport()
```

`focused_node` 是快照路线**必须**补的那个字段 —— `visible_nodes` 是**集合**，表达不了「谁在前台」。

**`ViewportReport`（请求契约）**：`rev` / `visibleNodes` / `visibleRelations` / `focusedNode`。
**`truncated` 不由前端报** —— 后端截断后自己置。

### `ContextStore` 加 `load_or_none`

```python
async def load_or_none(self, project_id, conversation_id) -> AgentContext | None:
    raw = await self._redis.get(self._key(project_id, conversation_id))
    if raw is None:
        return None                      # ★ 不建
    try:
        return AgentContext.model_validate_json(raw)
    except ValidationError:
        return None                      # ★ 坏了也 None（重建 = 创建，违背语义）
```

**两个方法分工**（**P4.1 决定①没错，它只适用于 `/ask`**）：

| 方法 | 不存在时 | 服务谁 | 因为那里「不存在」是 |
| --- | --- | --- | --- |
| `load` | 建一个 | `/ask`（无 cid 时） | **正常业务**（新会话从句提问开始） |
| `load_or_none` | `None` | `/viewport`、`GET /conversations/{id}` | **故障信号** |

**为什么不用 `exists()` + `load()`**：两次往返，且中间 TTL 可能刚好过期 → `load` 会偷偷建一个（TOCTOU）。
**为什么不用 `load(create=False)`**：布尔参数把语义藏进 `True/False`；方法名把它写在脸上。

### 路由

**`POST /internal/agent/viewport`（新）**

```
① ctx = await store.load_or_none(pid, cid)  ── None → 拒（SESSION_GONE）
② rev 检查：report.rev <= ctx.viewport.rev  ── → 拒（STALE_REV），回当前 rev
③ 写入快照：截断到 60，超了置 truncated=true
④ await store.save(ctx)
```

响应形状（**统一约定：200 + 显式字段**）：

```json
{ "accepted": true,  "reason": null,           "rev": 18 }
{ "accepted": false, "reason": "STALE_REV",    "rev": 18 }
{ "accepted": false, "reason": "SESSION_GONE", "rev": null }
```

⚠️ **被拒时必须回当前 `rev`** —— 否则前端会一直重发旧 rev，永远同步不上。**这是自愈的必要条件。**

**`GET /internal/agent/conversations/{id}`（新，补原设计的缺口）**

原设计 §2.1 列了 Java 侧「恢复会话（刷新页面用）」，但**没有任何地方说 Python 侧由谁提供数据** —— 这个洞一直没补。快照路线下它是**必需品**（「刷新恢复」的全部内容就是「读回快照」）。

返回 **`viewport` + `focus`**，**不给 `history`**（那是 Agent 的内部记忆，前端恢复屏幕用不上）。
不存在时：`{"exists": false, "reason": "SESSION_GONE"}`。

**`POST /internal/agent/ask` —— 加过期分支（本次决策的直接后果）**

现状：`load` 静默建空的 → 过期会话再提问会**"照答但失忆"，且用户毫无察觉**。

**V1 采方案 A：带了 cid 但 Context 不存在 → 直接返回「会话已过期」，不回答。**

| 请求 | 处置 |
| --- | --- |
| 没带 cid | 新会话，建（**不变**） |
| 带 cid 且 Context 在 | 正常恢复（**不变**） |
| **带 cid 但 Context 没了** | ← **新行为：返回过期，不回答** |

**判据是「调用方有没有带 cid」**，不是「上下文在不在」—— cid 是后端发的，带了就说明这是**一个曾经存在的会话**。

（前端处置：**灰色化该会话窗口**，用户点击提示「对话已过期，请开始新对话」。）

### `loop.py`

`_REV_PLACEHOLDER = 0` → `ctx.viewport.rev`。
发出去的 `UICmd.rev` 要标出「这条命令基于哪个版本的屏幕」；前端执行前对比自己的 rev，**旧的就丢** ——
防止后端基于旧屏幕的命令把新屏幕覆盖回去。**与上报的 rev 锁是同一机制的两端。**

### 验收

| # | 验收项 |
| --- | --- |
| 1 | **rev 乱序上报被拒**（设计原文） |
| 2 | **快照能读回来**（刷新恢复） |
| 3 | **过期会话上报/提问被拒**（reason = `SESSION_GONE`） |

### P4.2 明确不做

- ❌ ack / `lastUiCommands` / 命令重放
- ❌ 持久化数据库（过期即消失）
- ❌ 真 CAS（`WATCH` / Lua）—— 已知债
- ❌ `graphReady`、`highlightedPath` —— 等有消费者再加

---

## 4. 任务分解

| 步 | 做什么 | 验收 |
| --- | --- | --- |
| **P4.0** ✅ | 拍板 §3 三个问题 | 三个决策落地成文字 —— 2026-10-09 完成 |
| **P4.1** ✅ | Python：`store/context_store.py`；内存 dict → Redis | **重启 Python 后会话还在** —— 2026-10-09 已验收（`ba4ad55`）；`evict` 前缀清 Redis 也在这一步做完了 |
| **P4.2** | Python：viewport 上报 + 会话读回**两个**内部路由 + **rev 乐观锁** + **过期即结束**（**无 ack**，见 §3.5） | **rev 乱序上报被拒** / 快照能读回 / 过期会话被拒 |
| **P4.3** | Java：`AgentController` + 转发（JWT + 租户校验 + 内部 token + 超时）；含 `POST /navigate`（§3.3） | curl 打 Java 接口能通到 Python |
| **P4.4** | Java：uiCommands 二次校验（按 §3.1 的决定） | 构造不存在的 uid → 被丢弃 + 记 warn |
| **P4.5** | 收口：全链路 curl 冒烟 | §0 的两条验收全过 |

---

## 5. 与其它阶段的接口

- **上接 P3**：P3 的 `/internal/agent/ask` 形状**不变**——P4 只加一层代理，不改契约。
  `/admin/evict` 从「清内存 dict」改成「按 `agent:ctx:{projectId}:*` 前缀删 key」，**接口形状不变**。
- **下接 P5**：P4 定下的 4 个 `/api/agent/*` 接口 + 是否走「零 LLM 导航接口」直接决定 P5 前端怎么接。
- **后接 P6**：Python 容器不映射 ports、nginx 不路由 `/internal/**`（设计 §5 三道防线的网络层）。

---

## 6. 已知会带进 P4 的债

| 债 | 来源 | 影响 |
| --- | --- | --- |
| UP 路由 bug（`class-relations` UP vs `call-chain` UP 语义不一致） | P1 遗留，P3 暴露 | Agent 问实现类「谁调用了它」会答错 |
| ReAct 多步循环未做 | P3 主动挂起 | 一轮只查一次图 |
| `deploy/.env` 缺 `INTERNAL_TOKEN` | P2 记录 | P6 容器化会挡路 |
| `DEEPSEEK_API_KEY` 同时存在 `deploy/.env` 与 `agent-service/.env` | P3 | key 轮换要改两处 |
| `@Lazy` 临时破环 / 上传后未清旧概况 / 删项目未清 task 行 | 路线图自记 | 与 P4 无关，巩固期处理 |
| **rev 乐观锁不是真 CAS** | P4.2 主动接受 | `load → 改 → save` 之间无原子性：两个并发上报可能都通过检查、后写的覆盖先写的。单用户单浏览器实际风险低；真解决要 Redis `WATCH`/Lua |
| **无持久化库，会话 2h 后不可恢复** | P4.2 决策（V1 范围） | 跨设备继续、历史会话列表都做不到；前端只能靠 localStorage 维护本地列表 |
