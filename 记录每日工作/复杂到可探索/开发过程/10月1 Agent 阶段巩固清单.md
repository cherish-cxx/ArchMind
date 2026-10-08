# 10月1 Agent 阶段巩固清单（面试版）

> 覆盖 P0 → P3（Agent 执行步骤口径）。每题给出 **答对的标准** + **代码落点**。
> 档位：**★必答**（讲不出来这段经历就白写）· **◆讲原理**（决定你是"用过"还是"懂"）· **○知边界**
> 配套：[10月1 契约与字段总表](10月1%20契约与字段总表.md) · [10月1 P4 网关与Context设计](10月1%20P4%20网关与Context设计.md)
> 代码路径相对仓库根：`ArchMind/`（Java）· `agent-service/`（Python）

---

# A. 架构与拆分（开场必问，决定整场节奏）

### Q1. 为什么要拆成 Java + Python 两个服务？ ★必答
**答**：**按数据所有权切，不按语言切**。Java 独占 MySQL/Neo4j/磁盘/JWT（有状态、要事务）；Python 独占 LLM 调用和会话 Context（无状态、要频繁换模型）。两边变化的原因完全不同——这才该拆。不是"因为 LLM 用 Python 写方便"。
**代码**：边界就是两个文件——`agent-service/app/tools/java_client.py`（Python **唯一**会讲 HTTP 的地方）+ `ArchMind/.../controller/InternalToolController.java`（Java **唯一**的内部入口）。

### Q2. Python 直接连 Neo4j 会怎样？ ★必答
**答**：① 绕过租户校验（能读别人项目）；② 造出第二份数据副本，图和实现分家；③ 分布式单体。
**代码（可直接演示的硬证据）**：`agent-service/requirements.txt` 里**根本没有** neo4j / pymysql 驱动——只有 `fastapi uvicorn pydantic pydantic-settings httpx pytest pytest-asyncio`。
> 这条"边界能被依赖清单证明"的细节，比口头说"我们做了隔离"有力得多。

### Q3. projectId 谁来保证正确？ ★必答
**答**：Java 注入；Python 不产生、LLM 不可见。**但要如实说**：P4 之前这条链是**不完整的**——`CheckProjectUtil.checkProject` 还没补，现在 projectId 是调用方给的。
**代码**：`models/protocol.py` AskRequest 的 `project_id` 注释；`InternalToolController.strArg` 只取字符串不校验归属；Java 侧 `common/security/InternalTokenFilter.java` 只验共享密钥、**不验用户身份**。

### Q4. 为什么 Python 不接 JWT？ ★必答
**答**：让 user token **在结构上不可能泄进 LLM prompt**。Python 拿不到身份，只有 projectId 这个数字。
**代码**：`app/api/agent.py::require_internal_token` —— 只读 `X-Internal-Token`，没有任何 JWT 解析代码。

### Q5. 如果 Python 被攻破，攻击者能拿到什么？ ◆讲原理
**答**：只有明文 projectId + 5 个只读工具接口。拿不到用户身份、JWT、数据库凭据。
**代码**：同 Q2/Q4 —— 依赖清单 + 只有一个请求头。

### Q6. 两个服务间怎么鉴权？为什么够用？ ◆讲原理
**答**：`X-Internal-Token` 共享密钥 + `compare_digest` 定长比较（防时序侧信道）。够用是因为攻击面靠**网络层**收窄。真微服务要 mTLS/服务网格——**说得出这个演进方向才算懂**。
**代码**：`app/api/agent.py:33` 用 `secrets.compare_digest`，注释明确写了"和 Java 侧 `InternalTokenFilter` 的做法对齐"；`config.py` 的 `INTERNAL_TOKEN_HEADER` 是**两边共用的常量名**，注释："写在一处，改的时候不会只改一半"。

---

# B. 图谱与 AST（技术深度区，最能拉开差距）

