"""测试替身（fake）。

**为什么需要它们**：P3 的判定/循环链路一碰就是 LLM + Java 两个外部依赖，
两个都不是确定性的 —— LLM 每次措辞不同，Java 要联网。拿它们做断言，
测出来的就是「今天这两个恰好在」而不是「我们的逻辑对」。

替身让链路变得**可预测**：给定输入，输出完全固定。所以 P3 的分支逻辑
（低置信回澄清、NAVIGATE 不花第二次 LLM、预算耗尽即停）都能被钉死。

命名用 fake 不用 mock：这些是**有真实行为的手写替身**，不是「记录调用参数」的桩。
"""

from app.core.llm import LLMResult
from app.models.tool import CallChain, ClassNode, NodeKind, ToolEnvelope


class FakeLLM:
    """按剧本吐回复的 LLM。

    用法：
        llm = FakeLLM(['{"intent": "EXPLORE_FLOW", "confidence": 0.9}'])
        await classify_intent(ctx, "订单流程是什么", llm)
        assert llm.call_count == 1

    剧本的元素可以是**字符串**（正常返回）或**异常实例**（这一次调用抛它）——
    后者用来测降级：比如
        FakeLLM([LLMTransportError("超时"), '{"message": "..."}'])
    表示「第一次调用挂了、第二次正常」，一轮里两层降级都能覆盖到。

    `replies` 用完之后再调会**直接抛 AssertionError**，而不是循环使用或返回空。
    这是刻意的：`call_count` 是好几条测试的核心断言（比如「NAVIGATE 不该调 LLM #2」），
    剧本耗尽却静默继续，会把「多调了一次」这个 bug 伪装成正常。
    """

    def __init__(self, replies: list[str | Exception]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, object]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> LLMResult:
        self.calls.append({"messages": messages, "json_mode": json_mode, "temperature": temperature})

        if not self._replies:
            raise AssertionError(
                f"FakeLLM 的剧本用完了，但又被调了第 {len(self.calls)} 次 —— 多半是多调了一次 LLM"
            )
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(text=item, tokens_in=11, tokens_out=22)


class FakeJava:
    """记录调用并返回固定信封的 JavaClient 替身。

    只实现被 registry 用到的 5 个方法。**没有 `_post`、没有 httpx** ——
    它是靠鸭子类型顶替真 client 的，一旦哪天 registry 开始调别的方法，它会立刻
    `AttributeError` 炸出来。这正是想要的效果：接口变了，测试当场就红。
    """

    def __init__(self, envelope: ToolEnvelope | None = None, *, raises: Exception | None = None) -> None:
        self._envelope = envelope if envelope is not None else ok_overview()
        self._raises = raises
        self.calls: list[tuple[str, int, str | None]] = []
        #: 每次调用的关键字参数。用来验「slots 的 direction/depth 真的传下去了」——
        #: 这正是 P2 契约测试里「参数静默降级」那个坑的翻版，值得在 P3 也盯一次。
        self.kwargs: list[dict[str, object]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def _record(
        self, name: str, project_id: int, uid: str | None = None, **kw: object
    ) -> ToolEnvelope:
        self.calls.append((name, project_id, uid))
        self.kwargs.append(kw)
        if self._raises is not None:
            raise self._raises
        return self._envelope

    async def overview(self, project_id: int) -> ToolEnvelope:
        return await self._record("overview", project_id)

    async def classes(self, project_id: int, limit: int = 40) -> ToolEnvelope:
        return await self._record("classes", project_id, limit=limit)

    async def class_relations(self, project_id: int, uid: str, **kw) -> ToolEnvelope:
        return await self._record("class-relations", project_id, uid, **kw)

    async def call_chain(self, project_id: int, uid: str, **kw) -> ToolEnvelope:
        return await self._record("call-chain", project_id, uid, **kw)

    async def method_body(self, project_id: int, uid: str | None = None, **kw) -> ToolEnvelope:
        return await self._record("method-body", project_id, uid, **kw)


# ==================== 信封构造 ====================

ORDER_CONTROLLER = "com.hmall.trade.controller.OrderController"
ORDER_SERVICE = "com.hmall.trade.service.impl.OrderServiceImpl"


def ok_overview() -> ToolEnvelope:
    from app.models.tool import ProjectOverview

    return ToolEnvelope(
        ok=True,
        data=ProjectOverview(project_type="微服务", summary="电商交易系统"),
        evidence=[],
        truncated=False,
        graph_rev=1,
    )


def ok_chain() -> ToolEnvelope:
    """一条最小但形状完整的调用链：起点 + 一个下游 + 一个跨服务断点。

    带一条 Evidence —— 否则 citations 会全被 guard 丢掉，测不出「有出处」这条。
    """
    from app.models.tool import (
        ChainEdge, ChainNode, Evidence, EvidenceKind, Gap, GapReason, RelationType,
    )

    start = ClassNode(
        qualified_name=ORDER_CONTROLLER, simple_name="OrderController",
        package_name="com.hmall.trade.controller", kind=NodeKind.CLASS,
    )
    downstream = ChainNode(uid=ORDER_SERVICE, depth=1, via_fanout=False, edge_count=1,
                           module="trade")
    return ToolEnvelope(
        ok=True,
        data=CallChain(
            start=start,
            nodes=[downstream],
            edges=[ChainEdge(from_=ORDER_CONTROLLER, to=ORDER_SERVICE,
                             rel=RelationType.CALLS, call_count=1, via_fanout=False)],
            gaps=[Gap(at=ORDER_SERVICE, reason=GapReason.REMOTE_SERVICE)],
            truncated=False,
        ),
        evidence=[Evidence(kind=EvidenceKind.EDGE, from_=ORDER_CONTROLLER,
                           to=ORDER_SERVICE, rel=RelationType.CALLS, count=1)],
        truncated=False,
        graph_rev=1,
    )


def failed(note: str = "类不存在") -> ToolEnvelope:
    return ToolEnvelope(ok=False, data=None, evidence=[], truncated=False, note=note, graph_rev=1)
