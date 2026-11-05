# R6 收尾：SSE 调查轨迹 + 评测口径 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `/ask` 加一条步骤级 SSE 通道，让用户在答案返回前看到 Agent 正在查什么；同时把 R6 的验收口径补成可重复运行的 20 题评测（含漂移与断链补查）。

**Architecture:** 新增 `/ask/stream` 旁路，`/ask` 一行不改（评测脚本与回退继续用它）。Python 循环在执行每个 Step 时同步调用一个 `on_step` 钩子，路由把钩子塞进 `asyncio.Queue`，由 `StreamingResponse` 逐帧写出。Java 用 `SseEmitter` 承接，再以 JDK `HttpClient` 把 Python 的字节流按行解码后转发；`answer` 事件在网关层反序列化后仍走原有的 `filterUiCommands` 弱校验。前端把消息改成「先建占位、边收边填、最后收口」。

**Tech Stack:** Python 3.12 / FastAPI / httpx；Java 21 / Spring Boot 4.1 WebMVC（**不用 WebFlux**）/ JDK HttpClient；React 18 / Vite / node:test。

**Spec:** 本文件 §设计决策（承接 brainstorming 对话结论）+ `记录每日工作/复杂到可探索/开发路线图.md` §R6。

## Global Constraints

- 仓库布局：后端与 Python 在 `D:\ArchMind\ArchMind`（Java 模块是其中的 `ArchMind/` 子目录）；前端是**独立仓库** `D:\ArchMind\ArchMind-react`。计划中的路径若以 `src/` 开头且标注（前端），指前端仓库。
- 不引入新依赖：Python 不加 `sse-starlette`，Java 不加 WebFlux，前端不加 EventSource polyfill。
- 所有对外 JSON 仍是 camelCase（Python 侧 `CamelModel` + `by_alias=True`），SSE 的 `data` 复用同一套模型序列化。
- `POST /api/agent/ask`、`POST /internal/agent/ask` 的行为、响应字段、状态码**一行不改**。
- 事件 `data` 必须是**单行 JSON**（`json.dumps` 会把换行转义，禁止手工拼多行字符串）。
- 租户校验（`checkProjectUtil.checkProject`）必须在**任何上游调用之前**完成；流开始后不再有方式返回 `Result` 错误。
- 单轮预算不变：`MAX_LLM_CALLS=6` / `MAX_TOOL_CALLS=8` / `MAX_STEPS=12`。

## Review Focus

以下五类输入/条件最容易在上线后咬人，任务 2/4/6/7/12 的测试分别钉住它们：

1. **客户端中途断开**（用户关掉面板 / 切页）：上游循环必须被取消，且**本轮不落库**，否则既烧 token 又把半轮写进 Redis。
2. **中间层缓冲**：nginx / 反向代理默认会攒够才发，表现为「事件全在最后一起到」。Python 与 Java 都要带 `X-Accel-Buffering: no`；线上 nginx 已存在，若仍表现为一次性到达，再由你调整 `proxy_buffering off`（不在本计划执行范围内）。
3. **消费者出错不能打断回答**：前端解析异常、Java `SseEmitter.send` 抛 IO 异常时，Python 必须把这一轮跑完并落库，只是少了几个中间事件。
4. **事件乱序**：`answer` 不能先于它对应的 `step` 到达；队列单生产者单消费者，顺序由构造保证。
5. **流开始前的失败**：会话过期、租户拒绝、参数错会以 **JSON `Result`（HTTP 200 + code≠0）** 返回而不是 SSE，前端必须按 `Content-Type` 分支解析，不能把 JSON 当事件帧。

---

## 设计决策（需要你确认的以 ⚠️ 标出）

| # | 决策 | 理由 |
|---|---|---|
| D1 | SSE 只推**步骤事件 + 最终完整答案**，不做 token 级流式 | 满足「看到它在查什么」的真实诉求；改动面小一个数量级。**token 级流式记入开发路线图，作为后续优化项** |
| D2 | 新增 `/ask/stream`，**不动** `/ask` | 评测脚本、契约测试、回退路径全部零改动 |
| D3 | 事件四类：`start` / `step` / `answer` / `error` | 前端只需四个分支；`answer.data` 就是现有 `AgentResponse` |
| D4 | `StepType.LLM → PLAN`，删除 `INTENT` | 循环里每次模型调用都是规划；`INTENT` 已无生产者 |
| D5 | ⚠️ 20 题评测集落在 **ArchMind 项目**（`2115334472151040002`），由现有 40 题数据集派生 | 那 40 题的锚点已经人工校对过，hmall 只有 6 题且需要重新选题 |
| D6 | ⚠️ 漂移口径：同题 5 次，**前 3 次工具名序列的 Jaccard ≥ 0.6** | 比「序列完全相等」现实，比「集合相等」严格 |
| D7 | ⚠️ 工具调用验收线改为 **P50 ≤ 5 / P95 ≤ 8**，取代路线图的「2–4 次」 | 11/04 实测平均 5 次模型调用 / 6 次工具，原线直接不成立 |
| D8 | **部署不在本计划内**：线上是远程服务器，容器与 nginx 已就绪；流程是推 GitHub → 服务器拉取重新构建。本计划只做本地三端验证 | 部署动作由你执行，计划只负责代码与本地证据 |