### Q7. 图里有什么？ ★必答
**答**：节点 `Class` / `Method`；边 `CALLS` / `DEPENDS` / `EXTENDS` / `IMPLEMENTS`。实测 hmall 178 文件 → 181 Class + 288 Method + 186 CALLS + 116 DEPENDS + 16 IMPLEMENTS + 7 EXTENDS。
**代码**：落图 `ArchMind/.../dao/neo4j/CodeGraphNeo4jWriter.java`（`UNWIND` 批写 + `MERGE` 幂等）；Python 侧枚举 `models/tool.py:24-48`。

### Q8. 为什么不解析依赖注入和多态？ ★必答
**答**：纪律是「**宁可漏不要错**」——外部依赖、歧义、复杂表达式一律丢弃。理由：**一条错的边比十条例缺失的边危害大得多**。
**代码**：路线图 P1 段的「AST 边界」引文；实现上 `RelationResolver` 按 receiver 形态分派，**形态不明确就丢弃不加边**。

### Q9. 那这个纪律的代价是什么？ ★必答（多数人会漏这一层）
**答**：`@Autowired` 注入接口 + 实现类的调用**可能是断的**。所以 Agent 讲流程时必须显式承认断链（`gaps`），不能编。
**代码**：`models/tool.py:GapReason`；`core/prompt.py` 硬规则 2 强制要求提及断点。

### Q10. 调用链为什么撞到 Service 接口就断？ ★必答
**答**：接口方法**没有方法体、没有出边**。AST 语法的必然，不是 bug。
**代码**：`CallChainServiceImpl.java:36` 注释原文：「边指向接口方法，而接口方法没有方法体。扇出到实现类把它从死路变成分支点。」

### Q11. 反向扇出是什么？为什么它把死路变分支点？ ◆讲原理
**答**：撞到接口 → 沿 `IMPLEMENTS` 反向找到实现类 → 在实现类里按**签名相同**的方法继续往下。
**代码**：`CallChainServiceImpl.java:254 reverseFanout()`，用两条 Cypher —— `BATCH_INTERFACES_OF`（我实现了哪些接口）+ `BATCH_IFACE_METHOD_BY_SIG`（接口上同签名的方法）。

### Q12. 扇出为什么不计 depth？ ◆讲原理
**答**：扇出是"换了个等价的节点"，不是"往下走了一层"。计 depth 的话 `depth≤2` 就永远穿不过接口。
**代码**：`CallChainServiceImpl.java:146-147` 注释：「扇出来的实现类方法**留在同一层**（不计 depth）」。

### Q13. gaps 有哪几种？跨服务怎么识别？ ◆讲原理
**答**：`REMOTE_SERVICE` / `INTERFACE_NO_IMPLEMENTATION` / `NO_DOWNSTREAM_EDGE` / `EXTERNAL_DEPENDENCY`（后两个是保留值，V1 不产出）。跨服务靠**启发式**：模块名以 `-api` 结尾、或包名含 `.client`（Feign 特征）。**主动说"这是启发式、不是精确判断"**。
**代码**：`models/tool.py:65-71`；`core/prompt.py::_GAP_TEXT` 把四种 reason 翻译成人话。

### Q14. 为什么 call-chain 用 Java 侧逐层 BFS，不用变长 Cypher？ ◆讲原理
**答**：扇出要**中途换关系类型**（CALLS → IMPLEMENTS → 再回 CALLS），变长 Cypher 表达不了。这是**打样推翻了原设计**的一次。
**代码**：`CallChainServiceImpl.java` 的 `down()` / `up()` 两个方法；文件顶部注释记了这次改动。

### Q15. AST 为什么分三阶段？ ◆讲原理
**答**：A 解析声明 + 悬空调用；B 建索引字典；C 名字消解 + 按 receiver 分派 `CALLS`。分的原因是**同名类**——不建索引就消解不了。
**代码**：`service/ast/` 下的 `AstParserServiceImpl`（A）→ `model/ast/ProjectIndex`（B）→ `TypeResolver` + `RelationResolver`（C）。

---

# C. 契约与跨语言边界（P2 的全部价值）

