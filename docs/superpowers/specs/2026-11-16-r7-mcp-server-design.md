# R7 MCP Server 设计

> 2026-11-16。本文是 R7 的设计说明与实施计划（`docs/superpowers/plans/2026-11-16-r7-mcp-server.md`）的依据。

## 1. 意图

把 ArchMind 已经建好的代码认知（概况、类图、调用链、语义索引）通过 MCP 交付给任意外部 agent。目标不是「再做一个读代码的接口」，而是**把一次性付出的分析成本，变成一个可被多个消费者复用的上下文层**。

验收场景：Claude Code 通过 MCP 连接 ArchMind，针对已在 ArchMind 分析过的 hmall，回答「下单扣库存怎么走」；回答里能看到 `archmind_search_entities` / `archmind_trace_flow` 的调用，跨服务处显式标出断点。

## 2. 目标与非目标

**目标**

- 暴露 5 个只读工具：`archmind_list_projects`、`archmind_overview`、`archmind_search_entities`、`archmind_trace_flow`、`archmind_project_brief`。
- 用户级 PAT 鉴权，每次调用对项目做归属校验。
- 项目说明（AGENTS.md / CLAUDE.md 草稿）可导出，内含 projectId，供用户加入本地仓库。
- 部署为独立 MCP 进程，Streamable HTTP，可被 Claude Code 通过 HTTPS 连接。
- 对已分析但索引未就绪、图被重建、token 过期/吊销等状态给出显式错误，不静默返回空。

**非目标**

- 不做「MCP 触发分析 / 上传」（R7.5，`ingest_project`）。
- 不暴露 `call_chain` / `class_relations` / `classes` / `method_body`；前两者是 `trace_flow` 的内部依赖，`method_body` 留给后续「无仓库消费者」场景。
- 不做 OAuth 2.1（演进项）。
- 不自动写用户仓库文件；brief 只导出，由用户合并。
- 不改 `/internal/tools/*` 的契约与现有 agent 链路。
- 不暴露任意 Cypher，不做写操作。

## 3. 关键决策

| 决策 | 选择 | 理由 | 替代方案 |
| --- | --- | --- | --- |
| 交付面 | 3 个数据工具 + 项目列表 + brief | 只保留用户可感知的三类价值：语义入口、跨模块链路+断点、全局背景；其余退成内部实现 | 7 个工具（工程完备但增加选错概率） |
| 传输 | Streamable HTTP，独立进程 :8001 | `search_entities` 需要 Qdrant/embed，必须在服务端；stdio 会把内部 token 带到用户机器 | stdio 本地（仅限自托管） |
| 身份 | 用户级 PAT，token 只绑定 userId | 新项目不需要重新配置；每次调用校验项目归属 | 项目域 PAT（安全更紧，但每项目一个 token） |
| 项目选择 | `list_projects` + brief 中的 projectId 提示 | 一个 token 通用；有仓库时 Claude 可直接从 CLAUDE.md/AGENTS.md 读到 projectId | 每次对话问用户 |
| 项目边界 | 只服务已在 ArchMind 分析的项目 | MCP 定位是「交付认知」不是「现场建认知」 | ingest-on-demand（R7.5） |
| brief | 服务端生成草稿，导出两份（AGENTS.md / CLAUDE.md） | 不覆盖用户手写内容；文件名按客户端选择 | 自动写入仓库（危险） |
| token 格式 | `am_pat_` + 32 字节随机，库中只存 SHA-256 | 泄库不等于泄 token；前缀便于扫描 | 存明文（禁止） |
| 限流 | MCP 层 Redis，60 次/分/token | 所有请求都经过 MCP 进程，位置统一 | Java 限流（search 不经过 Java，会漏） |

## 4. 架构与数据流

```
Claude Code ──HTTPS /mcp──▶ mcp 进程 (agent-service 镜像, :8001)
                              │  Bearer PAT
                              ├─▶ Java POST /internal/mcp/authenticate (内部 token)
                              │      PAT → userId / patId（不带项目）
                              ├─▶ Java POST /internal/mcp/context   (内部 token)
                              │      PAT + projectId → 项目/分析状态/graphRev
                              ├─▶ Java GET  /internal/mcp/projects  (内部 token)
                              ├─▶ Java /internal/tools/*            (内部 token)
                              ├─▶ Java GET  /internal/mcp/brief     (内部 token)
                              └─▶ Qdrant + embed（search_entities 直连）

Web ──JWT──▶ Java POST/GET/DELETE /api/mcp/tokens
Web ──JWT──▶ Java GET /api/project/{id}/brief?target=claude|agents
```

`projectId` 只作为资源标识出现在请求里；**每次**由 Java 用 token 的 userId 校验归属，校验不通过返回 403。

## 5. 工具契约

所有工具返回：

