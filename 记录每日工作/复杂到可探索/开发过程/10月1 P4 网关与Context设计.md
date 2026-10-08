# 10月1 P4 网关与 Context 设计

> 承接 [9月19 Agent v1设计](9月19%20Agent%20v1设计.md) §9 的 P4 执行步骤、§7 的 Context 结构、§5 的接口清单。
> P3 已于 2026-10-01 交付（commit `4ad142f`；与其配套的 [9月22 P2 Python骨架设计](9月22%20P2%20Python骨架设计.md) 是前一段）。
> **P3 让 Agent 有了脑子，但它是没装门的**——只能从 Python 的 8000 端口直接 curl，无鉴权、无租户隔离、重启即丢会话。
> **P4 就是把门装上。**
> **本篇是设计，不含代码改动。**

---

## 0. P4 的边界

**P4 = Java 公网网关 + Context 从内存搬进 Redis + viewport/ack 反向通道。**

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
| `GET /api/agent/conversations/{id}` | 恢复会话（刷新页面用） | 从 Python 读 Context 还原 |
| `POST /api/agent/conversations/{id}/viewport` | 前端上报屏幕状态 | 转发 → Python 只接受 rev 递增 |
| `POST /api/agent/conversations/{id}/ack` | UICommand 执行回执 | 转发 → Python 才写 `lastUiCommands` |

### 2.2 Python 侧新增

| 文件 | 内容 |
| --- | --- |
| `app/store/context_store.py` | Redis db1 读写；key `agent:ctx:{projectId}:{conversationId}`；**TTL 2h**；rev 乐观锁 |
| `app/api/agent.py` | 内存 dict → store；新增 viewport / ack 两个内部路由 |
| `app/models/context.py` | 补 `Viewport` 模型（P3 故意没定义，避免留下没人用的死字段） |

### 2.3 Context 的最终形状（设计 §7，**加粗**的是 P3 已实现）

```jsonc
{
  "version": 1, "conversationId": "...", "projectId": 1001,
  "focus": {...}, "focusStack": [...],          // ← P3 已有
  "history": [...], "budget": {...},            // ← P3 已有
  "graphReady": true,                           // ← ★ P4 补（P3 无可填之处）
  "viewport": {                                 // ← ★ P4 新增
    "rev": 17,                                  // 单调递增，前端上报
    "visibleNodes": [...],                      // 硬上限 60
    "visibleRelations": [...],
    "lastUiCommands": [],                       // 刷新后重放
    "truncated": false                          // 超 60 置 true
  }
}
```

### 2.4 三条设计决定（设计 §7 原文，P4 是它们的落地时刻）

1. **`viewport` 的真相在前端，不在后端** —— Context 里的 `visibleNodes` 是「前端最后一次上报的屏幕状态」，不是后端推断的。所以必须有反向接口 + `rev` 单调递增，后端只接受更新的 rev。
2. **UICommand 必须有 ack** —— 前端执行完回执，后端才写入 `viewport.lastUiCommands`。**没有 ack，Context 必然和屏幕漂移。** 顺带解决刷新后的图重放。
3. **Redis db1 是主存储不是缓存** —— 会话本就有生命周期，丢了等于这次对话结束。TTL 2h。

---

## 3. ⚠️ 三个必须先拍板的问题（不定会返工）

### 3.1 Java 拿不到 evidence，怎么校验 `uiCommands`？

设计 §8-A2 写的是「**Java 侧**校验 `target.uid ∈ evidence`，未命中直接丢弃」。
但 evidence 是 Python 调 Java 工具时拿到的——**响应回到 Java 手上时，evidence 在 Python 里，Java 看不见**。

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| **A**（倾向） | Java 直接查图：uid 是不是这个项目的节点 | 无状态、最简单；校验弱（只验"存在"，不验"本轮查过"） |
| B | Java 缓存本轮发出去的 evidence | 校验强；要存状态，与「Java 无状态网关」冲突 |
| C | Python 在响应里带上 evidence | 校验最强；契约要加字段，且字段会被 P5 前端看到 |

**倾向 A 的理由**：P3 的 `guard.filter_ui_commands` 已在 Python 侧做过强校验，Java 这一层是**防御 Python 有 bug** 的第二道，「节点存在性」够用。
**待拍板。**

### 3.2 ⚠️ 超时预算自相矛盾（必须改）

设计 §8-C1 原文：

> **两端都要设**：Java 代理 Python **8s**、Python 调 Java 8s、**整轮 60s**

**这三条合起来是矛盾的**：Java 代理 Python 只给 8s，但 Python 一轮要调**两次 LLM**。
实测（2026-10-01，hmall）一轮 = Intent 620ms + 工具 120ms + 讲解 1815ms ≈ **2.5s**，8s 够；
但 LLM 一旦慢到 10s，**Java 网关会在 Python 还没答完时断开**，用户看到网关超时而不是一个慢答案。

**建议改成**：Java 代理 Python **45–60s**（对齐整轮预算）；Python 调 Java 工具保持 **8s**；Python 单次 LLM 保持 **25s**。
这样 8s 只用来保护「工具不该慢」，不误伤「LLM 本来就慢」。

### 3.3 NAVIGATE 能不能做到零 LLM

设计 §5：「用户点『展开』『退回去』时只走 ①②④，**不调工具、不调 LLM**」。
但 P3 实测 NAVIGATE 仍调了 1 次 LLM（575ms）—— **因为要先判出这是 NAVIGATE，就得调 LLM #1**。

要做到零 LLM，只能让前端在点按钮时**直接声明意图**（如 `POST /api/agent/navigate {op: "BACK"}`），跳过判定。
**现在拍板，P5 才不用改接口。**

---

## 4. 任务分解

| 步 | 做什么 | 验收 |
| --- | --- | --- |
| **P4.0** | 拍板 §3 三个问题；`deploy/.env` 补 `INTERNAL_TOKEN`；确认 Redis db1 可用 | 三个决策落地成文字 |
| **P4.1** | Python：`store/context_store.py` + `Viewport` 模型；内存 dict → Redis | **重启 Python 后会话还在** |
| **P4.2** | Python：viewport / ack 两个内部路由 + **rev 乐观锁** | **rev 乱序上报被拒** |
| **P4.3** | Java：`AgentController` + 转发（JWT + 租户校验 + 内部 token + 超时） | curl 打 Java 接口能通到 Python |
| **P4.4** | Java：uiCommands 二次校验（按 §3.1 的决定） | 构造不存在的 uid → 被丢弃 + 记 warn |
| **P4.5** | 收口：`evict` 改成按 key 前缀清 Redis；全链路 curl 冒烟 | §0 的两条验收全过 |

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