### Q16. 契约为什么必须照代码抄，不能照文档抄？ ★必答
**答**：设计文档只写了信封字段名，**五个工具的 data 载荷形状只存在于 Java DTO 里**。
**代码**：`models/tool.py` 顶部 docstring 原文：「字段名与 Java DTO 逐个对齐。改这里之前，先去读 Java 那边对应的类。注意：这不是「Python 风格改写」的地方 —— 名字对不上就会丢字段。」
**完整对照见** → [契约与字段总表](10月1%20契约与字段总表.md)

### Q17. 「参数静默降级」是什么？怎么发现的？ ★必答（你项目里最漂亮的一个发现）
**答**：Java 用 `strArg`/`intArg` 取值，**key 找不到就吃默认值，不抛异常、不记日志**。Python 顺手写 `max_depth` → **HTTP 200、参数被忽略、结果悄悄不对**。
怎么发现：写了一条测试，**让"传 1"和"传 2"产生不同结果**——否则测了等于没测。
**代码**：
- 根源 `InternalToolController.java:314` `intArg`：`raw instanceof Number n ? n.intValue() : fallback`
- 防线 `tests/test_contract.py::test_2` —— 注释里还记了选起点的坑：`IOrderService` 在 depth=1/2 下结果完全相同，用它**永远绿**

### Q18. 数字为什么必须是 JSON number 不能是字符串？ ◆讲原理
**答**：`intArg` 判的是 `raw instanceof Number`。发 `"maxDepth": "2"` 同样落回默认值。
**代码**：同上；`test_1` 专门断言 `isinstance(cc[k], int)`。

### Q19. `CamelModel` 做什么？`populate_by_name` 为什么需要？ ◆讲原理
**答**：`alias_generator=to_camel`：Python 写蛇形、JSON 走驼峰。`populate_by_name=True` 让**蛇形名也能直接构造**——测试和内部代码不用处处写 alias。
**代码**：`models/base.py` —— 单独成文件是因为三个契约模块都要用它（同一理由而变化的代码应该住在一起）。

### Q20. 契约测试为什么打真 Java，不 mock？ ★必答
**答**：mock 测的是「我以为 Java 会怎么反应」，而**那恰好是契约漂移要防的东西**。
**代码**：`tests/conftest.py` 顶部 docstring：「这组测试是集成测试：验的是「Java 实际怎么反应」，所以必须打真的 Java 后端。」

### Q21. Java 不可达时为什么 skip 而不是 fail？ ★必答
**答**：**红只应该代表一件事：契约漂移了。** 环境问题不该制造假警报——否则你会开始忽略红灯。
**代码**：`tests/conftest.py::java_alive` 只要拿到 HTTP 响应就算活着（**哪怕 401/500**），`require_java` 据此 skip；`pytest.ini` 用 `-ra` 让 skip 原因也列出来。

---

# D. Agent 设计意图

### Q22. Intent 判定为什么用 LLM，不用关键词？ ★必答
**答**：中文口语里「它呢？」「再往上看看」关键词必然失效——要么写不全，要么一写就误伤。
**代码**：`core/intent.py::classify_intent` + `core/prompt.py::intent_messages`（六个 intent 连同**各自触发语义**一起列，因为"六个选项"不够，LLM 不知道 FLOW 和 RELATION 的分界）。

### Q23. 六个 Intent 为什么是封闭枚举？ ★必答
**答**：开放枚举等于把「能答什么」的决定权交给 LLM——遇到"帮我写个登录接口"它就能现场发明 `EXPLORE_WRITE_CODE`。
**代码**：`models/intent.py::Intent`（StrEnum 六个）；防线是 `parse_intent` 的 `model_validate` —— **非法枚举在模型校验层就被拒**（`test_intent.py::test_parse_intent_rejects_invented_intent`）。

### Q24. `confidence < 0.6` 为什么不执行工具？ ★必答
**答**：**错的图比不回答伤害大得多**——用户看到一张看起来对、实际无关的图，既不知道错了，也没法纠正。
**代码**：`models/intent.py` 的 `CONFIDENCE_THRESHOLD = 0.6`（模块常量不是模型字段，因为它是**策略**不是**数据**）+ `IntentResult.needs_clarification` 属性；处置在 `core/loop.py::_clarify`（**先于**任何工具调用）。

