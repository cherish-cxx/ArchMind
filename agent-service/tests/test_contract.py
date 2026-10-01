"""T5 契约测试 —— A1（契约漂移）的防线。

设计 §7 的 5 个测试 + 1 个覆盖 T4 grounding 属性的 e2e。

⚠️ 这 6 个测试是「**当前 Java 实际行为**」的快照。绿不代表代码对，
只代表两边还没漂移。Java 哪天改了响应形状或参数语义，它们会红 —— 那正是它们的意义。
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.agent import get_llm_client
from app.config import INTERNAL_TOKEN_HEADER, settings
from app.main import app
from app.tools.java_client import JavaClient
from fakes import FakeLLM

# 测试 1 只关心「发出去的请求长什么样」，不关心响应。
# 用 ok:false 的信封最省事：它对**所有**工具的返回模型都合法（data 可以是 None）
_MOCK_ENVELOPE = {
    "ok": False,
    "data": None,
    "evidence": [],
    "truncated": False,
    "note": "T5 只抓请求体",
    "graphRev": 0,
}

# (接口短名, 调用, 期望的 args key 集合) —— 期望值照设计 §3.3 的全表写
_CALLS = [
    ("overview", lambda c, pid: c.overview(pid), set()),
    ("classes", lambda c, pid: c.classes(pid, limit=7), {"limit"}),
    (
        "class-relations",
        lambda c, pid: c.class_relations(pid, "a.B", limit=9),
        {"uid", "direction", "limit"},
    ),
    (
        "call-chain",
        lambda c, pid: c.call_chain(pid, "a.B", max_depth=3, max_nodes=5),
        {"uid", "direction", "maxDepth", "maxNodes"},
    ),
    ("method-body", lambda c, pid: c.method_body(pid, uid="a.B"), {"uid", "maxMethods", "maxLines"}),
]


# ==================== 测试 1：请求 key 必须 camelCase（不依赖 Java）====================


async def test_1_request_keys_are_camel_case(project_id):
    """拦坑① 和坑②。

    Java 用 `strArg` / `intArg` 取值，**key 找不到就返回默认值，不抛异常、不记日志**。
    所以 Python 这边顺手写成 `method_uid`、`max_depth`，会得到 HTTP 200 + 参数被静默忽略
    —— 编译期过、运行时不报错，只有结果悄悄不对。这是 A1 最凶的形态。

    这条**不依赖 Java**（用 MockTransport 抓请求体），随时能跑。
    """
    captured: dict[str, dict] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured[req.url.path.rsplit("/", 1)[-1]] = json.loads(req.content)
        return httpx.Response(200, json=_MOCK_ENVELOPE)

    c = JavaClient()
    # 换掉内部的 httpx client 只是为了拦住出网。碰私有属性在测试里可接受 ——
    # 加到生产代码上的 transport 参数只为测试服务，不值得。
    original = c._client
    c._client = httpx.AsyncClient(
        base_url="http://mock",
        transport=httpx.MockTransport(handler),
        timeout=8.0,
        headers={INTERNAL_TOKEN_HEADER: "t"},
        trust_env=False,
    )
    await original.aclose()

    problems: list[str] = []
    try:
        for name, call, expected in _CALLS:
            await call(c, project_id)
            body = captured[name]

            if set(body) != {"projectId", "args"}:
                problems.append(f"{name}: 顶层 key 是 {set(body)}，应为 {{projectId, args}}")
            if body["projectId"] != project_id:
                problems.append(f"{name}: projectId 没传对（{body['projectId']}）")

            keys = set(body["args"])
            if keys != expected:
                problems.append(f"{name}: args key {sorted(keys)} != 期望 {sorted(expected)}")
            snake = sorted(k for k in keys if "_" in k)
            if snake:
                problems.append(f"{name}: 出现 snake_case {snake} —— Java 会静默吃默认值（坑①）")

        # 坑②：数字必须是 JSON number，不能是字符串
        cc = captured["call-chain"]["args"]
        for k in ("maxDepth", "maxNodes"):
            if not isinstance(cc[k], int):
                problems.append(f"call-chain.{k} 是 {type(cc[k]).__name__}，必须是 JSON number（坑②）")
    finally:
        await c.aclose()

    assert not problems, "请求形状不对：\n  " + "\n  ".join(problems)


# ==================== 测试 2：★ 参数真的生效了吗 ====================


async def test_2_max_depth_is_actually_applied(client, project_id, chain_start):
    """★ 全组最有价值的一条 —— **唯一能抓出「参数静默降级」的测试**。

    坑②⑤：key 写错、或数字发成字符串，Java 都静默吃默认值，HTTP 还是 200。
    要抓它，就必须让「传 1」和「传 2」产生**不同**的结果 —— 否则测了等于没测。

    ⚠️ **起点不能随便选。** 实测 `com.hmall.trade.service.IOrderService` 在
    maxDepth=1 和 2 下结果**完全相同**（都是 maxD=1）—— 用它会永远绿。
    `OrderController` 才分得出 1 层（3 个节点）和 2 层（8 个节点）。
    """
    shallow = await client.call_chain(project_id, chain_start, max_depth=1)
    deep = await client.call_chain(project_id, chain_start, max_depth=2)
    assert shallow.ok, f"maxDepth=1 没跑通：{shallow.note}"
    assert deep.ok, f"maxDepth=2 没跑通：{deep.note}"
    assert shallow.data and deep.data

    max1 = max(n.depth for n in shallow.data.nodes)
    max2 = max(n.depth for n in deep.data.nodes)

    assert max1 <= 1, f"maxDepth=1 却出现了 depth={max1} 的节点 —— 参数没被读到"
    assert max2 == 2, f"maxDepth=2 却没有 depth=2 的节点（拿到 {max2}）—— 参数没被读到"


# ==================== 测试 3：非法枚举是业务失败，不是异常 ====================


async def test_3_call_chain_rejects_both_direction(client, project_id, chain_start):
    """拦坑·附：`call-chain` 的 direction 只有 DOWN / UP，**没有 BOTH**
    （DOWN 是多层 BFS、UP 是单层反查，本来就没有「双向」这个组合）。

    它与 `class-relations` 的 `RelationDirection` 是两个枚举，别图省事合成一个。
    Java 拒绝非法枚举走的是 **HTTP 200 + ok:false**，不是异常。
    """
    env = await client.call_chain(project_id, chain_start, direction="BOTH")  # type: ignore[arg-type]

    assert env.ok is False, "BOTH 竟然被接受了 —— 枚举定义漂移了？"
    assert env.data is None, "业务失败时 data 应为 None"
    assert env.note, "业务失败必须给出原因"


# ==================== 测试 4：不存在的类是业务结果 ====================


async def test_4_missing_class_is_business_failure(client, project_id):
    """拦坑③⑥：查一个不存在的类是**业务结果**，HTTP 200 + ok:false，不是 500。

    `java_client` 不能只看 status code，也不该把它抛成异常。
    （**故意不断言 note 的措辞** —— 文案不是契约，只要求它给出原因。）
    """
    env = await client.class_relations(project_id, "com.nope.NoSuchClass")

    assert env.ok is False
    assert env.data is None
    assert env.note and env.note.strip()


# ==================== 测试 5：graphRev=0 不是错误 ====================


async def test_5_overview_on_unmapped_project(client, unmapped_project_id):
    """拦坑④：`graphRev=0` 表示「从未落图」或「图是版本号机制上线前建的」，**不是错误**。

    前置条件：一个「已创建但没落图」的项目（overview 有数据、graphRev=0）。
    hmall 已落图（graphRev=1），给不了这个条件。
    """
    if unmapped_project_id is None:
        pytest.skip(
            "缺少前置数据：需要一个「已创建但未落图」的项目（overview 有数据、graphRev=0）。"
            "用环境变量 TEST_UNMAPPED_PROJECT_ID 指定"
        )

    env = await client.overview(unmapped_project_id)

    assert env.ok is True, f"未落图项目也该答得出来：{env.note}"
    assert env.graph_rev == 0, "从未落图时 graphRev 应为 0"
    assert env.data is not None


# ==================== 测试 6：grounding —— 幻觉引用被真的拦掉了 ====================


def test_6_hallucinated_citation_and_uicmd_are_filtered(
    require_java, project_id, chain_start
):
    """P3 的 grounding 端到端验证 —— 打**真 Java**，但把 LLM 换成剧本。

    为什么必须换掉 LLM：这一条要验的是「拦截机制在真实数据上生不生效」，
    不是「今天 DeepSeek 心情好不好」。真 LLM 进来，测试就变成不确定的了。

    P2 时这个测试读的是 `debugToolResults` 拿原始信封；P3 删了那个临时字段，
    改成**从外面注入一个必然幻觉的引用**，直接断言它没能出现在结果里 ——
    比原来更好：原来看的是「我们引用的都在 evidence 里」，
    现在看的是「我们硬塞了一个不在 evidence 里的，它被丢掉了」，验证的是**拦截本身**。
    """
    ghost = "com.hallucinated.GhostService"
    # LLM #1 判意图；LLM #2 故意在 citedUids 里混进一个不存在的类
    fake = FakeLLM(
        [
            json.dumps({"intent": "EXPLORE_FLOW", "confidence": 0.95}),
            json.dumps({"message": "顺着调用链往下看。", "citedUids": [ghost, chain_start]}),
        ]
    )

    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/internal/agent/ask",
                headers={INTERNAL_TOKEN_HEADER: settings.internal_token},
                json={
                    "projectId": project_id,
                    "message": "订单创建的流程是什么",
                    "focus": {"type": "CLASS", "uid": chain_start},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()

    cited = {x["uid"] for x in body["citations"]}

    assert ghost not in cited, f"★ 幻觉引用 {ghost} 没被拦住 —— guard 失效了"
    assert chain_start in cited, f"真实节点 {chain_start} 应该在 evidence 里，却被误拦了"

    # 出图命令必须指向被引用过的真实节点，不能凭空指向一个 uid
    targets = [c["target"]["uid"] for c in body["uiCommands"] if c.get("target")]
    assert targets, "call-chain 应该产出 SHOW_PATH"
    assert set(targets) <= cited, f"UICommand 指向了没被引用的节点：{set(targets) - cited}"