**未明确、且会影响实现的**：D5–D8 四条。若你认可默认值，实现按表执行；不同意的话改的是验收线，不是代码结构。D8 已确认：部署不执行，只做本地验证。

**配套计划**：路线图 R6「工具契约」表里仍标 ❌/⚠️ 的四项（`search` / `inspect` 字段·注解·异常 / `impact` / `coverage`）不在本计划内，见 [R6 第二批：工具能力补齐](2026-11-05-r6-tool-batch.md)。两份计划互不依赖，可分别交付。

---

## 文件结构

**Python（`agent-service/`）**

| 文件 | 新建/修改 | 责任 |
|---|---|---|
| `app/models/response.py` | 改 | `StepType` 收敛为 `PLAN` / `TOOL` |
| `app/core/agent_loop.py` | 改 | 所有 `steps.append` 收口到 `_add_step`，加 `on_step` 钩子 |
| `app/core/turn_support.py` | 改 | `retrieve` / `recall_memories` 透传钩子 |
| `app/core/events.py` | 新建 | SSE 帧编码与事件名常量 |
| `app/api/agent.py` | 改 | 抽出共享的 ctx 解析；新增 `/ask/stream` |
| `tests/test_step_events.py` | 新建 | 钩子顺序、异常隔离 |
| `tests/test_ask_stream.py` | 新建 | 流的三条出口 |
| `tests/test_followup_chain.py` | 新建 | 接口 → 实现类的补查机制 |
| `scripts/eval/r6_golden.json` | 新建 | 20 题验收集 |
| `scripts/eval/run_ab_eval.py` | 改 | `--repeats`、漂移、工具序列 |
| `tests/test_r6_golden.py` | 新建 | 数据集 schema 校验 |

**Java（`ArchMind/`）**

| 文件 | 新建/修改 | 责任 |
|---|---|---|
| `config/AgentProperties.java` | 改 | `streamTimeoutMs` |
| `service/agent/SseBlockDecoder.java` | 新建 | 纯函数式 SSE 行解码 |
| `service/agent/AgentClient.java` | 改 | `streamAsk` |
| `service/agent/AgentGatewayService.java` | 改 | `askStream` + answer 事件弱校验 |
| `controller/AgentController.java` | 改 | `/ask/stream` |
| `service/agent/SseEvent.java` | 新建 | `record SseEvent(String name, String data)` |
| `src/test/.../SseBlockDecoderTest.java` | 新建 | 解码边界 |

**前端（`ArchMind-react/`）**

| 文件 | 新建/修改 | 责任 |
|---|---|---|
| `src/api/sse.js` | 新建 | 字节流 → 事件回调 |
| `src/api/request.js` | 改 | `streamRequest`（注入 token、401 续期一次） |
| `src/api/agent.js` | 改 | `askStream` |
| `src/state/conversationStore.js` | 改 | 占位消息的四个动作 |
| `src/hooks/useAgentChat.js` | 改 | 改走 `askStream` |
| `src/components/agent/AgentPanel.jsx` | 改 | 实时步骤列表 |
| `src/api/agentTypes.js` | 改 | `PLAN` 类型 |
| `src/api/sse.test.js` | 新建 | 分块解析 |

---

## Task 1: StepType 收敛为 PLAN / TOOL

**Files:**
- Modify: `agent-service/app/models/response.py:155-161`
- Modify: `agent-service/app/core/agent_loop.py:383`
- Test: `agent-service/tests/test_agent_loop.py`