```json
{
  "ok": true,
  "data": "...",
  "meta": {
    "projectId": 2117859147238891521,
    "graphRev": 7,
    "index": {"status": "ready", "rev": 7, "coverage": "4120/4123"},
    "truncated": false
  }
}
```

预期失败不抛协议错误，返回：

```json
{"ok": false, "error": {"code": "not_indexed", "message": "...", "hint": "..."}}
```

错误码：`not_authorized`、`not_found`、`not_indexed`、`tool_failed`、`invalid_argument`。

| 工具 | 输入 | 输出 | 主要失败 |
| --- | --- | --- | --- |
| `archmind_list_projects` | 无 | 当前用户的 `projectId/name/analysisStatus/graphRev/index` | 401 |
| `archmind_overview` | `projectId` | 项目类型/摘要/技术栈/架构/模块 | 403/404；无概况时 `tool_failed` |
| `archmind_search_entities` | `projectId`, `query`, `k=10` | 排序候选：`uid/simpleName/packageName/kind/description/score/methods` | `not_indexed`（带 reason） |
| `archmind_trace_flow` | `projectId`, `uid`, `maxDepth=3`, `maxNodes=60` | `start/stages/breaks/counts/evidence`；层标签标 `inferred` | `invalid_argument`（无 uid）；`tool_failed` |
| `archmind_project_brief` | `projectId`, `target=claude|agents` | `filename/content` | `tool_failed`（无概况） |

`trace_flow` 的层标签由包名/类名推断（controller / service / repository / client / config / other），必须带 `inferred: true` 与 `layerBasis: "package-name"`。

## 6. 安全

- token 明文只在创建响应中出现一次；数据库只存 SHA-256；列表只返回前缀。
- 过期时间默认 90 天，可吊销；`last_used_at` 用于泄露定位。
- 每次项目访问都校验 `project.userId == token.userId`；token 可预留项目绑定（`project_id` 可空）。
- 限流 60 次/分/token；429 在 MCP 层返回。限流在 Redis 故障时 fail-open（记日志，不含 token）。
- 认证缓存默认关闭（`MCP_CONTEXT_CACHE_SECONDS=0`），保证吊销/过期立即生效；开启后接受该窗口内的吊销延迟。
- 反向代理下关闭 MCP SDK 的 DNS-rebinding 防护；鉴权由 PAT 中间件强制。
- 任何日志、错误响应、brief 内容都不包含 token、userId；brief 只含 projectId。
- 全部工具只读。

## 7. 数据模型

```sql
CREATE TABLE IF NOT EXISTS mcp_pat (
  id           BIGINT PRIMARY KEY,
  user_id      BIGINT NOT NULL,
  project_id   BIGINT NULL,              -- 预留：项目域 token
  name         VARCHAR(64) NOT NULL,
  token_hash   CHAR(64) NOT NULL,
  prefix       VARCHAR(16) NOT NULL,
  create_time  DATETIME NOT NULL,
  expires_at   DATETIME NULL,
  last_used_at DATETIME NULL,
  revoked_at   DATETIME NULL,
  UNIQUE KEY uk_mcp_pat_hash (token_hash),
  KEY idx_mcp_pat_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

## 8. 已知限制

- 图对应某次分析快照，可能落后于工作区；所有响应带 `graphRev`，brief 带生成时间与 `graphRev`（图版本即分析版本）。`method_body` 不在本版，Claude 读当前文件时以文件为准。
- `overview` 是分析时 LLM 生成，字段可能缺失或过期，只能作为背景，不作为具体类的事实证据。
- gap 目前只有 `REMOTE_SERVICE` 与 `INTERFACE_NO_IMPLEMENTATION`。
- `trace_flow` 的层标签是推断，不是图上的事实。
- token 绑定账号而不是项目，单个 token 泄露会影响该账号的全部项目；可以为不同客户端各建一个 token，并用过期、吊销、限流、审计收窄风险。

## 9. 验收

1. 网页创建 PAT → 复制 → 配置 `.mcp.json` → Claude Code 能看到 5 个 `archmind_*` 工具。
2. 在 hmall 仓库里问「下单扣库存怎么走」，出现 `archmind_search_entities` 与 `archmind_trace_flow` 调用；答案引用真实类，跨服务处显示断点。
3. 导出 `AGENTS.md` 与 `CLAUDE.md`，内容含 projectId、技术栈、模块；用户粘贴后重新开会话，Claude 能读取 projectId。
4. 负向：吊销 token → 401；用 A 的 token 查 B 的项目 → 403；索引未就绪 → `not_indexed`；超过 60 次/分 → 429。
5. 响应与日志中不出现 token 明文；brief 中不出现 userId/token。

## 10. 后续

- R7.5：`ingest_project(path)`（本地路径打包上传、异步分析、状态轮询）。
- OAuth 2.1 + 项目级 scope（托管客户端）。
- `method_body` 与空目录消费者演示。
- 团队共享：`project_member` 表，PAT 校验从属主改为成员。
