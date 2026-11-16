"""纯 ASGI PAT 中间件。

为什么不用 BaseHTTPMiddleware：后者会包一层任务/流，和 Streamable HTTP
的长连接、SSE 响应容易互相拖累；这里只需要在进入 MCP app 前查一次身份。

职责：
  - 非 http 作用域（尤其是 lifespan）**原样透传**；
  - 从 `Authorization: Bearer <token>` 取 token，缺失/非法 → 401 JSON；
  - 交给 `McpContextClient.authenticate` 换 Principal；
  - 按 pat_id 限流，超限 → 429 JSON；
  - 把 Principal 放进 contextvar，再调用内层 app。

**任何输出都不回显 token**：错误消息是固定文案。
"""

import json
import logging

from app.mcp.principal import McpAuthError, current_principal, principal_scope  # noqa: F401

logger = logging.getLogger(__name__)


def _bearer_token(scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name.lower() != b"authorization":
            continue
        try:
            text = value.decode("latin-1").strip()
        except Exception:  # noqa: BLE001 —— 非法字节按「没有 token」处理
            return None
        scheme, _, token = text.partition(" ")
        if scheme.lower() != "bearer":
            return None
        token = token.strip()
        return token or None
    return None


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _error(status: int, code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


class PatAuthMiddleware:
    def __init__(self, app, context, limiter) -> None:
        self.app = app
        self.context = context
        self.limiter = limiter

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        token = _bearer_token(scope)
        if token is None:
            await _send_json(send, 401, _error(401, "unauthorized", "缺少 Bearer token"))
            return

        try:
            principal = await self.context.authenticate(token)
        except McpAuthError as e:
            code = "unauthorized" if e.status == 401 else "auth_failed"
            await _send_json(send, e.status, _error(e.status, code, e.message))
            return
        except Exception as e:  # noqa: BLE001 —— 不让异常文本（可能含请求细节）外泄
            logger.warning("MCP 鉴权异常: %s", type(e).__name__)
            await _send_json(send, 401, _error(401, "unauthorized", "鉴权失败"))
            return

        try:
            allowed = await self.limiter.allow(principal.pat_id)
        except Exception as e:  # noqa: BLE001 —— 限流故障放行，别把服务打死
            logger.warning("MCP 限流异常，放行: %s", type(e).__name__)
            allowed = True

        if not allowed:
            await _send_json(send, 429, _error(429, "rate_limited", "请求过于频繁"))
            return

        with principal_scope(principal):
            await self.app(scope, receive, send)