**Interfaces:**
- Produces: `StepType.PLAN`（字符串 `"PLAN"`）、`StepType.TOOL`；`StepType.INTENT` / `StepType.LLM` 不再存在。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_agent_loop.py`）

```python
async def test_steps_use_plan_not_llm_or_intent():
    llm = FakeLLM([
        reply(tool_call("t1", "overview")),
        reply(tool_call("a1", "answer", ANSWER_ARGS)),
    ])
    resp = await run_agent_turn(ctx(), "订单流程", FakeJava(), llm)
    types = {s.type for s in resp.steps}
    assert StepType.PLAN in types
    assert StepType.TOOL in types
    assert not hasattr(StepType, "LLM")
    assert not hasattr(StepType, "INTENT")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_agent_loop.py::test_steps_use_plan_not_llm_or_intent -v`
Expected: FAIL —— `StepType` 没有 `PLAN`

- [ ] **Step 3: 改枚举与唯一生产点**

`StepType` 只留 `PLAN = "PLAN"`、`TOOL = "TOOL"`；`agent_loop._ask` 里 `StepType.LLM` 改为 `StepType.PLAN`。检查 `app/` 与 `tests/` 里不再有 `StepType.LLM` / `StepType.INTENT`。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_agent_loop.py -v`
Expected: PASS（含既有 30 条）

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/models/response.py agent-service/app/core/agent_loop.py agent-service/tests/test_agent_loop.py
git commit -m "refactor: 步骤类型收敛为 PLAN/TOOL"
```

---

## Task 2: `on_step` 钩子贯通循环与检索/记忆

**Files:**
- Modify: `agent-service/app/core/agent_loop.py`（8 处 `steps.append(Step(...))`）
- Modify: `agent-service/app/core/turn_support.py:31-82`
- Test: `agent-service/tests/test_step_events.py`

**Interfaces:**
- Produces:
  - `StepHook = Callable[[Step], None]`
  - `run_agent_turn(ctx, message, java, llm, retriever=None, memory=None, *, on_step: StepHook | None = None) -> AgentResponse`
  - `_add_step(steps: list[Step], on_step: StepHook | None, type_: StepType, detail: str, ms: int) -> Step`
  - `retrieve(ctx, message, retriever, steps, on_step=None)`、`recall_memories(ctx, message, memory, steps, reason, on_step=None)` 末尾各多一个可选参数。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_step_events.py 自带最小构造，别 import 另一个测试文件
from fakes import FakeJava, FakeLLM
from test_agent_loop import ANSWER_ARGS, ctx, reply, tool_call

async def test_on_step_streams_every_step_in_order():
    llm = FakeLLM([
        reply(tool_call("t1", "overview")),
        reply(tool_call("a1", "answer", ANSWER_ARGS)),
    ])
    seen = []
    resp = await run_agent_turn(ctx(), "订单流程", FakeJava(), llm, on_step=seen.append)
    assert [(s.n, s.type) for s in seen] == [(s.n, s.type) for s in resp.steps]
    assert [s.n for s in seen] == sorted(s.n for s in seen)

async def test_on_step_exception_does_not_break_turn():
    def boom(step):
        raise RuntimeError("consumer down")
    llm = FakeLLM([reply(tool_call("a1", "answer", ANSWER_ARGS))])
    resp = await run_agent_turn(ctx(), "订单流程", FakeJava(), llm, on_step=boom)
    assert resp.message
```

> 若 `from test_agent_loop import ...` 在 pytest 下不可用，就把那四个名字复制进本文件 —— 辅助函数重复三行，好过给测试目录加 `__init__.py`。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_step_events.py -v`
Expected: FAIL —— `run_agent_turn` 不接受 `on_step`

- [ ] **Step 3: 实现 `_add_step` 并替换所有生产点**

`_add_step` 内部：构造 `Step(n=len(steps)+1, ...)` → `steps.append` → 若 `on_step` 非空则 `try: on_step(step) except Exception: logger.warning(...)`。替换范围：`_ask`、`_run_call`（4 处）、`_prefetch_locked_method`、`_run_navigate`、`turn_support.retrieve`、`turn_support.recall_memories`。串行传参：`run_agent_turn` → `retrieve` / `recall_memories` / `_prefetch_locked_method` / `_ask` / `_run_call`。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/ -q`
Expected: PASS（既有 223 passed 不回退）

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/core/agent_loop.py agent-service/app/core/turn_support.py agent-service/tests/test_step_events.py
git commit -m "feat: 循环暴露步骤回调"
```

---

## Task 3: SSE 帧编码与共享 ctx 解析

**Files:**
- Create: `agent-service/app/core/events.py`
- Modify: `agent-service/app/api/agent.py:138-182`
- Test: `agent-service/tests/test_events.py`（新建）

**Interfaces:**
- Produces:

```python
START = "start"; STEP = "step"; ANSWER = "answer"; ERROR = "error"
def sse_frame(event: str, data: dict[str, Any]) -> str
# -> f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

async def _prepare_ask(req: AskRequest, store: ConversationStore) -> tuple[AgentContext | None, AgentResponse | None]
# (ctx, None) 正常； (None, resp) 表示会话过期，调用方直接返回 resp
```

- [ ] **Step 1: 写失败测试**

```python
def test_sse_frame_is_single_data_line():
    frame = sse_frame("step", {"detail": "a\nb", "n": 1})
    lines = frame.split("\n")
    assert lines[0] == "event: step"
    assert lines[1].startswith("data: ")
    assert frame.endswith("\n\n")
    assert json.loads(lines[1][6:]) == {"detail": "a\nb", "n": 1}
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_events.py -v`
Expected: FAIL —— `app.core.events` 不存在

- [ ] **Step 3: 实现 `events.py`，并把路由里的 ctx 解析抽成 `_prepare_ask`**

`_prepare_ask` 必须包含今天的全部行为：新 cid 创建、带 cid 丢失 → `_session_expired`、`_apply_focus`、`ctx.method_uid = req.method_uid`、`ctx.user_id` 回填。`/ask` 改为调用它，行为不变。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_app_smoke.py tests/test_events.py tests/test_persistence.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/core/events.py agent-service/app/api/agent.py agent-service/tests/test_events.py
git commit -m "refactor: 抽出 ask 上下文解析并定义 SSE 帧"
```

