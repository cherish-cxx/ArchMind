# R7 MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 ArchMind 已构建的代码认知以 MCP 工具交付给外部 agent：用户级 PAT 鉴权、5 个只读工具、项目说明导出。

**Architecture:** Java 负责 PAT、项目归属校验与数据接口；Python 新增独立 MCP 进程（agent-service 镜像，:8001）承载 Streamable HTTP 与语义检索；前端提供 token 管理与说明导出。所有工具只读，`projectId` 每次由 token 的 userId 校验。

**Tech Stack:** Java 21 / Spring Boot 4.1 / MyBatis-Plus、Python 3.12 / FastAPI / MCP Python SDK (FastMCP) / Redis、React 18 / Vite。

**Spec:** `docs/superpowers/specs/2026-11-16-r7-mcp-server-design.md`

> **实施状态（2026-11-17）：** Task 1–11 已完成，每个任务经独立子代理审查；Java R7 测试、Python 全量 315 passed / 16 skipped、前端构建、`docker compose config`、`bash -n`、`py_compile` 均通过。真实栈 E2E 与 Claude Code 手工演示未执行（本机未起服务）。上线前需在服务器 `deploy/.env` 设置 `MCP_PUBLIC_URL`，并确认 MySQL 用户有建表权限。

## Global Constraints

- token 明文只在创建响应出现一次；库中只存 SHA-256；日志与响应不得出现 token 或 `Authorization`。
- `projectId` 永远视为不可信输入，每次访问都校验 `project.userId == token.userId`；不通过返回 403。
- 工具全部只读；不暴露任意 Cypher；不改 `/internal/tools/*` 契约。
- MCP 工具名统一 `archmind_` 前缀；预期失败返回 `{"ok": false, "error": {...}}`，不抛协议错误。
- 每个数据工具响应都带项目级 `meta.graphRev`；`list_projects` 的每个条目带自己的 `graphRev` 与 `index`；索引相关工具带 `meta.index`。
- Java 测试用 `./mvnw test`；Python 用 `pytest`；前端用 `npm run build`。
- 契约测试需要 `TEST_PROJECT_ID` 指向有图项目；没有环境时 skip，不 fail。

## Review Focus

1. **token 泄露**：日志、错误响应、brief 中出现 token 或 userId —— 期望所有输出只含前缀或 projectId。
2. **跨租户访问**：A 的 token 带 B 的 projectId —— 期望 403，且不泄露 B 的项目名。
3. **索引缺失/失败**：`search_entities` 不得把空结果伪装成成功 —— 期望 `not_indexed` + reason + hint。
4. **截断**：大图 `trace_flow` 超过 `maxNodes` —— 期望 `truncated=true`，节点数不超过预算。
5. **快照过期**：图落后于工作区 —— 期望每个响应都有 `graphRev`，brief 有生成/分析时间。

---

### Task 1: PAT 数据模型与服务

**Files:**
- Create: `ArchMind/sql/2026-11-16-mcp_pat.sql`
- Create: `ArchMind/src/main/java/com/example/archmind/entity/McpPat.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dao/McpPatMapper.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/McpPatService.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/impl/McpPatServiceImpl.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/request/McpPatCreateRequest.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/McpPatCreatedResponse.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/McpPatView.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/McpProjectItem.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/McpPrincipal.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/McpContext.java`
- Create: `ArchMind/src/main/java/com/example/archmind/common/exception/McpAuthException.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/McpPatServiceImplTest.java`

**Interfaces:**
- Produces:
  - `McpPatService.create(Long userId, McpPatCreateRequest req) -> McpPatCreatedResponse`
  - `McpPatService.list(Long userId) -> List<McpPatView>`
  - `McpPatService.revoke(Long userId, Long patId) -> void`
  - `McpPatService.authenticate(String rawToken) -> McpPrincipal`
  - `McpPatService.context(McpPrincipal principal, Long projectId) -> McpContext`
  - `McpPatService.projects(Long userId) -> List<McpProjectItem>`
  - `McpPrincipal { Long patId; Long userId; Long boundProjectId; LocalDateTime expiresAt; }`
  - `McpContext { Long userId; Long patId; Long projectId; String projectName; String analysisStatus; Long graphRev; }`
  - `McpProjectItem { Long projectId; String name; String analysisStatus; Long graphRev; }`
  - `McpAuthException(int status, String message)`
  - DTO 统一用 Lombok `@Data` + `@Builder`（与 `ProjectResponse` 一致）。

- [ ] **Step 1: 写迁移 SQL**

