"""PAT 的 Java 内部接口客户端。

只有两个问题要在这层解决：
  ① Java 用 HTTP 状态码表达鉴权结论（401/403/404），这里把它映射成 `McpAuthError`；
  ② 响应是 camelCase，这里转成领域对象，工具层不碰 JSON key。

**token 永远不出现在异常消息与日志里**：错误消息是固定文案，
请求失败只记录状态码/路径，不记录 header 与 body。
"""

import hashlib
import time
from datetime import datetime
from typing import Any

import httpx

from app.config import INTERNAL_TOKEN_HEADER, settings as agent_settings
from app.mcp.config import mcp_settings
from app.mcp.principal import McpAuthError, Principal, ProjectContext

_AUTH_PATH = "/internal/mcp/authenticate"
_CONTEXT_PATH = "/internal/mcp/context"
_PROJECTS_PATH = "/internal/mcp/projects"
_TIMEOUT_SECONDS = 8.0


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class McpContextClient:
    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or agent_settings.java_base_url,
            timeout=_TIMEOUT_SECONDS,
            headers={
                INTERNAL_TOKEN_HEADER: internal_token or agent_settings.internal_token
            },
            transport=transport,
            # 服务间固定地址，不走桌面代理（与 java_client 同一理由）
            trust_env=False,
        )
        self._cache: dict[tuple[str, ...], tuple[float, Any]] = {}
        self._cache_seconds = mcp_settings.context_cache_seconds

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── 三个内部接口 ────────────────────────────────────────────────────────

    async def authenticate(self, token: str) -> Principal:
        """PAT → Principal。401 以外的失败也不回显响应体。"""
        key = ("auth", _digest(token))
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        resp = await self._post(_AUTH_PATH, {"token": token})
        if resp.status_code == 401:
            raise McpAuthError(401, "无效或过期的 token")
        if resp.status_code != 200:
            raise McpAuthError(resp.status_code, "PAT 鉴权失败")

        data = resp.json()
        principal = Principal(
            pat_id=int(data["patId"]),
            user_id=int(data["userId"]),
            bound_project_id=(
                int(data["boundProjectId"])
                if data.get("boundProjectId") is not None
                else None
            ),
            expires_at=_parse_datetime(data.get("expiresAt")),
            raw_token=token,
        )
        self._cache_put(key, principal)
        return principal

    async def context(self, principal: Principal, project_id: int) -> ProjectContext:
        """PAT + projectId → 项目上下文。归属校验在 Java，403/404 原样上抛。"""
        key = ("context", _digest(principal.raw_token), str(project_id))
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        resp = await self._post(
            _CONTEXT_PATH,
            {"token": principal.raw_token, "projectId": project_id},
        )
        if resp.status_code == 401:
            raise McpAuthError(401, "无效或过期的 token")
        if resp.status_code == 403:
            raise McpAuthError(403, "无权访问该项目")
        if resp.status_code == 404:
            raise McpAuthError(404, "项目不存在")
        if resp.status_code != 200:
            raise McpAuthError(resp.status_code, "项目上下文获取失败")

        data = resp.json()
        ctx = ProjectContext(
            user_id=int(data["userId"]),
            pat_id=int(data["patId"]),
            project_id=int(data["projectId"]),
            project_name=str(data.get("projectName") or ""),
            analysis_status=str(data.get("analysisStatus") or ""),
            graph_rev=int(data.get("graphRev") or 0),
        )
        self._cache_put(key, ctx)
        return ctx

    async def projects(self, user_id: int) -> list[dict]:
        """当前用户的项目列表。Java 返回 `Result<List<...>>`，取 data。"""
        try:
            resp = await self._client.get(_PROJECTS_PATH, params={"userId": user_id})
        except httpx.HTTPError as e:
            raise McpAuthError(503, "项目列表服务暂不可用") from e
        if resp.status_code != 200:
            raise McpAuthError(resp.status_code, "项目列表获取失败")
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        return list(data or [])

    # ── HTTP 出口 ───────────────────────────────────────────────────────────

    async def _post(self, path: str, body: dict) -> httpx.Response:
        try:
            return await self._client.post(path, json=body)
        except httpx.HTTPError as e:
            # 不把 e 的字符串拼进消息：httpx 的异常文本可能带请求细节。
            raise McpAuthError(503, "鉴权服务暂不可用") from e

    # ── 缓存 ────────────────────────────────────────────────────────────────

    def _cache_get(self, key: tuple[str, ...]) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(key, None)
            return None
        return value

    def _cache_put(self, key: tuple[str, ...], value: Any) -> None:
        if self._cache_seconds <= 0:
            return
        self._cache[key] = (time.monotonic() + self._cache_seconds, value)