---

## Task 4: `/internal/agent/ask/stream`

**Files:**
- Modify: `agent-service/app/api/agent.py`（新增路由）
- Test: `agent-service/tests/test_ask_stream.py`（新建）

**Interfaces:**
- Produces: `POST /internal/agent/ask/stream`，`Content-Type: text/event-stream`，响应头带 `Cache-Control: no-cache`、`X-Accel-Buffering: no`。
- 事件序列保证：`start` → 若干 `step` → 恰好一个 `answer` 或 `error`。

- [ ] **Step 1: 写失败测试（三条出口）**

```python
def _payload(**kw):
    return {"projectId": 7, "userId": 1, "message": "订单流程", **kw}

def test_stream_emits_start_steps_then_answer(client):
    with client.stream("POST", "/internal/agent/ask/stream", json=_payload()) as r:
        events = list(parse_sse(r.iter_lines()))
    names = [e.name for e in events]
    assert names[0] == "start"
    assert names[-1] == "answer"
    assert "step" in names
    assert events[-1].data["conversationId"]

def test_stream_session_expired_is_an_answer_event(client_gone):
    ...  # 带 cid 但 store 里没有 → answer 事件里 sessionExpired=True，且没有 step

def test_stream_llm_failure_emits_error_event(client_llm_down):
    ...  # LLMNotConfiguredError 仍向上抛（500）；LLMError → error 事件
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_ask_stream.py -v`
Expected: FAIL —— 404

- [ ] **Step 3: 实现路由**

结构固定为：`ctx, expired = _prepare_ask(...)` → 过期则 `StreamingResponse` 只吐一帧 `answer` → 否则建 `asyncio.Queue()`，`on_step = lambda s: queue.put_nowait((STEP, s))`，把真正执行包进 `asyncio.create_task`：跑 `run_agent_turn(..., on_step=...)` → `store.commit_turn` → 塞 `ANSWER` + 即 `AgentResponse` 序列化 → `finally: queue.put_nowait(None)`；异常塞 `ERROR` 后同样结束。

生成器：先 `yield sse_frame(START, {"conversationId": conversation_id})`，然后循环 `await queue.get()` 直到 `None`。**生成器的 `finally` 必须 `task.cancel()`** —— 这就是 Review Focus 第 1 条（客户端断开不落库、不继续烧 token）。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_ask_stream.py tests/test_agent_loop.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/app/api/agent.py agent-service/tests/test_ask_stream.py
git commit -m "feat: 新增步骤级 SSE 问答接口"
```

---

## Task 5: Java SSE 行解码器

**Files:**
- Create: `ArchMind/src/main/java/com/example/archmind/service/agent/SseEvent.java`
- Create: `ArchMind/src/main/java/com/example/archmind/service/agent/SseBlockDecoder.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/agent/SseBlockDecoderTest.java`

**Interfaces:**
- Produces:

```java
public record SseEvent(String name, String data) {}

public final class SseBlockDecoder {
    /** 喂一行；遇到空行且已累积内容时返回完成的块，否则 Optional.empty()。 */
    public Optional<SseEvent> accept(String line);
}
```

- [ ] **Step 1: 写失败测试**

```java
@Test void parses_named_block() {
    var d = new SseBlockDecoder();
    assertTrue(d.accept("event: step").isEmpty());
    assertTrue(d.accept("data: {\"n\":1}").isEmpty());
    assertEquals(new SseEvent("step", "{\"n\":1}"), d.accept("").orElseThrow());
}
@Test void joins_multiline_data_and_ignores_comments() {
    d.accept(": keep-alive");
    d.accept("data: a");
    d.accept("data: b");
    assertEquals("a\nb", d.accept("").orElseThrow().data());
}
@Test void tolerates_crlf_and_defaults_to_message() {
    d.accept("data: x\r");
    assertEquals("message", d.accept("\r").orElseThrow().name());
}
```

- [ ] **Step 2: 运行确认失败**

Run（Windows bash，在 `ArchMind/` 下）: `./mvnw -q -Dtest=SseBlockDecoderTest test`
Expected: FAIL —— 类不存在

- [ ] **Step 3: 实现解码器**

累计 `name` / `StringBuilder data`；`event:` 赋值、`data:` 追加（多行用 `\n` 连接）、`:` 开头忽略、空行结块并重置。不处理 `id:` / `retry:`。

- [ ] **Step 4: 运行通过**

Run: `./mvnw -q -Dtest=SseBlockDecoderTest test`
Expected: BUILD SUCCESS

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/service/agent/SseEvent.java ArchMind/src/main/java/com/example/archmind/service/agent/SseBlockDecoder.java ArchMind/src/test/java/com/example/archmind/service/agent/SseBlockDecoderTest.java
git commit -m "feat: SSE 行解码器"
```