```sql
CREATE TABLE IF NOT EXISTS mcp_pat (
  id           BIGINT PRIMARY KEY,
  user_id      BIGINT NOT NULL,
  project_id   BIGINT NULL,
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

- [ ] **Step 2: 写失败的测试**

```java
// McpPatServiceImplTest
@Test
void createStoresOnlyHashAndReturnsPlaintextOnce() {
    when(mapper.insert(any())).thenReturn(1);
    McpPatCreateRequest req = new McpPatCreateRequest();
    req.setName("claude-code");

    McpPatCreatedResponse res = service.create(7L, req);

    assertTrue(res.getToken().startsWith("am_pat_"));
    ArgumentCaptor<McpPat> captor = ArgumentCaptor.forClass(McpPat.class);
    verify(mapper).insert(captor.capture());
    assertEquals(64, captor.getValue().getTokenHash().length());
    assertNotEquals(res.getToken(), captor.getValue().getTokenHash());
    assertEquals(res.getToken().substring(0, 15), captor.getValue().getPrefix());
}

@Test
void contextRejectsForeignProject() {
    McpPrincipal principal = new McpPrincipal(1L, 7L, null, null);
    Project p = new Project();
    p.setId(99L);
    p.setUserId(8L);
    when(projectMapper.selectById(99L)).thenReturn(p);

    McpAuthException ex = assertThrows(McpAuthException.class,
            () -> service.context(principal, 99L));
    assertEquals(403, ex.getStatus());
}
```

- [ ] **Step 3: 运行测试，确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=McpPatServiceImplTest test`
Expected: FAIL（类不存在）

- [ ] **Step 4: 实现实体、DTO、Mapper、Service**

要点：
- token：`"am_pat_" + Base64.getUrlEncoder().withoutPadding().encodeToString(32 个 SecureRandom 字节)`；哈希用 `MessageDigest.getInstance("SHA-256")` 转小写 hex；`prefix = token.substring(0, 15)`。
- `authenticate`：按 `token_hash` 查；`revokedAt != null` → 401；`expiresAt != null && isBefore(now)` → 401；更新 `lastUsedAt`；返回 `McpPrincipal`。
- `context`：项目不存在 → 404；`!project.userId.equals(principal.userId)` → 403；`boundProjectId != null && !boundProjectId.equals(projectId)` → 403；`graphRevService.currentRev(projectId)` 填 `graphRev`。
- `projects`：`LambdaQueryWrapper<Project>().eq(Project::getUserId, userId).orderByDesc(Project::getCreateTime)`，逐项填 `graphRevService.currentRev(id)`。
- `create` 的 `expiresInDays` 为空时默认 90，范围 1..365。

- [ ] **Step 5: 运行测试，确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=McpPatServiceImplTest test`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add ArchMind/sql/2026-11-16-mcp_pat.sql ArchMind/src/main/java/com/example/archmind/{entity/McpPat.java,dao/McpPatMapper.java,service/McpPatService.java,service/impl/McpPatServiceImpl.java,dto/request/McpPatCreateRequest.java,dto/response/McpPatCreatedResponse.java,dto/response/McpPatView.java,dto/response/McpProjectItem.java,common/exception/McpAuthException.java} ArchMind/src/test/java/com/example/archmind/service/impl/McpPatServiceImplTest.java
git commit -m "feat(r7): add mcp PAT model and service"
```

---

### Task 2: PAT 管理接口（JWT）

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/controller/McpPatController.java`
- Test: `ArchMind/src/test/java/com/example/archmind/controller/McpPatControllerTest.java`

**Interfaces:**
- Consumes: `McpPatService`（Task 1）
- Produces:
  - `POST /api/mcp/tokens` → `Result<McpPatCreatedResponse>`
  - `GET /api/mcp/tokens` → `Result<List<McpPatView>>`
  - `DELETE /api/mcp/tokens/{patId}` → `Result<Void>`

- [ ] **Step 1: 写失败的测试**

```java
@ExtendWith(MockitoExtension.class)
class McpPatControllerTest {
    @Mock McpPatService service;
    @Mock CheckProjectUtil checkProjectUtil;
    MockMvc mvc;

    @BeforeEach
    void setUp() {
        mvc = MockMvcBuilders.standaloneSetup(
                new McpPatController(service, checkProjectUtil)).build();
    }

