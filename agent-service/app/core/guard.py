"""幻觉拦截 —— 四道拦截里的 A2。

**为什么这个模块全是纯函数**：这里做的是「LLM 说的东西，图里真有吗」的核对，
本质是集合运算，没有任何 IO。做成纯函数意味着它可以用最少成本测到最细 ——
A2 是整个 P3 里最容易出错、也最容易测的一块。

关于 A4（LLM 用常识补全断链），防线在**别处**，不在这里：
  - 设计 §8-A4 给的防线原文是「返回 `gaps` + prompt 强制要求提及『未解析到』」——
    即 prompt 约束 + 把 gaps 如实传下去，不是事后文本扫描。
  - 曾考虑加一个「扫 message 里的类名、不在 evidence 里就告警」的检查，**放弃了**：
    LLM 会写简单名（`OrderService`）而 evidence 里是全限定名（…`OrderServiceImpl`），
    这类比较必然大量误报。一个天天误报的检查等于没有检查，还会训练人忽略它。
  - 真正结构化、能判准的那个抓手是 `citations` 与 `uiCommands[].target` ——
    它们本来就是结构化数据。所以下面这两条才是 A2/A4 的**机制**，其余是 prompt 的责任。
"""

from collections.abc import Iterable

from app.models.response import Citation, UICmd
from app.models.tool import ToolEnvelope


def evidence_uids(envelopes: Iterable[ToolEnvelope]) -> set[str]:
    """把本轮所有工具信封里的 evidence 碾成 uid 集合。

    evidence 是 Java **从图里捞出来的真实凭据**，不是我们写死的、也不是 LLM 生成的 ——
    所以它是「什么可以被引用」的唯一真相。citations 和 uiCommands 都得从这里取。
    """
    uids: set[str] = set()
    for env in envelopes:
        for e in env.evidence:
            uids.add(e.from_)
            if e.to:
                uids.add(e.to)
    return uids


def filter_ui_commands(
    commands: Iterable[UICmd], evidenced: set[str]
) -> tuple[list[UICmd], list[UICmd]]:
    """★ A2 的拦截：丢掉指向不存在节点的 UICommand。

    返回 `(保留的, 丢弃的)` 而不是只返回保留的 —— 被丢了什么**必须能被记下来**。
    静默丢弃等于把「LLM 开始幻觉了」这个信号埋掉，下次它幻觉得更离谱时就没有前兆可循。

    没有 `target` 的命令（如 `CLEAR_VIEW`）不受约束 —— 它不指向任何节点，
    不存在「指向幻觉节点」的问题。
    """
    kept: list[UICmd] = []
    dropped: list[UICmd] = []
    for cmd in commands:
        if cmd.target is None or cmd.target.uid in evidenced:
            kept.append(cmd)
        else:
            dropped.append(cmd)
    return kept, dropped


def split_citations(
    citations: Iterable[Citation], evidenced: set[str]
) -> tuple[list[Citation], list[Citation]]:
    """A2 在讲解侧的对应拦截：citations 必须全部有出处。

    同样返回 `(grounded, ungrounded)` 两个列表。

    与 `filter_ui_commands` 的差别在**处置**：UICommand 丢了只是少画一个节点，
    用户无感；citation 丢了意味着**讲解里提到的东西对不上账**，那是更严重的事，
    调用方（`core/loop.py`）应该 error 级记录，而不是像 UICommand 那样 warn 了事。
    """
    grounded: list[Citation] = []
    ungrounded: list[Citation] = []
    for c in citations:
        (grounded if c.uid in evidenced else ungrounded).append(c)
    return grounded, ungrounded