---

## Task 6: Java 流式客户端 / 网关 / 控制器

**Files:**
- Modify: `ArchMind/src/main/java/com/example/archmind/config/AgentProperties.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/agent/AgentClient.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/service/agent/AgentGatewayService.java`
- Modify: `ArchMind/src/main/java/com/example/archmind/controller/AgentController.java`
- Test: `ArchMind/src/test/java/com/example/archmind/service/agent/AgentGatewayServiceTest.java`（追加）

**Interfaces:**
- Produces:

```java
// AgentProperties
private int streamTimeoutMs = 120_000;

// AgentClient
public void streamAsk(AgentAskRequest req,
                      Consumer<SseEvent> onEvent,
                      Runnable onComplete,
                      Consumer<Throwable> onError);
// 阻塞直到上游结束；调用方负责把它放到独立线程池

// AgentGatewayService
public SseEmitter askStream(AgentAskRequest req);

// AgentController
@PostMapping(value = "/ask/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
public SseEmitter askStream(@RequestBody AgentAskRequest req)
```

- [ ] **Step 1: 写失败测试**

```java
@Test void answer_event_gets_ui_commands_filtered() {
    // stub AgentClient: 立刻回调 answer 事件（data 含一条指向不存在节点的 FOCUS_NODE）
    // stub CodeGraphQueryService.existingClassUids(...) -> Set.of()
    SseEmitter emitter = service.askStream(req);
    // 用 RecordingSseEmitter 子类截获 send 的 event
    assertEquals(...);  // 发出前已被剔除
}
@Test void tenant_failure_throws_before_stream_starts() { ... }
```

- [ ] **Step 2: 运行确认失败**

Run: `./mvnw -q -Dtest=AgentGatewayServiceTest test`
Expected: FAIL —— `askStream` 不存在

- [ ] **Step 3: 实现**

- `AgentClient.streamAsk`：把 `build()` 里构造 `HttpClient` 的那段抽成 `private static HttpClient newHttpClient(int connectTimeoutMs)`，`build()` 与流式路径共用；`streamAsk` 持有自己的 `HttpClient` 字段（构造时建一次）。用 `sendAsync(request, BodyHandlers.ofInputStream())`；请求带 `Accept: text/event-stream` + 现有内部 token 头；逐行 `BufferedReader.readLine()` 喂 `SseBlockDecoder`，完成块回调 `onEvent`。非 200 时把响应体塞进 `AgentTransportException` → `onError`。请求超时用 `props.getStreamTimeoutMs()`。
- 新增线程池 bean（`ThreadPoolTaskExecutor`，core=4、max=8、queue=32、线程名前缀 `agent-stream-`），放在 `config/` 下已有配置类旁边。
- `AgentGatewayService.askStream`：先 `checkProject` + `currentUserIdOrThrow()`（**在创建 emitter 之前**）；建 `SseEmitter(0L)`；`executor.execute(() -> agentClient.streamAsk(...))`。`onEvent` 里：`answer` → Jackson 反序列化成 `AgentResponse` → `filterUiCommands` → 回写 JSON；其余事件原样转发。`onComplete` → `emitter.complete()`；`onError` → 发一帧 `error` 再 `complete()`。`onTimeout` / `onCompletion` 里取消上游（用 `AtomicBoolean` 防重入）。
- `AgentController`：直接返回 gateway 的 emitter，**不包 `Result`** —— 这是全项目唯一的例外，注释写明原因。

- [ ] **Step 4: 运行通过**

Run: `./mvnw -q test`
Expected: BUILD SUCCESS（既有测试不回退）

- [ ] **Step 5: 提交**

```bash
git add ArchMind/src/main/java/com/example/archmind/config ArchMind/src/main/java/com/example/archmind/service/agent ArchMind/src/main/java/com/example/archmind/controller/AgentController.java ArchMind/src/test/java/com/example/archmind/service/agent/AgentGatewayServiceTest.java
git commit -m "feat: Java 网关流式透传步骤事件"
```

---

## Task 7: 前端流式通道

**Files:**
- Create: `ArchMind-react/src/api/sse.js`
- Modify: `ArchMind-react/src/api/request.js`
- Modify: `ArchMind-react/src/api/agent.js`
- Test: `ArchMind-react/src/api/sse.test.js`（新建）

**Interfaces:**
- Produces:

```js
// sse.js —— 纯函数，不碰 fetch
export async function readSseEvents(stream, onEvent)  // onEvent({ name, data })
export function parseEventData(name, dataString)      // JSON.parse，answer/error 用

// request.js
export async function streamRequest(path, { method = 'POST', body, headers = {}, signal })
// 注入 Authorization，200 且 content-type 含 text/event-stream 时返回 Response 本身；
// 401 时用现有 refreshTokens 续期一次后重放；其余情况解析 Result 并抛 ApiError

// agent.js
export async function askStream({ projectId, message, conversationId = null, focus = null,
                                  methodUid = null, signal, onStep })
// 返回 normalizeAgentResponse(answer)
```

- [ ] **Step 1: 写失败测试**

```js
test('readSseEvents: 跨 chunk 的半行也能拼回来', async () => {
  const chunks = ['event: step\nda', 'ta: {"n":1}\n\n', 'event: answer\ndata: {}\n\n']
  const enc = new TextEncoder()
  const stream = new ReadableStream({
    start(c) { chunks.forEach((x) => c.enqueue(enc.encode(x))); c.close() }
  })
  const got = []
  await readSseEvents(stream, (e) => got.push(e))
  assert.deepEqual(got.map((e) => e.name), ['step', 'answer'])
  assert.deepEqual(JSON.parse(got[0].data), { n: 1 })
})

test('readSseEvents: 忽略注释行与空帧', async () => { ... })
```

- [ ] **Step 2: 运行确认失败**

Run（前端仓库）: `node --test src/api/sse.test.js`
Expected: FAIL —— 模块不存在

- [ ] **Step 3: 实现三处**

`sse.js` 用 `TextDecoder` + 行缓冲（**按 `\n` 切，最后一段可能不完整，留到下次**），累计 `event:` 与 `data:`，空行回调。
`streamRequest` 复用 `request.js` 里的 `getTokens` / `refreshTokens` / `LONG_FIELD`；**必须按 `res.headers.get('content-type')` 分支**（Review Focus 第 5 条）：不是 `text/event-stream` 就 `parseJson` 后抛 `ApiError`。
`askStream` 复用 `bodyWithProjectId` 与 `normalizeAgentResponse`；`step` 事件的 `data` **就是一个 `Step` 对象**（不是数组），直接 `onStep(parseEventData("step", dataString))`。

- [ ] **Step 4: 运行通过**

Run: `node --test src/api/sse.test.js`
Expected: PASS

- [ ] **Step 5: 提交（前端仓库单独提交）**

```bash
git add src/api/sse.js src/api/request.js src/api/agent.js src/api/sse.test.js
git commit -m "feat: 前端 SSE 读取与流式问答"
```

---

## Task 8: 消息占位与增量渲染

**Files:**
- Modify: `ArchMind-react/src/state/conversationStore.js`
- Modify: `ArchMind-react/src/hooks/useAgentChat.js`
- Modify: `ArchMind-react/src/components/agent/AgentPanel.jsx:20-39,123`
- Modify: `ArchMind-react/src/api/agentTypes.js:24,191`
- Test: `ArchMind-react/src/state/conversationStore.test.js`（新建）

**Interfaces:**
- Produces:

```js
beginAgentMessage() -> string            // 插入 pending 占位，返回 message id
appendMessageStep(id, step) -> void      // 幂等：同 n+type 替换而不是重复追加
completeAgentMessage(id, response) -> void
failAgentMessage(id, text) -> void
```

- [ ] **Step 1: 写失败测试**

```js
test('begin → appendStep → complete：占位被就地填满', () => {
  const id = beginAgentMessage()
  appendMessageStep(id, { n: 1, type: 'PLAN', detail: '规划#1', ms: 120 })
  appendMessageStep(id, { n: 1, type: 'PLAN', detail: '规划#1', ms: 120 })
  let m = getConversationState().messages.at(-1)
  assert.equal(m.steps.length, 1)          // 幂等
  completeAgentMessage(id, { message: '答', conversationId: 'c_1', steps: [] })
  m = getConversationState().messages.at(-1)
  assert.equal(m.pending, false)
  assert.equal(m.text, '答')
})
test('failAgentMessage：占位变成错误消息，不插第二条', () => { ... })
```

- [ ] **Step 2: 运行确认失败**

Run: `node --test src/state/conversationStore.test.js`
Expected: FAIL —— 导出不存在

- [ ] **Step 3: 实现**

store：占位消息 `{ role:'agent', pending:true, steps:[], text:'', ... }`；`complete` 用 `response.steps` 覆盖（服务端是权威），但保留已收到的更早的 steps 以防服务端截断；`conversationId` 照旧写入。
`useAgentChat`：`appendUserMessage` 后 `beginAgentMessage`；`askStream({ onStep: (s) => appendMessageStep(id, s) })`；`answer` 回来后 `completeAgentMessage`。超时改成**空闲超时**：每收到一个事件就 `clearTimeout` + 重设 95s（Review Focus 第 4 条的对照）。
`AgentPanel`：`StepList` 加 `live` 参数，`live` 时 `<details open>`；`pending && steps.length===0` 时显示「调查中…」。`agentTypes.js` 的 `StepType` 加 `PLAN`，保留 `INTENT` 仅用于旧数据渲染。