    @Test
    void createUsesCurrentUser() throws Exception {
        when(checkProjectUtil.getCurrentUserId()).thenReturn(7L);
        when(service.create(eq(7L), any())).thenReturn(
                McpPatCreatedResponse.builder().id(1L).token("am_pat_x").build());

        mvc.perform(post("/api/mcp/tokens")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"name\":\"claude-code\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.token").value("am_pat_x"));
    }

    @Test
    void listNeverReturnsPlaintextToken() throws Exception {
        when(checkProjectUtil.getCurrentUserId()).thenReturn(7L);
        when(service.list(7L)).thenReturn(List.of(
                McpPatView.builder().id(1L).prefix("am_pat_abc").build()));

        mvc.perform(get("/api/mcp/tokens"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data[0].token").doesNotExist());
    }
}
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=McpPatControllerTest test`
Expected: FAIL（controller 不存在）

- [ ] **Step 3: 实现 controller**

`@RequestMapping("/api/mcp/tokens")`；`@Valid @RequestBody McpPatCreateRequest`；用户 id 从 `checkProjectUtil.getCurrentUserId()` 取；`revoke` 传当前用户与 patId。`/api/**` 已由 SecurityConfig 要求认证，无需改安全配置。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=McpPatControllerTest test`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/controller/McpPatController.java ArchMind/src/test/java/com/example/archmind/controller/McpPatControllerTest.java
git commit -m "feat(r7): add PAT management endpoints"
```

---

### Task 3: 内部 MCP 鉴权与项目列表接口

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/controller/InternalMcpController.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/request/McpContextRequest.java`
- Test: `ArchMind/src/test/java/com/example/archmind/controller/InternalMcpControllerTest.java`

**Interfaces:**
- Consumes: `McpPatService`（Task 1）
- Produces:
  - `POST /internal/mcp/authenticate` body `{"token":"..."}` → 200 `McpPrincipal`；401 用 HTTP 状态码
  - `POST /internal/mcp/context` body `{"token":"...","projectId":123}` → 200 `McpContext`；401/403/404 用 HTTP 状态码
  - `GET /internal/mcp/projects?userId=7` → `Result<List<McpProjectItem>>`

- [ ] **Step 1: 写失败的测试**

```java
@Test
void contextReturns401ForUnknownToken() throws Exception {
    when(service.authenticate("bad")).thenThrow(new McpAuthException(401, "无效 token"));

    mvc.perform(post("/internal/mcp/context")
                    .contentType(MediaType.APPLICATION_JSON)
                    .content("{\"token\":\"bad\",\"projectId\":1}"))
            .andExpect(status().isUnauthorized());
}

@Test
void contextReturns403ForForeignProject() throws Exception {
    McpPrincipal p = new McpPrincipal(1L, 7L, null, null);
    when(service.authenticate("good")).thenReturn(p);
    when(service.context(p, 99L)).thenThrow(new McpAuthException(403, "无权访问该项目"));

    mvc.perform(post("/internal/mcp/context")
                    .contentType(MediaType.APPLICATION_JSON)
                    .content("{\"token\":\"good\",\"projectId\":99}"))
            .andExpect(status().isForbidden());
}
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=InternalMcpControllerTest test`
Expected: FAIL

- [ ] **Step 3: 实现 controller**

`context`：`authenticate(req.token)` → `context(principal, req.projectId)`；捕获 `McpAuthException` 返回 `ResponseEntity.status(e.getStatus()).body(Result.fail(e.getStatus(), e.getMessage()))`。`projects`：`Result.success(service.projects(userId))`。`/internal/**` 由 `InternalTokenFilter` 保护，安全配置无需改动。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=InternalMcpControllerTest test`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/controller/InternalMcpController.java ArchMind/src/main/java/com/example/archmind/dto/request/McpContextRequest.java ArchMind/src/test/java/com/example/archmind/controller/InternalMcpControllerTest.java
git commit -m "feat(r7): add internal MCP context and project list endpoints"
```

---

### Task 4: 项目说明（brief）生成与导出

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/ProjectBriefService.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/impl/ProjectBriefServiceImpl.java`
- Create: `ArchMind/src/main/java/com/example/archmind/dto/response/BriefResponse.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/controller/ProjectController.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/controller/InternalMcpController.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/impl/ProjectBriefServiceImplTest.java`

**Interfaces:**
- Consumes: `ProjectMapper`、`ProjectOverviewService.tryGetExistingOverview(Long)`、`GraphRevService.currentRev(Long)`
- Produces:
  - `ProjectBriefService.render(Long projectId, String target) -> BriefResponse`
  - `BriefResponse { String filename; String content; long graphRev; }`
  - `GET /api/project/{projectId}/brief?target=claude|agents`（JWT）
  - `GET /internal/mcp/brief?projectId=&target=` → `ToolEnvelope<BriefResponse>`

- [ ] **Step 1: 写失败的测试**

```java
// 测试辅助：sampleOverview() 返回 ProjectOverviewResponse，
// 含 summary="电商交易系统"，modules=[{name:"订单", responsibility:"下单与库存扣减"}]，
// techStack=[{name:"Spring Boot"}]
@Test
void rendersProjectMapWithProjectIdAndNoUserIdentity() {
    Project p = new Project(); p.setId(99L); p.setName("hmall");
    when(projectMapper.selectById(99L)).thenReturn(p);
    when(overviewService.tryGetExistingOverview(99L)).thenReturn(sampleOverview());
    when(graphRevService.currentRev(99L)).thenReturn(7L);

    BriefResponse res = service.render(99L, "claude");

    assertEquals("CLAUDE.md", res.getFilename());
    assertTrue(res.getContent().contains("projectId: 99"));
    assertTrue(res.getContent().contains("## 技术栈"));
    assertTrue(res.getContent().contains("订单"));
    assertTrue(res.getContent().contains("graphRev=7"));
    assertTrue(res.getContent().contains("生成时间"));
    assertFalse(res.getContent().contains("userId"));
}

@Test
void rejectsUnknownTarget() {
    assertThrows(BusinessException.class, () -> service.render(99L, "readme"));
}
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd ArchMind && ./mvnw -q -Dtest=ProjectBriefServiceImplTest test`
Expected: FAIL

- [ ] **Step 3: 实现渲染与接口**

Markdown 必须包含：标题（`# AGENTS.md` / `# CLAUDE.md`）、草稿警示、生成时间、`projectId`、项目名、类型、摘要、技术栈、架构分层、模块表、`## 使用 ArchMind MCP`（端点、`Authorization: Bearer $ARCHMIND_PAT`、工具清单、本项目 projectId）。不写 userId、token。公共接口先 `checkProjectUtil.checkProject(projectId)`；内部接口返回 `ToolEnvelope.success(res).withGraphRev(res.getGraphRev())`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd ArchMind && ./mvnw -q -Dtest=ProjectBriefServiceImplTest test`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/service/ProjectBriefService.java ArchMind/src/main/java/com/example/archmind/service/impl/ProjectBriefServiceImpl.java ArchMind/src/main/java/com/example/archmind/dto/response/BriefResponse.java ArchMind/src/main/java/com/example/archmind/controller/ProjectController.java ArchMind/src/main/java/com/example/archmind/controller/InternalMcpController.java ArchMind/src/test/java/com/example/archmind/service/impl/ProjectBriefServiceImplTest.java
git commit -m "feat(r7): render and export AGENTS/CLAUDE project brief"
```

---

### Task 5: Python MCP 进程骨架与 PAT 中间件

**Files:**
- Modify: `agent-service/requirements.txt`（追加 `mcp>=1.9,<2`）
- Create: `agent-service/app/mcp/config.py`（独立 `McpSettings`，不改动已有未提交的 `app/config.py`）
- Create: `agent-service/app/mcp/__init__.py`
- Create: `agent-service/app/mcp/deps.py`
- Create: `agent-service/app/mcp/context_client.py`
- Create: `agent-service/app/mcp/rate_limit.py`
- Create: `agent-service/app/mcp/auth.py`
- Create: `agent-service/app/mcp/server.py`
- Create: `agent-service/app/mcp/asgi.py`
- Test: `agent-service/tests/test_mcp_auth.py`

**Interfaces:**
- Produces:
  - `McpServices { JavaClient java; Retriever retriever; IndexStore index_store; McpContextClient context; RateLimiter limiter; }`
  - `deps.configure(services=None) -> McpServices`、`deps.services() -> McpServices`、`deps.reset() -> None`
  - `McpContextClient.authenticate(token) -> Principal`（401 抛 `McpAuthError`；调用 `/internal/mcp/authenticate`）
  - `McpContextClient.context(principal, project_id) -> ProjectContext`（403/404 抛 `McpAuthError`；调用 `/internal/mcp/context`）
  - `McpContextClient.projects(user_id) -> list[dict]`
  - `RateLimiter.allow(pat_id) -> bool`
  - `PatAuthMiddleware(app, context, limiter)`（纯 ASGI）
  - `app.mcp.asgi:app`（Starlette：`/health` + `/mcp`）

- [ ] **Step 1: 写失败的测试**

```python
@pytest.mark.asyncio
async def test_missing_token_returns_401():
    app = build_test_app(FakeContext(), FakeLimiter())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/mcp", json={})
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_rate_limited_returns_429():
    app = build_test_app(FakeContext(), FakeLimiter(allow=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/mcp", headers={"Authorization": "Bearer good"}, json={})
    assert r.status_code == 429

@pytest.mark.asyncio
async def test_invalid_token_does_not_echo_token():
    app = build_test_app(FakeContext(), FakeLimiter())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/", headers={"Authorization": "Bearer secret-value"})
    assert r.status_code == 401
    assert "secret-value" not in r.text

@pytest.mark.asyncio
async def test_valid_token_sets_principal():
    seen = {}
    async def inner(scope, receive, send):
        seen["user"] = current_principal().user_id
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    app = PatAuthMiddleware(inner, FakeContext(), FakeLimiter())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200 and seen["user"] == 7
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd agent-service && pytest tests/test_mcp_auth.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现骨架与中间件**

要点：
- SDK 导入：`from mcp.server.fastmcp import FastMCP`；若安装版本没有 `streamable_http_app`，先升级到满足 `mcp>=1.9,<2` 的版本。
- `Principal` 携带 `raw_token`（仅内存，不打印）；contextvar `_principal` 提供 `current_principal()` 与 `principal_scope(p)`。
- 中间件只处理 `scope["type"] == "http"`；`lifespan` 直接透传。缺 token → 401 JSON；`McpAuthError` → 对应状态；限流 → 429 JSON。
- `McpContextClient` 按 HTTP 状态码映射：401 → `McpAuthError(401)`，403 → 403，404 → 404；不解析 body 判断。
- `deps.configure()` 幂等；`Retriever`、`IndexStore` 共用同一个 `IndexStore` 实例。
- `app/mcp/config.py` 定义 `McpSettings`（环境变量前缀 `MCP_`，含 `rate_limit_per_minute=60`、`context_cache_seconds=0`——默认关闭缓存以便吊销即时生效，可用 `MCP_CONTEXT_CACHE_SECONDS` 开启），不改动已有的 `app/config.py`。
- `FastMCP("archmind", transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))`：PAT 已强制鉴权，关闭 SDK 的 DNS-rebinding 防护，否则反向代理的 Host 会被挡成 421。
- `asgi.py`：保留 SDK 默认的 `/mcp` 路径；`Starlette(routes=[Route("/health", health), Mount("/", app=PatAuthMiddleware(inner, ...))])`，避免鉴权前的 307。外层 lifespan 需转发给 MCP app 的 lifespan，否则 session manager 不启动。
- 测试辅助：`build_test_app(context, limiter)` 返回 `PatAuthMiddleware` 包住一个最小 ASGI app；`FakeContext.authenticate` 对 `"good"` 返回 `Principal(pat_id=1, user_id=7, bound_project_id=None, expires_at=None, raw_token="good")`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd agent-service && pytest tests/test_mcp_auth.py -q`
Expected: PASS

- [ ] **Step 5: 验证导入**

Run: `cd agent-service && python -c "from app.mcp.asgi import app; print(type(app).__name__)"`
Expected: `Starlette`

- [ ] **Step 6: 提交**

```bash
git add agent-service/requirements.txt agent-service/app/mcp agent-service/tests/test_mcp_auth.py agent-service/tests/test_mcp_context_client.py agent-service/tests/test_mcp_rate_limit.py
git commit -m "feat(r7): add MCP process skeleton and PAT middleware"
```

---

### Task 6: `archmind_list_projects` 与 `archmind_overview`

**Files:**
- Create: `agent-service/app/mcp/tools.py`
- Create: `agent-service/app/mcp/format.py`
- Modify: `agent-service/app/mcp/server.py`
- Test: `agent-service/tests/test_mcp_tools_basic.py`

**Interfaces:**
- Consumes: `deps.services()`、`current_principal()`
- Produces:
  - `archmind_list_projects() -> dict`
  - `archmind_overview(projectId: int) -> dict`
  - `format.meta(ctx, index=None, truncated=False) -> dict`
  - `format.tool_error(code, message, hint="") -> dict`
  - `ensure_project(project_id) -> ProjectContext`

- [ ] **Step 1: 写失败的测试**

```python
@pytest.mark.asyncio
async def test_list_projects_includes_index_state():
    configure_with_fakes(context=FakeContext(projects=[{"projectId": 1, "name": "hmall"}],
                                             index_state={"status": "ready", "rev": 7}))
    out = await archmind_list_projects()
    assert out["ok"] is True
    assert out["data"][0]["index"]["status"] == "ready"

@pytest.mark.asyncio
async def test_overview_returns_meta_graph_rev():
    configure_with_fakes(java=FakeJava(), context=FakeContext())
    out = await archmind_overview(1)
    assert out["ok"] is True
    assert out["meta"]["graphRev"] == 7
    assert out["meta"]["projectId"] == 1
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd agent-service && pytest tests/test_mcp_tools_basic.py -q`
Expected: FAIL

- [ ] **Step 3: 实现工具**

`ensure_project`：`ctx = await services().context.context(current_principal(), project_id)`；`McpAuthError` 转 `tool_error("not_authorized"|"not_found", ...)`。`list_projects` 并发读取每个项目的 `index_store.get_state`，映射为 `{"status": state.status, "rev": state.activeRev, "coverage": f"{actual}/{expected}"}`。`overview` 调 `java.overview`，`env.ok` 为假时 `tool_failed`。在 `server.py` 用 `@mcp.tool(name="archmind_list_projects")` 等注册。
- 所有工具测试都在 `principal_scope(Principal(pat_id=1, user_id=7, bound_project_id=None, expires_at=None, raw_token="good"))` 内调用；`configure_with_fakes(...)` 用 `deps.configure(McpServices(...))` 并在测试结束时 `deps.reset()`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd agent-service && pytest tests/test_mcp_tools_basic.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/mcp/tools.py agent-service/app/mcp/format.py agent-service/app/mcp/server.py agent-service/tests/test_mcp_tools_basic.py
git commit -m "feat(r7): expose list projects and overview tools"
```

---

### Task 7: `archmind_search_entities`

**Files:**
- Modify: `agent-service/app/mcp/tools.py`
- Test: `agent-service/tests/test_mcp_search.py`

**Interfaces:**
- Consumes: `services().retriever.search(project_id, query, k)`、`services().index_store.get_state(project_id)`
- Produces: `archmind_search_entities(projectId: int, query: str, k: int = 10) -> dict`

- [ ] **Step 1: 写失败的测试**

```python
@pytest.mark.asyncio
async def test_returns_ranked_candidates():
    retriever, _, _ = fake_retriever(
        state={"status": "ready", "activeRev": 7},
        points=[point("com.hmall.controller.OrderController", score=0.81)],
    )
    configure_with_fakes(retriever=retriever, context=FakeContext())
    out = await archmind_search_entities(1, "下单扣库存", 10)
    assert out["ok"] is True
    assert out["data"][0]["uid"] == "com.hmall.controller.OrderController"
    assert out["data"][0]["score"] == 0.81

@pytest.mark.asyncio
async def test_absent_index_is_explicit_error():
    retriever, _, _ = fake_retriever(state=None)
    configure_with_fakes(retriever=retriever, context=FakeContext())
    out = await archmind_search_entities(1, "下单扣库存", 10)
    assert out["ok"] is False
    assert out["error"]["code"] == "not_indexed"
    assert "分析" in out["error"]["hint"]
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd agent-service && pytest tests/test_mcp_search.py -q`
Expected: FAIL

- [ ] **Step 3: 实现工具**

`ensure_project` 后调 `Retriever.search`；候选用 `model_dump(by_alias=True)`。若 `not retrieval.attempted and not retrieval.candidates`，返回 `not_indexed` 并附 `retrieval.reason` 与索引状态；`partial` 写进 `meta.index.coverage`。不要吞掉 `qdrant_failed` / `embed_failed`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd agent-service && pytest tests/test_mcp_search.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/mcp/tools.py agent-service/tests/test_mcp_search.py
git commit -m "feat(r7): add semantic entity search tool"
```

---

### Task 8: `archmind_trace_flow`

**Files:**
- Modify: `agent-service/app/mcp/format.py`
- Modify: `agent-service/app/mcp/tools.py`
- Test: `agent-service/tests/test_mcp_trace_flow.py`

**Interfaces:**
- Consumes: `java.call_chain(project_id, uid, direction=ChainDirection.DOWN, method_uid=None, max_depth, max_nodes)`
- Produces:
  - `format.infer_layer(uid: str) -> str`
  - `format.shape_flow(data: CallChain) -> dict`
  - `archmind_trace_flow(projectId: int, uid: str, maxDepth: int = 3, maxNodes: int = 60) -> dict`

- [ ] **Step 1: 写失败的测试**

```python
def test_groups_by_depth_and_marks_inferred_layers():
    data = sample_chain([
        node("com.hmall.controller.OrderController", depth=0),
        node("com.hmall.service.IOrderService", depth=1),
        node("com.hmall.service.impl.OrderServiceImpl", depth=2),
        node("com.hmall.mapper.OrderMapper", depth=3),
    ])
    out = shape_flow(data)
    assert [s["depth"] for s in out["stages"]] == [0, 1, 2, 3]
    assert out["stages"][0]["layer"] == "controller"
    assert out["stages"][2]["layer"] == "service"
    assert out["stages"][3]["layer"] == "repository"
    assert out["stages"][0]["nodes"][0]["inferred"] is True

def test_maps_gaps_to_breaks_with_hint():
    data = sample_chain([], gaps=[Gap(at="com.hmall.api.client.CartClient",
                                      reason=GapReason.REMOTE_SERVICE)])
    out = shape_flow(data)
    assert out["breaks"][0]["reason"] == "REMOTE_SERVICE"
    assert "跨服务" in out["breaks"][0]["hint"]

def test_truncated_flag_propagates():
    data = sample_chain([node("com.hmall.service.IOrderService", depth=1)], truncated=True)
    assert shape_flow(data)["truncated"] is True
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd agent-service && pytest tests/test_mcp_trace_flow.py -q`
Expected: FAIL

- [ ] **Step 3: 实现整形与工具**

测试辅助：`sample_chain(nodes, gaps=[])` 构造 `CallChain(start=ClassNode(...), nodes=..., edges=[], gaps=..., truncated=False)`；`node(uid, depth)` 由 uid 推导 `simple_name`/`package_name`。

层推断规则：包名含 `controller` 或类名以 `Controller` 结尾 → `controller`；包名含 `service` 或类名匹配 `I?[A-Z]\w*Service(Impl)?` → `service`；包名含 `mapper|repository|dao` 或类名以 `Mapper|Repository|Dao` 结尾 → `repository`；包名含 `client|feign|api` 或类名以 `Client` 结尾 → `client`；包名含 `config` → `config`；其余 `other`。每层节点按 `module or ""`、`uid` 排序；输出 `counts`、`breaks`（reason 加中文 hint：`REMOTE_SERVICE` → 「跨服务：对端实现不在本图」；`INTERFACE_NO_IMPLEMENTATION` → 「接口在本项目中找不到实现」）、`truncated`。`uid` 为空返回 `invalid_argument`，hint 指向 `archmind_search_entities`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd agent-service && pytest tests/test_mcp_trace_flow.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/mcp/format.py agent-service/app/mcp/tools.py agent-service/tests/test_mcp_trace_flow.py
git commit -m "feat(r7): add layered flow trace tool"
```

---

### Task 9: `archmind_project_brief` 工具

**Files:**
- Modify: `agent-service/app/models/tool.py`（新增 `BriefResponse`）
- Modify: `agent-service/app/tools/java_client.py`（新增 `project_brief`）
- Modify: `agent-service/app/mcp/context_client.py`（或 `tools.py` 直接走 JavaClient）
- Modify: `agent-service/app/mcp/tools.py`
- Test: `agent-service/tests/test_mcp_brief.py`

**Interfaces:**
- Produces:
  - `JavaClient.project_brief(project_id: int, target: str) -> ToolEnvelope[BriefResponse]`
  - `BriefResponse { filename: str; content: str; graph_rev: int }`
  - `archmind_project_brief(projectId: int, target: str = "claude") -> dict`

- [ ] **Step 1: 写失败的测试**

```python
@pytest.mark.asyncio
async def test_returns_filename_and_content():
    java = FakeJava(brief_envelope=ToolEnvelope(
        ok=True, data=BriefResponse(filename="CLAUDE.md", content="# CLAUDE.md\nprojectId: 1",
                                    graph_rev=7), evidence=[], truncated=False, graph_rev=7))
    configure_with_fakes(java=java, context=FakeContext())
    out = await archmind_project_brief(1, "claude")
    assert out["ok"] is True
    assert out["data"]["filename"] == "CLAUDE.md"
    assert out["meta"]["graphRev"] == 7
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd agent-service && pytest tests/test_mcp_brief.py -q`
Expected: FAIL

- [ ] **Step 3: 实现模型、客户端与工具**

`BriefResponse` 继承 `CamelModel`，字段 `filename/content/graph_rev` 输出为 `graphRev`。`JavaClient.project_brief` GET `/internal/mcp/brief?projectId=&target=`，复用 `ToolEnvelope` 解析。工具先 `ensure_project`，再调 Java；`ok=false` 时返回 `tool_failed`。target 只接受 `claude|agents`，否则 `invalid_argument`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd agent-service && pytest tests/test_mcp_brief.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/models/tool.py agent-service/app/tools/java_client.py agent-service/app/mcp/tools.py agent-service/tests/test_mcp_brief.py
git commit -m "feat(r7): expose project brief tool"
```

---

### Task 10: 前端 MCP 接入面板

> 前端在独立仓库 `../ArchMind-react`，不在本仓库提交。

**Files:**
- Create: `../ArchMind-react/src/api/mcp.js`
- Create: `../ArchMind-react/src/components/McpAccess.jsx`
- Modify: `../ArchMind-react/src/components/ProjectDetail.jsx`

**Interfaces:**
- Consumes: `request` from `../api/request.js`
- Produces:
  - `listMcpTokens()`、`createMcpToken(name, expiresInDays)`、`revokeMcpToken(id)`、`projectBrief(projectId, target)`
  - `<McpAccess project={project} />`

- [ ] **Step 1: 实现 API 模块**

```js
export const listMcpTokens = () => request('/api/mcp/tokens')
export const createMcpToken = (name, expiresInDays) =>
  request('/api/mcp/tokens', { method: 'POST', body: { name, expiresInDays } })
export const revokeMcpToken = (id) =>
  request(`/api/mcp/tokens/${id}`, { method: 'DELETE' })
export const projectBrief = (projectId, target) =>
  request(`/api/project/${projectId}/brief?target=${target}`)
```

- [ ] **Step 2: 实现组件**

包含：token 列表（名称、前缀、创建/最后使用/过期、吊销按钮）；创建表单（名称、90/180/365 天）；创建成功后弹窗只显示一次明文并提供复制；`.mcp.json` 片段（用 `${ARCHMIND_PAT}`）；「导出 AGENTS.md」「导出 CLAUDE.md」两个按钮，用 `Blob` 下载，并用项目 id 生成文件名。

- [ ] **Step 3: 接入 ProjectDetail**

在 `DomainPreview` 之后渲染 `<McpAccess project={project} />`；仅在 `project.analysisStatus === 'COMPLETED'` 时显示导出按钮，其余显示「完成分析后可导出」。

- [ ] **Step 4: 构建验证**

Run: `cd ../ArchMind-react && npm run build`
Expected: 构建成功，无未使用变量/导入错误。

- [ ] **Step 5: 提交（前端仓库）**

```bash
cd ../ArchMind-react
git add src/api/mcp.js src/components/McpAccess.jsx src/components/ProjectDetail.jsx
git commit -m "feat(r7): add MCP access and brief export panel"
```

---

### Task 11: 部署与端到端验收

**Files:**
- Modify: `deploy/docker-compose.yml`
- Modify: `../ArchMind-react/nginx.conf`
- Create: `.mcp.json.example`
- Create: `agent-service/scripts/e2e_mcp.py`
- Modify: `docs/` 下演示说明（可选）

**Interfaces:**
- Consumes: 全部前序任务
- Produces: 公网/本机可连的 `https://<host>/mcp`；`python scripts/e2e_mcp.py` 可重复验收。

- [ ] **Step 1: 增加 compose 服务**

```yaml
  mcp:
    build:
      context: ../agent-service
      network: host
    container_name: archmind-mcp
    environment:
      - JAVA_BASE_URL=http://backend:8080
      - INTERNAL_TOKEN=${INTERNAL_TOKEN}
      - REDIS_URL=redis://172.21.160.203:6379
      - REDIS_DB=1
      - REDIS_PASSWORD=${REDIS_PASSWORD:-}
      - EMBED_URL=${EMBED_URL:-http://172.21.160.203:8010}
      - QDRANT_URL=${QDRANT_URL:-http://172.21.160.203:6333}
    command: ["uvicorn", "app.mcp.asgi:app", "--host", "0.0.0.0", "--port", "8001"]
    expose: ["8001"]
    depends_on: [agent-service]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')"]
      interval: 10s
      timeout: 3s
      retries: 5
    restart: unless-stopped
```

- [ ] **Step 2: nginx 增加 `/mcp`**

```nginx
location /mcp {
    proxy_pass http://mcp:8001;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_buffering off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

- [ ] **Step 3: 写 `.mcp.json.example`**

```json
{
  "mcpServers": {
    "archmind": {
      "type": "http",
      "url": "https://your-host/mcp",
      "headers": { "Authorization": "Bearer ${ARCHMIND_PAT}" }
    }
  }
}
```

- [ ] **Step 4: 写 E2E 脚本**

`agent-service/scripts/e2e_mcp.py`：读 `ARCHMIND_MCP_URL`（含 `/mcp`，如 `http://127.0.0.1:8001/mcp`）、`ARCHMIND_PAT`、`ARCHMIND_PROJECT_ID`；用 `mcp.client.streamable_http.streamablehttp_client` + `ClientSession` 完成 initialize、`list_tools`、`call_tool("archmind_list_projects")`、`call_tool("archmind_search_entities", {"projectId": id, "query": "下单扣库存", "k": 10})`、`call_tool("archmind_trace_flow", {"projectId": id, "uid": "<检索第一项>", "maxDepth": 3})`；断言五个工具名都在、search 返回候选、trace 返回 stages；打印摘要，失败非零退出。

- [ ] **Step 5: 运行验收**

Run: `cd agent-service && python scripts/e2e_mcp.py`
Expected: 输出 `tools=5`、`search_candidates>0`、`stages>0`，退出码 0。

- [ ] **Step 6: 手工验收**

1. 网页创建 PAT，复制，配置 `.mcp.json`，重启 Claude Code。
2. 在 hmall 仓库问「下单扣库存怎么走」，确认出现 `archmind_search_entities` 与 `archmind_trace_flow`。
3. 导出 `AGENTS.md` 与 `CLAUDE.md`，粘贴后用新会话确认 Claude 读到 projectId。
4. 负向：吊销 token → 401；用他人项目 id → 403；未建索引项目 → `not_indexed`；连续调用超过 60 次/分 → 429。

- [ ] **Step 7: 提交**

```bash
git add deploy/docker-compose.yml .mcp.json.example agent-service/scripts/e2e_mcp.py
git commit -m "chore(r7): deploy MCP service and add end-to-end check"
```

---

## Self-Review

- Spec 的五个工具、PAT 模型、限流、brief 导出、回滚/失败语义均有对应任务。
- 类型一致性：Python `ProjectContext` 字段与 Java `McpContext` 的 camelCase 输出一一对应；`BriefResponse` 在 Java 与 Python 两端同名。
- Review Focus 的五条分别由 Task 1/3（越权）、Task 7（索引缺失）、Task 8（截断）、Task 6/4（graphRev）覆盖。
- 未实现项：`method_body`、ingest-on-demand、OAuth、团队共享，均写入 spec 的「后续」。
