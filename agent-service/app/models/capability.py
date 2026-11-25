"""能力卡片契约 —— `/internal/tools/capabilities/cards` 的 Python 侧形状。

一个「能力」= 一个可由用户业务语言触达的入口（如 `POST /orders`、
`SCHEDULED a.Job#run()`）。字段与 Java `CapabilityCardDTO` /
`CapabilityMemberDTO` / `CapabilityCardPageResponse` 一一对应。

两个容易踩的点：
  - `uid` 是**不透明字符串**，可能含空格、`/`、`#`；这里只读不拆。
  - Java 没配 Jackson 的 NON_NULL，可为空的字段会发成显式 `null`，
    所以类型必须带 `| None`（默认值 `= None` 救不了显式 null）。
"""

from app.models.base import CamelModel


class CapabilityMember(CamelModel):
    """能力入口可达的一个成员类（按 depth 由近到远）。"""

    uid: str
    name: str
    role: str | None = None
    depth: int = 0


class CapabilityCard(CamelModel):
    """一张能力卡片：所属领域 + 入口 + 成员类摘要。"""

    uid: str
    name: str
    description: str | None = None
    area_uid: str
    area_name: str
    area_description: str | None = None
    entry_uid: str
    entry_kind: str | None = None
    member_count: int = 0
    members: list[CapabilityMember] = []
    members_truncated: bool = False


class CapabilityCardPage(CamelModel):
    """按 uid 游标分页的能力卡片（`/internal/tools/capabilities/cards`）。"""

    rev: int
    items: list[CapabilityCard] = []
    next_cursor: str | None = None
