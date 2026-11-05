"""SSE 帧编码 —— 只有格式，没有传输。

传输（`StreamingResponse`）在路由层；这里保证**每一帧都是合法的单行 data**。
`json.dumps` 会把字符串里的换行转义成 `\\n`，所以拼出来的帧一定只有三个换行：
事件行、数据行、结束空行各一个。
"""

import json
from typing import Any

#: 事件名。前端按这四个名字分支，多一个都会落进「未知事件」。
START = "start"
STEP = "step"
ANSWER = "answer"
ERROR = "error"


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """把 `(事件名, JSON 对象)` 编成一帧。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