- [ ] **Step 4: 运行通过**

Run: `node --test src/state/conversationStore.test.js && npx vite build`
Expected: PASS + build 成功

- [ ] **Step 5: 提交**

```bash
git add src/state src/hooks/useAgentChat.js src/components/agent/AgentPanel.jsx src/api/agentTypes.js
git commit -m "feat: 答案到达前实时渲染调查步骤"
```

---

## Task 9: 断链补查的机制测试

**Files:**
- Test: `agent-service/tests/test_followup_chain.py`（新建）

**Interfaces:**
- 消费：Task 1/2 之后的 `run_agent_turn`；`tests/fakes.py` 的假 JavaClient / 脚本化 LLM。

- [ ] **Step 1: 写失败测试**

```python
async def test_interface_gap_leads_to_impl_lookup():
    # 脚本化模型：1) call-chain(接口 uid) → 2) method-body(Impl uid) → 3) answer
    # 假 JavaClient：第一次返回带 gap 的信封，且 data 里含 Impl uid；
    #                 第二次 method-body 返回源码，evidence 含 Impl uid
    resp = await run_agent_turn(ctx, "TradeClient 支付流程", java, llm)
    names = [r.name for r in ctx.turn.tool_results]
    assert names == ["call-chain", "method-body"]
    assert "com.example...TradeClientImpl" in {c.uid for c in resp.citations}
    assert "断点" in resp.message or any("断点" in s.body for s in resp.sections)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_followup_chain.py -v`
Expected: FAIL —— fixture 或断言不成立

- [ ] **Step 3: 补 fixture，不改生产代码**

若失败原因是「第二步的 Impl uid 不在 known 里」，说明 `_collect_uids` 没覆盖该信封形状 —— 这时修的是 `agent_loop._UID_FIELDS`/采集逻辑，并在测试里写明。若只是假数据不全，补 `tests/fakes.py`。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_followup_chain.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/tests/test_followup_chain.py agent-service/tests/fakes.py agent-service/app/core/agent_loop.py
git commit -m "test: 钉住接口断链后的二次补查"
```

---

## Task 10: 20 题评测集

**Files:**
- Create: `agent-service/scripts/eval/r6_golden.json`
- Test: `agent-service/tests/test_r6_golden.py`（新建）

**Interfaces:**
- Produces:

```json
{
  "version": 1,
  "name": "r6-archmind-20",
  "projectId": 2115334472151040002,
  "questions": [
    {"id": "Q01", "question": "...", "expectAnchorUids": ["..."],
     "multiStep": true, "mustDiscloseGap": false}
  ]
}
```

- [ ] **Step 1: 写失败测试**

```python
def test_dataset_shape(r6):
    assert len(r6["questions"]) == 20
    assert len({q["id"] for q in r6["questions"]}) == 20
    assert sum(q["multiStep"] for q in r6["questions"]) >= 8
    assert all(q["expectAnchorUids"] for q in r6["questions"])
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_r6_golden.py -v`
Expected: FAIL —— 文件不存在

- [ ] **Step 3: 生成数据集**

从 `scripts/eval/retrieval_dataset.archmind.json` 取 **Q01–Q20**，逐条搬运 `id` / `question` / `anchors` → `expectAnchorUids`。派生两个字段，规则写死、不靠判断：

- `multiStep` = 题干含「流程 / 怎么 / 如何 / 谁调用 / 之后」之一 → `true`；
- `mustDiscloseGap` = `expectAnchorUids` 中任一类的包名含 `.config.` 或 `.handler.` → `true`。

若派生出的 `multiStep` 少于 8 条，从 Q21–Q40 里按同一规则补足到 20 条。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_r6_golden.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/scripts/eval/r6_golden.json agent-service/tests/test_r6_golden.py
git commit -m "eval: R6 二十题验收集"
```

---

## Task 11: 评测脚本加重复与漂移

**Files:**
- Modify: `agent-service/scripts/eval/run_ab_eval.py`
- Test: `agent-service/tests/test_ab_eval.py`

**Interfaces:**
- Produces：
  - `--repeats N`（默认 1）
  - 每题输出 `runs: [{toolSequence: [str], toolCalls, tokensIn, tokensOut, latencyMs, citationViolations, gapDisclosed, degraded}]`
  - 汇总输出 `drift: {meanJaccard, perQuestion: {id: float}}`
  - 纯函数 `def jaccard(a: list[str], b: list[str]) -> float`

- [ ] **Step 1: 写失败测试**

