"""步骤追加与回调 —— 循环和检索/记忆共用。

独立成模块是为了避免循环导入：`turn_support` 被 `agent_loop` 导入，
不能反过来导入循环里的私有函数。
"""

import logging

from app.models.response import Step, StepHook, StepType

logger = logging.getLogger(__name__)


def add_step(
    steps: list[Step],
    on_step: StepHook | None,
    type_: StepType,
    detail: str,
    ms: int,
) -> Step:
    """追加一步，并通知观察者。

    **先入列表、再回调**：回调里就算抛异常，`resp.steps` 也不会少一条 ——
    推流是附加路径，不能改变本轮的结构。
    """
    step = Step(n=len(steps) + 1, type=type_, detail=detail, ms=ms)
    steps.append(step)
    if on_step is not None:
        try:
            on_step(step)
        except Exception:  # noqa: BLE001 —— 观察者坏了不该毁掉这一轮
            logger.warning("on_step 回调失败（步骤 %s）", step.n, exc_info=True)
    return step
