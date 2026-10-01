"""A2 / A4 拦截的单元测试 —— **不打网络、不打 LLM、不打 Java**。

这是 P3 里跑得最快、也最该先绿的一层：guard 是纯函数，构造输入就能断言，
不需要任何环境准备。集成测试（test_contract.py）负责回答「两边漂移了吗」，
这里负责回答「拦截逻辑本身对不对」—— 两个问题，两层测试。
"""

from app.core.guard import evidence_uids, filter_ui_commands, split_citations
from app.models.response import Citation, NodeRef, NodeRefType, UICmd, UiOp
from app.models.tool import Evidence, EvidenceKind, ToolEnvelope

_A = "com.hmall.trade.controller.OrderController"
_B = "com.hmall.trade.service.IOrderService"
# 这个 uid 刻意不出现在任何 evidence 里 —— 它就是「幻觉」的样本
_GHOST = "com.hmall.trade.service.PaymentGatewayService"


def _env(*, from_: str, to: str | None = None) -> ToolEnvelope:
    # CamelModel 开了 populate_by_name，所以字段名 `from_` 可以直接传（不必走 alias "from"）
    return ToolEnvelope(
        ok=True,
        evidence=[Evidence(kind=EvidenceKind.EDGE, from_=from_, to=to)],
        truncated=False,
    )


def _cmd(op: UiOp, uid: str | None) -> UICmd:
    target = NodeRef(type=NodeRefType.CLASS, uid=uid) if uid else None
    return UICmd(op=op, target=target, rev=0)


# ==================== evidence_uids ====================


def test_evidence_uids_collects_both_ends():
    """一条边有两个端点，两个都得算「有出处」。"""
    uids = evidence_uids([_env(from_=_A, to=_B)])
    assert uids == {_A, _B}


def test_evidence_uids_tolerates_missing_to():
    """`to` 为 None 的 evidence（如 CLASS 类凭据）不该被算成一个 uid 叫 None。"""
    uids = evidence_uids([_env(from_=_A)])
    assert uids == {_A}
    assert None not in uids


def test_evidence_uids_merges_across_envelopes():
    """一轮可能调多个工具，evidence 要跨信封合并 —— 否则第二个工具的结果永远引不了。"""
    uids = evidence_uids([_env(from_=_A), _env(from_=_B)])
    assert uids == {_A, _B}


# ==================== filter_ui_commands（A2 主防线）====================


def test_filter_drops_command_on_hallucinated_node():
    """★ 核心断言：指向不存在节点的命令被丢，且**能被指出来**。"""
    good = _cmd(UiOp.FOCUS_NODE, _A)
    bad = _cmd(UiOp.EXPAND_NODE, _GHOST)

    kept, dropped = filter_ui_commands([good, bad], {_A, _B})

    assert kept == [good]
    assert dropped == [bad], "丢弃的命令必须返回，否则幻觉信号被静默吞掉"


def test_filter_keeps_command_without_target():
    """`CLEAR_VIEW` 不指向任何节点，不该被 evidence 检查误伤。"""
    clear = _cmd(UiOp.CLEAR_VIEW, None)

    kept, dropped = filter_ui_commands([clear], set())

    assert kept == [clear]
    assert dropped == []


def test_filter_keeps_everything_when_all_grounded():
    """全部有出处时不做任何删减 —— 防止拦截逻辑误伤正常输出。"""
    cmds = [_cmd(UiOp.FOCUS_NODE, _A), _cmd(UiOp.SHOW_PATH, _B)]

    kept, dropped = filter_ui_commands(cmds, {_A, _B})

    assert kept == cmds
    assert dropped == []


def test_filter_with_empty_evidence_drops_all_targeted_commands():
    """工具没给 evidence（如 overview）时，任何带 target 的命令都无出处 → 全丢。"""
    cmds = [_cmd(UiOp.FOCUS_NODE, _A), _cmd(UiOp.CLEAR_VIEW, None)]

    kept, dropped = filter_ui_commands(cmds, set())

    assert kept == [_cmd(UiOp.CLEAR_VIEW, None)]
    assert dropped == [_cmd(UiOp.FOCUS_NODE, _A)]


# ==================== split_citations ====================


def test_split_citations_separates_ungrounded():
    grounded = Citation(uid=_A, kind=NodeRefType.CLASS)
    ungrounded = Citation(uid=_GHOST, kind=NodeRefType.CLASS)

    ok, bad = split_citations([grounded, ungrounded], {_A})

    assert ok == [grounded]
    assert bad == [ungrounded]


def test_split_citations_all_grounded_is_noop():
    cits = [Citation(uid=_A, kind=NodeRefType.CLASS), Citation(uid=_B, kind=NodeRefType.CLASS)]

    ok, bad = split_citations(cits, {_A, _B})

    assert ok == cits
    assert bad == []