### Q25. 代词消解怎么做？规则表为什么要写进 prompt？ ◆讲原理
**答**：「它」指代谁，在这个系统里**有正确答案**（答案就在 `focus` 里），所以不该让 LLM 自由发挥。
**代码**：`core/prompt.py::_PRONOUN_RULES`（四条规则表）；代码侧的落点是 `tools/registry.py::resolve_uid`——`slots.entity_ref` 优先，空则回落 `ctx.focus.uid`。

### Q26. NAVIGATE 为什么不该花第二次 LLM？ ★必答
**答**：它只是移动视图，没有东西需要"讲解"。这是 **E2 成本决策**。
**代码**：`models/intent.py::NO_EXPLAIN_INTENTS = frozenset({NAVIGATE, OUT_OF_SCOPE})`——单独拎出来而不是散在 if 里，因为它是**成本决策，不该被埋进流程代码**；`core/loop.py::_navigate` 只产 `UICmd` 不调 LLM。
**⚠️ 诚实补充**：目前 NAVIGATE 仍调 1 次 LLM（要判出它是 NAVIGATE 就得调 LLM #1）。真做到零 LLM 需要前端直接声明意图（P4/P5）。

### Q27. 有锚点和没锚点，能力差多少？ ★必答
**答**：有锚点 → 5 个工具全用得上；没锚点 → 只能答 overview 或让 LLM 在类清单里猜。**所以 V1 不做自由问答，做「先选类，再提问」。**
**代码**：`tools/registry.py::ToolSpec.needs_anchor` + `plan()`——`needs_anchor and ctx.focus is None` 时**返回 None，提前拦住**，不空跑一次注定 `ok:false` 的工具调用。

---

# E. 幻觉与安全（你项目的"思想高度"）

### Q28. 四道拦截分别防什么？ ★必答

| 编号 | 风险 | 防线 | 代码 |
| --- | --- | --- | --- |
| A2 | LLM 在不存在的 uid 上生成命令 | 引用必须 ⊆ evidence，越界丢弃 | `core/guard.py::filter_ui_commands` / `split_citations` |
| A3 | 意图猜错，拉出错的图 | `confidence < 0.6` → 不执行工具 | `core/loop.py::_clarify` |
| A4 | LLM 用常识补全断链 | `gaps` 如实返回 + prompt 强制提及 | `core/prompt.py` 硬规则 2 |
| C3 | 变长查询打爆 Neo4j | 预算**显式传参** | `tools/java_client.py` 顶部约束③ |

### Q29. `citations ⊆ evidence` 是怎么实现的？ ★必答
**答**：**先选 citations，再用 citations 拼 message**——顺序不能反。反过来做就成了"先写讲解、再回头找出处"，必然引用到 evidence 之外的东西。这样反过来，**「引用没出处」结构上不可能发生**。
**代码**：`core/explain.py::_template_chain`（降级路径里保留了这个顺序，注释 ★ 标出）；接上 LLM 后由 `_parse_and_ground` + `guard.split_citations` 承担。

### Q30. 接上 LLM 之后这条保证被削弱了吗？ ★必答（**分水岭**）
**答**：**被削弱了。** message 是自由文本，拦不住它提及图外的类。现在只剩 prompt 硬规则 + 事后剔除（越界记 **error** 不是 warn）。这是**接 LLM 必然的代价，不是疏忽**。
**代码**：`core/explain.py` 顶部 docstring 专门写了「一条被削弱的保证（要如实知道）」——**代码里留了自己的弱点说明**，这是很强的信号。

### Q31. 为什么放弃了「扫 message 里的类名」这个检查？ ◆讲原理
**答**：LLM 写简单名（`OrderService`）而 evidence 是全限定名（`…OrderServiceImpl`），这类比较**必然大量误报**。天天误报的检查等于没有检查，还会训练人忽略它。
**代码**：`core/guard.py` 顶部 docstring 记了这次"考虑过又放弃"的决策。

