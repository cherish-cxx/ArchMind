"""步骤回调 —— SSE 的推流靠它，所以顺序与异常隔离都要在这里钉死。"""

from app.core.agent_loop import run_agent_turn
from fakes import FakeJava, FakeLLM
from test_agent_loop import ANSWER_ARGS, ctx, reply, tool_call


async def test_on_step_streams_every_step_in_order():
    llm = FakeLLM([
        reply(tool_call("t1", "overview")),
        reply(tool_call("a1", "answer", ANSWER_ARGS)),
    ])
    seen = []

    resp = await run_agent_turn(
        ctx(), "订单流程", FakeJava(), llm, on_step=seen.append
    )

    assert [(s.n, s.type, s.detail) for s in seen] == [
        (s.n, s.type, s.detail) for s in resp.steps
    ]
    assert [s.n for s in seen] == [1, 2, 3]


async def test_on_step_exception_does_not_break_turn():
    def boom(step):
        raise RuntimeError("consumer down")

    llm = FakeLLM([reply(tool_call("a1", "answer", ANSWER_ARGS))])

    resp = await run_agent_turn(ctx(), "订单流程", FakeJava(), llm, on_step=boom)

    assert resp.message
