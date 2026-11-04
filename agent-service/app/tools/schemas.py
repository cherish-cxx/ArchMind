"""五个数据工具的 input_schema 契约（R6 第一阶段）。

**这里只有数据，没有执行**：schema 现在是给模型和校验层看的唯一参数定义，
不参与 `registry.plan()` 的分发，也不改变任何工具行为。

参数名沿用 Java 内部接口的 camelCase（`maxDepth` / `methodUid` …），
这样后续执行层拿到 `args` 后不必再做一次命名翻译。

三条需要写进字段描述、否则模型一定会用错的语义：

1. `uid` 是**类**的全限定名；方法级操作要用 `methodUid`。
2. `class-relations.direction` 有 `BOTH`，`call-chain.direction` 没有。
3. `limit` 在 `classes` 是“返回多少个类”，在 `class-relations` 是
   “每个关系列表各自截多少”——同一个名字，两种作用域。
"""

from typing import Any

#: 按工具名索引。`registry.ToolSpec.input_schema` 直接引用这里的对象。
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "overview": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "classes": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 40,
                "description": "返回的类数量上限。超出的部分用 truncated 标记。",
            },
        },
        "additionalProperties": False,
    },
    "class-relations": {
        "type": "object",
        "properties": {
            "uid": {
                "type": "string",
                "description": "类的全限定名（图里的 uid）。方法级操作请改用 methodUid。",
            },
            "direction": {
                "type": "string",
                "enum": ["UP", "DOWN", "BOTH"],
                "default": "BOTH",
                "description": "UP 看谁指向它，DOWN 看它指向谁，BOTH 上下都看。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 40,
                "description": "每个关系列表（callers/dependents/subclasses/callees/dependencies/parents）各自的上限，不是总数。",
            },
        },
        "required": ["uid"],
        "additionalProperties": False,
    },
    "call-chain": {
        "type": "object",
        "properties": {
            "uid": {
                "type": "string",
                "description": "起点的类的全限定名。",
            },
            "direction": {
                "type": "string",
                "enum": ["DOWN", "UP"],
                "default": "DOWN",
                "description": "DOWN 多层往下追（主场景），UP 只反查一层。注意没有 BOTH。",
            },
            "maxDepth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "default": 2,
                "description": "往下追几层。默认 2 正好覆盖“接口 → 实现 → 它调了谁”。",
            },
            "maxNodes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 40,
                "description": "单次返回的节点预算，超出时 truncated=true。",
            },
            "methodUid": {
                "type": "string",
                "description": "可选。锁定一个方法（owner#signature）时只追它的上下游，而不是整个类。",
            },
        },
        "required": ["uid"],
        "additionalProperties": False,
    },
    "method-body": {
        "type": "object",
        "properties": {
            "uid": {
                "type": "string",
                "description": "类的全限定名；与 methodUid 二选一。只给类时按 public 优先挑最多 maxMethods 个方法。",
            },
            "methodUid": {
                "type": "string",
                "description": "方法 uid；与 uid 二选一。给接口方法 uid 时会自动落到实现类，并在 resolvedFrom 标明原 uid。",
            },
            "maxMethods": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 3,
                "description": "只给 uid 时最多切几个方法。",
            },
            "maxLines": {
                "type": "integer",
                "minimum": 1,
                "maximum": 300,
                "default": 120,
                "description": "每个方法的行数预算。方法锁定时可放宽到 300。",
            },
        },
        "anyOf": [{"required": ["uid"]}, {"required": ["methodUid"]}],
        "additionalProperties": False,
    },
}