### Q32. `gaps` 为什么必须讲出来？ ★必答
**答**：把「跨服务调用」讲成「没找到」，等于丢掉了**微服务项目里最有价值的信息**。
**代码**：`core/prompt.py` 硬规则 2；降级模板里也保留（`_template_chain` 里 ★ 标出的段落）——**降级了也不能把断链讲成"到此结束"**。

---

# F. LLM 工程

### Q33. `json_object` 模式有什么前置条件？ ◆讲原理（**答得出来的人很少**）
**答**：DeepSeek（同 OpenAI 规则）要求 **prompt 里必须出现 "json" 字样**，否则 400，且报的错会指向别处、很难查。
**代码**：`core/llm.py::complete` 的注释把这条约束**显式交给调用方**（`core/prompt.py` 负责）；`tests/test_intent.py::test_intent_prompt_contains_json_keyword` —— 这条测试存在的唯一理由就是防止有人润色提示词时删掉那个词。

### Q34. temperature 为什么默认 0？ ◆讲原理
**答**：Intent 判定要的是**可复现**，不是创造力。
**代码**：`core/llm.py::complete(temperature: float = 0.0)`；要变化时调用方显式传。

### Q35. LLM 挂了怎么办？哪些错该降级、哪些该炸？ ★必答
**答**：分三类——
- `LLMNotConfiguredError`（没配 key）→ **必须炸成 500**
- `LLMTransportError`（超时/连不上）→ **降级**：讲解退化成模板
- Intent 判定挂了 → 回落到 **P2 的规则**（有 focus 就当流程问题）
**关键**：第三类的降级路径是「**已经跑过的老路径**」，不是我新写的没验证过的分支。
**代码**：`core/llm.py` 三个异常类；`core/loop.py::_classify` 的 `except LLMNotConfiguredError: raise` 在前、`except LLMError` 在后。

### Q36. Prompt 里最重要的三样东西是什么？ ◆讲原理
**答**：① 工具结果 ② **可引用 uid 白名单** ③ 四条硬规则。
**代码**：`core/prompt.py::explain_messages` —— 硬规则写死在 system 里，白名单由 `_render_evidence` 从**信封的 evidence**（不是 data）里生成。

### Q37. 为什么 P2 的模板函数搬进 `explain.py` 而不是删掉？ ★必答
**答**：它**恰好就是设计要求的 LLM 降级路径**（C2）。不是历史包袱，是预案。
**代码**：`core/explain.py::_template` / `_template_chain` / `_template_overview` —— 文件 docstring 直接写"P2 的模板函数没有随桩一起删"。

---

# G. 测试策略

### Q38. 为什么 LLM 和 Java 都要替换成 fake？ ★必答
**答**：两者都不确定性——LLM 每次措辞不同，Java 要联网。拿它们做断言，测出来的是「今天这两个恰好在」，不是「我们的逻辑对」。
**代码**：`tests/fakes.py` 顶部 docstring。

### Q39. fake 和 mock 的区别？为什么用 fake？ ◆讲原理
**答**：fake 是**有真实行为的手写替身**；mock 主要记录调用参数。我要测**行为**（低置信度会不会误调工具），不只是测**调用**。
**代码**：`tests/fakes.py` 顶部 docstring 明说"命名用 fake 不用 mock"。

### Q40. `FakeLLM` 的剧本元素为什么可以是异常？ ◆讲原理
**答**：`FakeLLM([LLMTransportError(...), '{"message":...}'])` 表示「第一次挂、第二次正常」——**一轮里两层降级一次覆盖**。
**代码**：`tests/fakes.py::FakeLLM.complete` 的 `isinstance(item, Exception)` 分支；`test_loop.py::test_intent_llm_down_falls_back_to_p2_rule` 用的就是它。

### Q41. `Depends` 依赖注入和测试什么关系？ ★必答
**答**：**这是 P3 那层单测能写出来的前提。** 原来路由从 `request.app.state.java_client` 直接拿，测试只能拿到真对象。
**代码**：`api/agent.py::get_java_client` / `get_llm_client` 两个函数——注释直接写"这两个函数的存在，是 P3 那一整层单元测试的前提"；用法见 `test_contract.py::test_6` 的 `app.dependency_overrides[get_llm_client] = lambda: fake`。

