"""工具参数的校验与归一化（R6）。

**纯函数**：只依赖 `TOOL_SCHEMAS`，不碰网络、日志、上下文和预算。
当前没有调用方 —— legacy 分发传的本来就是合法参数；等 `agent_loop`
落地后由统一执行入口调用，所以这里不改变任何现有行为。

归一化和拒绝的边界：

- 能**无歧义纠正**的就纠正并记一条 note：默认值、数字字符串、大小写枚举、
  越界数值、`owner#signature` 收敛成 owner；
- **语义有歧义**的就拒绝：未知字段、类型不对、methodUid 不属于 uid、
  必填缺失。静默纠正最危险。
"""

from dataclasses import dataclass
from typing import Any, Mapping

from app.tools.errors import ToolError
from app.tools.registry import owner_of
from app.tools.schemas import TOOL_SCHEMAS

#: 单个字符串参数的通用上限。uid 是方法 uid 时可能带签名，留得比类 uid 宽。
_STRING_MAX = 512


@dataclass(frozen=True)
class NormalizedArgs:
    """归一化结果。`notes` 记录被纠正过的地方，供观测和调试。"""

    args: dict[str, Any]
    notes: tuple[str, ...] = ()


def normalize_args(
    tool: str, raw: Mapping[str, Any] | None = None
) -> NormalizedArgs:
    """按 schema 校验并归一化一次工具调用的参数。

    `raw` 为 None 等价于空对象（`overview` 就属于这种）。
    失败抛 `ToolError`，成功返回 `NormalizedArgs`。
    """
    schema = TOOL_SCHEMAS.get(tool)
    if schema is None:
        raise ToolError.unknown_tool(tool, sorted(TOOL_SCHEMAS))

    raw = dict(raw or {})
    notes: list[str] = []

    allowed = set(schema.get("properties") or {})
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ToolError.invalid_arguments(
            field=unknown[0],
            hint=f"{tool} 不接受参数 {unknown}；可用参数：{sorted(allowed) or '无'}",
        )

    args: dict[str, Any] = {}
    for key, prop in (schema.get("properties") or {}).items():
        if key in raw:
            args[key] = _normalize_value(tool, key, raw[key], prop, notes)
        elif "default" in prop:
            args[key] = prop["default"]

    _check_required(tool, schema, args)
    _normalize_uids(tool, args, notes)
    return NormalizedArgs(args=args, notes=tuple(notes))


def _check_required(tool: str, schema: dict[str, Any], args: dict[str, Any]) -> None:
    for key in schema.get("required") or ():
        if key not in args:
            raise ToolError.invalid_arguments(
                field=key, hint=f"{tool} 缺少必填参数 {key}"
            )

    # method-body 的 anyOf：uid / methodUid 至少一个
    if tool == "method-body" and not ({"uid", "methodUid"} & set(args)):
        raise ToolError.invalid_arguments(
            field="uid", hint="method-body 需要 uid 或 methodUid 至少一个"
        )


def _normalize_value(
    tool: str,
    key: str,
    value: Any,
    prop: dict[str, Any],
    notes: list[str],
) -> Any:
    expected = prop.get("type")
    if expected == "integer":
        return _as_int(key, value, prop, notes)
    if expected == "string":
        return _as_str(tool, key, value, prop, notes)
    # schema 目前只有这两种类型；新增类型必须在这里显式实现，不能默默放过
    raise ToolError.invalid_arguments(field=key, hint=f"{key} 的类型 {expected} 未支持")


def _as_int(
    key: str, value: Any, prop: dict[str, Any], notes: list[str]
) -> int:
    # bool 是 int 子类，必须先排掉，否则 True 会被当成 1
    if isinstance(value, bool):
        raise ToolError.invalid_arguments(
            field=key, hint=f"{key} 必须是整数，不能是布尔值"
        )
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
        notes.append(f"{key}: 浮点转整数")
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            raise ToolError.invalid_arguments(
                field=key, hint=f"{key} 必须是整数，收到 {value!r}"
            ) from None
        notes.append(f"{key}: 字符串转整数")
    else:
        raise ToolError.invalid_arguments(
            field=key, hint=f"{key} 必须是整数，收到 {type(value).__name__}"
        )

    low, high = prop.get("minimum"), prop.get("maximum")
    if low is not None and number < low:
        notes.append(f"{key}: 低于下限 {low}，已截断")
        number = low
    if high is not None and number > high:
        notes.append(f"{key}: 高于上限 {high}，已截断")
        number = high
    return number


def _as_str(
    tool: str, key: str, value: Any, prop: dict[str, Any], notes: list[str]
) -> str:
    if not isinstance(value, str):
        raise ToolError.invalid_arguments(
            field=key, hint=f"{key} 必须是字符串，收到 {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise ToolError.invalid_arguments(field=key, hint=f"{key} 不能为空")
    if len(text) > _STRING_MAX:
        raise ToolError.invalid_arguments(
            field=key, hint=f"{key} 超过 {_STRING_MAX} 字符"
        )

    enum = prop.get("enum")
    if enum:
        canonical = text.upper()
        if canonical not in enum:
            raise ToolError.invalid_arguments(
                field=key, hint=f"{key} 只能是 {'/'.join(enum)}，收到 {text!r}"
            )
        if canonical != text:
            notes.append(f"{key}: 大小写已归一")
        return canonical

    return text


def _normalize_uids(tool: str, args: dict[str, Any], notes: list[str]) -> None:
    """类级工具只接受类 uid；方法 uid 收敛成 owner 并记 note。"""
    if "uid" in args and tool in {"class-relations", "call-chain", "method-body"}:
        owner = owner_of(args["uid"])
        if owner != args["uid"]:
            notes.append("uid: 方法 uid 已收敛到所属类")
            args["uid"] = owner

    method_uid = args.get("methodUid")
    if method_uid is None:
        return

    if "#" not in method_uid or "(" not in method_uid:
        # 模型很容易照着"类名 + 方法名"拼一个 `owner#resolveSourceDir` ——
        # 少了参数列表 Java 就找不到（实测会白白打一次 Java 再失败）。
        # 在这里拦住，提示它改传类 uid，让工具自己挑方法。
        raise ToolError.invalid_arguments(
            field="methodUid",
            hint=(
                "methodUid 必须形如 owner#方法名(参数类型)，只有方法名不行；"
                "不确定签名时改传 uid（类名），由工具自己挑方法"
            ),
        )
    if tool == "call-chain" and owner_of(method_uid) != args.get("uid"):
        raise ToolError.invalid_arguments(
            field="methodUid",
            hint="methodUid 不属于 uid 指定的类；两者必须一致",
        )