```python
def test_jaccard_of_first_three_tools():
    assert jaccard(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    assert jaccard(["a", "b", "c"], ["a", "x", "y"]) == 0.25   # {a}=1, {a,b,c,x,y}=5
    assert jaccard([], []) == 1.0

def test_summarize_reports_drift():
    rows = [{"runs": [{"toolSequence": ["a", "b"]}, {"toolSequence": ["a", "b"]}]}]
    assert summarize(rows, "tools")["drift"]["meanJaccard"] == 1.0
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_ab_eval.py -v`
Expected: FAIL —— `jaccard` 不存在

- [ ] **Step 3: 实现**

`_metrics` 增记 `toolSequence`（从 `ctx.turn.tool_results` 取 `name`，按发生顺序）；`run_mode` 按 `--repeats` 循环并收集 `runs`；`summarize` 计算每题的成对 Jaccard（取前 3 个工具名）并求均值。缺工具时按空列表处理，**不要**抛异常。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_ab_eval.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agent-service/scripts/eval/run_ab_eval.py agent-service/tests/test_ab_eval.py
git commit -m "eval: 重复运行与工具选择漂移"
```

---

## Task 12: 端到端验收与文档回填

**Files:**
- Modify: `记录每日工作/复杂到可探索/开发路线图.md`（R6 小节）

> 部署不在本任务内：线上是远程服务器，容器与 nginx 已就绪。本地验证通过后推 GitHub，由你在服务器拉取重建。本任务只产出本地证据与文档回填。

- [ ] **Step 1: 全量单测**

Run: `cd agent-service && python -m pytest -q`
Expected: 全绿；契约测试按既有约定用 `TEST_PROJECT_ID` 指向有图项目

- [ ] **Step 2: Java 与前端构建**

Run: `cd ArchMind && ./mvnw -q test`
Run: `cd D:/ArchMind/ArchMind-react && node --test src/api/sse.test.js src/state/conversationStore.test.js && npx vite build`
Expected: 全部成功

- [ ] **Step 3: 起三端，量首事件**

启动 Java + agent-service + `npm run dev`；浏览器提问 hmall「下单扣库存」。在 Network 面板确认：
- 响应 `Content-Type: text/event-stream`；
- 第一个 `step` 事件在 **2s 内**到达；
- `answer` 事件里带 `sections` / `citations` / `graph`，与 `/ask` 同题结果结构一致。

- [ ] **Step 4: 断开验证**

提问后 3s 内关掉面板：Python 日志应出现任务取消，`agent_turn` 表不新增行，Redis 里 `focus_stack` 不变化。

- [ ] **Step 5: 跑 20 题 × 5 次**

Run: `python scripts/eval/run_ab_eval.py --project-id 2115334472151040002 --dataset scripts/eval/r6_golden.json --repeats 5 --out scripts/eval/out/r6-acceptance.json`
记录：完成率、工具调用 P50/P95、漂移均值、citation 越界数、P50/P95 延迟、token 合计。

- [ ] **Step 6: 回填路线图并提交**

把 D5–D7 的最终口径与实测数字写进 `开发路线图.md` 的 R6 小节，勾掉 SSE 两项，并在 SSE 小节记一条「token 级流式：后续优化」。

```bash
git add 记录每日工作/复杂到可探索/开发路线图.md
git commit -m "docs: R6 验收数字与 SSE 落地"
```

---

## 验收口径（与路线图 R6 对照）

| 项 | 验收线 | 由谁证明 |
|---|---|---|
| 一轮 2–4 次工具调用 | ⚠️ 改为 **P50 ≤ 5 / P95 ≤ 8**，且 ≥8 道题出现 ≥2 次调用 | Task 11 输出 |
| 断链能自动补查 | 机制有单测；评测里 `mustDiscloseGap` 题全部说到断点 | Task 9 + Task 11 |
| 同一问题 5 次不漂移 | 前 3 个工具名的**平均 Jaccard ≥ 0.6** | Task 11 输出 |
| 预算耗尽仍回答 | 既有测试保持绿 | Task 2 全量回归 |
| SSE 首 token < 2s | 改为**首事件 < 2s**（步骤事件，不是答案 token） | Task 12 Step 3 |
| 20 题对比 | 六选一已删，改为**绝对阈值**，不再做 A/B | Task 10/11 |

## 本计划之外（明确不做）

- **字段 / 接口类型的引用仍会被丢**（`CartMapper`、`LoginInterceptor` 只出现在字段声明和 `implements` 子句）：路线图已定为「保留记录，日后再处理」，属 Java 分析侧改造，不在 R6。
- **让模型自己早点收尾**：只有在 20 题数字跑出来、确认 P50 确实压不到 5 次以内时才立项；本计划只改验收线，不改循环行为。
- **token 级流式**（答案逐字出现）：D1 已排除。若之后要做，它是 `/ask/stream` 之上新增 `delta` 事件，不需要重做本计划的结构。