### Q42. 为什么这个测试套件「绿不代表代码对」？ ★必答
**答**：契约测试是「**当前 Java 实际行为**」的快照。绿只代表**两边还没漂移**。
**代码**：`tests/test_contract.py` 顶部 docstring。

### Q43. 三层测试各测什么？ ◆讲原理
**答**：纯单测（无网，测分支，0.2s）/ 契约（打真 Java，测漂移）/ 冒烟（真 LLM，人工看质量）。
**代码**：`pytest.ini` 的 `testpaths`/`-ra`；`tests/` 下 `test_guard|intent|loop`（单测）+ `test_contract`（契约）。

---

# H. 可靠性与工程细节

### Q44. 级联超时怎么设计？现在有什么矛盾？ ★必答（**能答出来非常亮**）
**答**：设计是「Java 代理 Python 8s / Python 调 Java 8s / 整轮 60s」——**这三条自相矛盾**：Python 一轮要调两次 LLM，8s 会把还没答完的轮次掐断。建议改成 Java 代理 **45–60s**，8s 只保护"工具不该慢"。
**代码**：`java_client.py::TOOL_TIMEOUT_SECONDS = 8.0`；`config.py::llm_timeout = 25.0`；矛盾写在 [P4 设计](10月1%20P4%20网关与Context设计.md) §3.2。

### Q45. 共享连接池为什么必须共享？ ◆讲原理
**答**：不只是省连接——更关键是**丢掉连接池自带的并发上限**。循环一并发发几个工具调用就可能打爆 Java。
**代码**：`java_client.py::__init__` 建共享 `httpx.AsyncClient`，注释原文：「P3 的循环一轮可能并发发好几个工具调用，没有闸门就可能一下子打爆 Java。」

### Q46. `lifespan` 是什么？ ◆讲原理
**答**：FastAPI 的启动/关停钩子。`yield` 前建资源、后释放；`yield` 那一刻开始接请求。
**代码**：`main.py::lifespan`（`java_client` + `llm_client` 都在这建）。

### Q47. 工具返回 `ok:false` 为什么不算异常？ ★必答
**答**：它是**业务结果**（类不存在、项目未分析），不是系统故障。HTTP 仍是 200，Python 原样转述原因。
**代码**：`java_client._post` —— 只对 401 / 非 200 / 运输错误抛异常；`core/loop.py::_tool_said_no` 原样转述 `note`。

### Q48. 401 为什么不降级？ ★必答
**答**：两边 token 对不上是**配置错误**。降级成"工具暂时答不了"会把配置问题藏起来。
**代码**：`java_client::InternalAuthError`（docstring 明确"必须炸出来"）；`core/loop.py` 里 `except InternalAuthError: raise` **写在 `except ToolTransportError` 之前**。

### Q49. `trust_env=False` 是干什么的？ ◆讲原理
**答**：Windows 上 httpx 从**注册表**读系统代理。本机 Clash 挂在 127.0.0.1:7897，`trust_env=True` 时**连 localhost:8080 都会被丢给代理**，表现为超时/502，而同一台机器 curl 却正常——**极难排查**。
补充：调 DeepSeek 时实测两种设置都通，**仍然选了 False**（行为可预测）。
**代码**：`java_client.py::__init__` 的 ★ 注释（最详细）；`core/llm.py` 顶部 docstring。

### Q50. `MAX_NODES` 硬上限是多少？ ◆讲原理（**设计文档写错过**）
**答**：设计文档写「硬上限 40」是**错的**。40 是控制器默认值，真硬上限是 `MAX_NODES_HARD = 200`（depth 是 3）。Java 是 **clamp 不是拒绝**。
**代码**：`InternalToolController.java:48-54`（默认值）+ `CallChainServiceImpl.java:44-45`（硬上限）+ `:115-116`（`Math.min` clamp）。**结论**：Java 不会替 Agent 拦到 40——预算必须 Python 显式传。

---

# I. 产品与边界

### Q51. 为什么只能「先点类再提问」？ ★必答
**答**：因为**没有 embedding 就没有语义入口**。用户必须说对类名。
**代码**：`ArchMind/src/main/resources/application.yml` 里 `spring.ai.model.embedding: none`（Spring AI 配置里写死了）——**这是可以直接指出来的证据**。
**主动补充**：`classes` 默认 `limit=40` 的排序偏差实测确认过——返回的 40 条全是老单体包，`trade` 一个都没有，所以"让 LLM 在清单里选"这条兜底基本失效。

### Q52. `suggestions` 为什么叫「闭环的发动机」？ ★必答
**答**：没有它，用户答完一轮不知道该说啥，对话转不动。
**代码**：`core/loop.py::_suggestions` —— 只给两条（多了是噪音）；`models/response.py::Suggestion.intent` 在 P3 从裸 `str` **收紧成 `Intent` 枚举**，因为前端点一下就是发一轮 `ask`。

### Q53. UICommand 为什么从 9 个砍到 4 个？ ◆讲原理
**答**：砍掉的是 `ADD_NODE`/`REMOVE_NODE`/`HIGHLIGHT_*`。理由：要求 LLM 精确操作图结构——**LLM 一旦算错图就不一致**。核心原则：「**LLM 声明要看什么，工具产看的数据，前端算怎么画**」。
**代码**：`models/response.py::UiOp` 只有 4 个值（`Literal`/StrEnum 钉死，LLM 想返回不存在的 op 在模型校验层就被拒）。

### Q54. `message` 和 `uiCommands` 为什么解耦？ ◆讲原理
**答**：独立失败互不拖累——图挂了字还在。
**代码**：`models/response.py::AgentResponse` 里两个独立字段；`core/loop.py` 里 `_ui_commands()` 和 `explain()` 是两段独立逻辑。

---

# J. 取舍、债务与演进

### Q55. 你对原设计做了哪些主动偏离？为什么？ ★必答（**答得好直接加分**）
**答**：四处，全是**收窄**不是偷懒。见下表（每处的原因都写在代码 docstring 里）：

| # | 偏离 | 理由 | 代码 |
| --- | --- | --- | --- |
| 1 | UICommand 确定性推导，不交给 LLM | 让 LLM 生成再事后拦 = **先制造幻觉再拦** | `core/loop.py::_ui_commands` |
| 2 | intent→工具用分发表，不是 LLM 规划的 ReAct | 先把主干跑通测透，那时预算与去重才有意义；接缝留在 `spec.handler()` | `tools/registry.py` 顶部 docstring |
| 3 | 不做 message 侧类名扫描 | 简单名 vs 全限定名**必然大量误报** | `core/guard.py` 顶部 docstring |
| 4 | Context 只存内存 + 砍掉无读写方的死字段 | `anchor.target`/`viewport` P3 无人读写 | `models/context.py` 顶部 docstring |

### Q56. 现在有哪些债？ ★必答
**答**：
| 债 | 影响 | 来源 |
| --- | --- | --- |
| UP 方向语义不一致 | Agent 问实现类「谁调用了它」答错 | P1 遗留 |
| ReAct 多步循环未做 | 一轮只查一次图 | P3 主动挂起 |
| 超时预算矛盾 | LLM 慢时网关会掐断 | 设计文档 |
| `deploy/.env` 缺 `INTERNAL_TOKEN` | 挡 P6 容器化 | P2 记录 |
| `DEEPSEEK_API_KEY` 存两处 | key 轮换要改两处 | P3 |
| `@Lazy` 临时破环 / 上传后未清旧概况 / 删项目未清 task 行 | — | 路线图自记 |

### Q57. 为什么不引 LangChain？ ★必答
**答**：① 边界控制——ReAct 循环、预算、四道拦截都要自己攥着，框架会把这些藏起来；② 面试时这是谈资：**我自研了循环，所以我讲得清它每一步在干什么**。
**代码**：`requirements.txt` 只有 7 个依赖，**没有 langchain / openai**——LLM 调用是 `core/llm.py` 里一段 40 行的 httpx 裸 POST。

### Q58. MQ 什么时候上？为什么要上？ ◆讲原理
**答**：把 `analyze` 从线程池改成 MQ 驱动是**真实需求**（可靠投递、失败重试、削峰），不是硬塞。但现在规模下线程池够用——**先跑起来，再跑得好**。
**代码/文档**：路线图「统一任务系统」段；`TaskType.QA` 常量**已存在但 Agent 链路不走它**（生命周期完全不同）。

### Q59. 微服务的演进路线是什么？ ◆讲原理
**答**：两个服务 + 共享密钥 → LLM 能力收拢到 Python 成为可复用服务（Java 分析流水线改调 `/internal/llm/complete`）→ 服务发现 → mTLS/服务网格 → 容器编排。
**代码/文档**：设计 §11；`application.yml` 里 Spring AI 的 key 和 `agent-service/.env` 里的 key **是同一个**——这就是"两处持有 LLM key"的现状。

---

# K. 陷阱题 / 压力题

### Q60. 「这不就是 Sourcegraph / CodeSee 吗？」 ★必答
**答**：重叠：都有代码图谱和可视化。**分野**：他们是**静态检索 + 搜索**，我是**以 Agent 为交互界面**——图谱是 Agent 的**数据源**，不是终点。主动说清边界：单语言、几千文件规模、无 embedding。

### Q61. 「你怎么保证 LLM 不胡说？」 ★必答
**答**：不要说"我 prompt 写得好"。要说**机制**：引用必须 ⊆ evidence（结构化可校验）+ 断链强制披露 + 低置信不执行工具。**并从结构上承认 message 侧仍依赖 prompt**。
**代码**：`core/guard.py` 三个纯函数就是这三条机制的代码化。

### Q62. 「为什么不用 RAG / 向量检索？」 ★必答
**答**：① **DeepSeek 没有 embedding 接口**（`application.yml` 里 `embedding: none`），技术上做不了，换 Qwen/智谱是明确下一步；② **更本质的区别**：RAG 解决"找到相关片段"，我这个问题需要的是**精确的关系**（谁调谁）——图查询恰好是对形态，向量检索反而引入"近似相关"。
**代码**：`requirements.txt` 里**没有任何向量库**。

### Q63. 「你项目最难的地方是什么？」 ★必答
**答**：三选一讲深，别三个都提——① 契约漂移（静默失败，最难查）② 反向扇出把死路变分支 ③ 让 LLM 不编造（从 prompt 口号变成可校验机制）。

### Q64. 「如果给你重做，你会改什么？」 ◆讲原理
**答**：把 `CheckProjectUtil` 的租户边界**提前到 P1**（现在 P4 才补，中间几周是裸的）；超时预算**一开始就按整轮算**。

### Q65. 「这个项目有多少代码？你写了多久？」 ★必答
**答**：Agent 服务 P2+P3 约 **7 个自然日**；测试从 6 个涨到 **48 个**（47 passed / 1 skipped）；`4ad142f` 一个提交 19 文件 +2326/-220。**数字答不上来会显得不是自己写的。**

---

# 附：三个你现在**答不好**的（先知道是缺口，别急着背）

| 缺口 | 现状 | 补的时机 |
| --- | --- | --- |
| **UP 方向语义不一致** | `class-relations` 的 UP 只看直接 `CALLS` 边（问 `OrderServiceImpl` 得"只有自己"）；`call-chain` 的 UP 带反向扇出才对（得 3 个真实调用方）。**实测：问接口两者一致，问实现类才岔开** | P4 前想清修法 |
| **ReAct 多步循环** | 确定性单步分发，一轮只查一次图；接缝在 `core/loop.py` 的 `spec.handler(java, inv)` | P4 前后 |
| **超时预算矛盾** | 见 Q44 | P4.0 必须拍板 |

---

## 建议的巩固节奏
1. **先攻 A（架构）+ E（幻觉）+ J（取舍）** —— 这三块是面试官真正想听的
2. B / C / F / H 是技术细节，**被追问时才展开**
3. I / K 是守住边界不吹牛
