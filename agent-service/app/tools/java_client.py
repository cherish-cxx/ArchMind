"""Java `/internal/tools/*` 的客户端 —— Python 侧**唯一会讲 HTTP 的地方**。

边界只此一处：这之上全是 Python 对象和 snake_case，这之下全是 camelCase 的 JSON。

三条硬约束（**违反任何一条都静默失效，不报错**）：
  ① `args` 的 key 必须 camelCase —— Java 用 `strArg`/`intArg` 取值，找不到就吃默认值；
     Python 这边顺手写 `method_uid` 会得到 HTTP 200 + 参数被忽略。
  ② 数字必须是 JSON number —— `intArg` 判的是 `raw instanceof Number`，
     发 `"maxDepth": "2"` 同样落回默认值。
  ③ 超时必须显式设 —— 级联超时 C1 的第一道闸，不依赖 Java 侧默认值。

关于预算：设计文档写「maxNodes 硬上限 40」是**错的**。40 是控制器的默认值，
真正的硬上限在 `CallChainServiceImpl.MAX_NODES_HARD = 200`。
也就是说 **Java 不会替 Agent 拦到 40** —— 想让预算生效，只能这边显式传。
"""

import logging
from typing import Any, TypeVar

import httpx

from app.config import INTERNAL_TOKEN_HEADER, settings
from app.models.tool import (
    CallChain,
    ChainDirection,
    ClassNode,
    ClassPage,
    ClassRelations,
    DescribeStatus,
    MethodBody,
    ProjectOverview,
    RelationDirection,
    ToolEnvelope,
)

logger = logging.getLogger(__name__)

# ── 常量：与 Java `InternalToolController` 的默认值对齐 ──────────────────────
# 这些是 **Agent 自己的预算**，不是 Java 的硬上限。必须显式传下去才生效。
DEFAULT_LIMIT = 40          # classes / class-relations 的单次返回上限
DEFAULT_CHAIN_DEPTH = 2     # 往下 2 层：正好覆盖「接口 → 实现 → 它调了谁」
DEFAULT_MAX_NODES = 40      # 调用链单次节点预算
DEFAULT_MAX_METHODS = 3
DEFAULT_MAX_LINES = 120
#: 索引分页：Java 侧硬上限 200，和它对齐
DEFAULT_PAGE_SIZE = 200
#: 方法级锁定（methodUid）只切 1 个方法，行数放宽到 Java 侧硬上限 300 ——
#: 120 行很容易把带事务的长方法切成半截。
DEFAULT_METHOD_MAX_LINES = 300

# 工具级超时（设计 §8）
TOOL_TIMEOUT_SECONDS = 8.0

_PREFIX = "/internal/tools"
_PATH_OVERVIEW = f"{_PREFIX}/overview"
_PATH_CLASSES = f"{_PREFIX}/classes"
_PATH_CLASS_RELATIONS = f"{_PREFIX}/class-relations"
_PATH_CALL_CHAIN = f"{_PREFIX}/call-chain"
_PATH_METHOD_BODY = f"{_PREFIX}/method-body"
_PATH_CLASSES_PAGE = f"{_PREFIX}/classes/page"
_PATH_DESCRIBE_STATUS = "/internal/describe/status"


class JavaClientError(Exception):
    """java_client 抛出的所有异常的基类。"""


class InternalAuthError(JavaClientError):
    """401 —— 两边 `INTERNAL_TOKEN` 对不上。

    这是**配置错误**不是业务结果，必须炸出来。要在 P3 的循环里**先于**降级逻辑处理，
    否则一条配置错误会被当成「工具暂时答不了」而静默降级。
    """


class ToolTransportError(JavaClientError):
    """没能拿到一个合法信封：连不上 / 超时 / 非 200 非 401。

    P3 由循环接住，转成降级答复（C2）。
    """


TModel = TypeVar("TModel")


class JavaClient:
    """Java 内部工具接口的客户端。

    **实例持有连接池**（`httpx.AsyncClient`），由 `main.py` 的 lifespan 建一次、
    整个服务共用，关停时 `aclose()`。

    为什么必须共享：每次调用新建 client 会丢掉连接复用，更关键的是**丢掉连接池自带的
    并发上限** —— P3 的循环一轮可能并发发好几个工具调用，没有闸门就可能一下子打爆 Java。
    """

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        # base_url / token 允许覆盖，只是为了让契约测试能指向别处
        self._client = httpx.AsyncClient(
            base_url=base_url or settings.java_base_url,
            timeout=TOOL_TIMEOUT_SECONDS,
            headers={INTERNAL_TOKEN_HEADER: token or settings.internal_token},
            # ★ 必须关掉 env 代理。Windows 上 httpx 会从**注册表**读系统代理
            # （本机是 Clash 挂在 127.0.0.1:7897），trust_env 一旦为 True，
            # 连 http://localhost:8080 都会被丢给代理 —— 表现为超时 / 502，
            # 而同一台机器上 curl 可能照常，极难排查。
            # 服务间调用打的是固定地址（本地 localhost / 容器 backend:8080），
            # 本来就不该走用户的桌面代理。
            trust_env=False,
        )

    async def aclose(self) -> None:
        """由 lifespan 在服务关停时调用。忘了调会报 ResourceWarning。"""
        await self._client.aclose()

    # ── 五个工具 ────────────────────────────────────────────────────────────

    async def overview(self, project_id: int) -> ToolEnvelope[ProjectOverview]:
        """项目概况。数据源是 `project_overview` 表，**不碰图** —— 未落图的项目也能答。"""
        return await self._post(_PATH_OVERVIEW, project_id, {}, ToolEnvelope[ProjectOverview])

    async def classes(
        self, project_id: int, limit: int = DEFAULT_LIMIT
    ) -> ToolEnvelope[list[ClassNode]]:
        """项目里有哪些类。兜底用：用户没给锚点时，让 LLM 在清单里选而不是硬猜。"""
        return await self._post(
            _PATH_CLASSES, project_id, {"limit": limit}, ToolEnvelope[list[ClassNode]]
        )

    async def class_relations(
        self,
        project_id: int,
        uid: str,
        direction: RelationDirection = RelationDirection.BOTH,
        limit: int = DEFAULT_LIMIT,
    ) -> ToolEnvelope[ClassRelations]:
        """某个类的上下游关系（用户点一下类看关联）。

        ⚠️ 这里的 direction 是 `RelationDirection`（UP/DOWN/**BOTH**），
        和 `call_chain` 的不是同一个枚举，**别混用**。
        """
        return await self._post(
            _PATH_CLASS_RELATIONS,
            project_id,
            {"uid": uid, "direction": direction, "limit": limit},
            ToolEnvelope[ClassRelations],
        )

    async def call_chain(
        self,
        project_id: int,
        uid: str,
        direction: ChainDirection = ChainDirection.DOWN,
        method_uid: str | None = None,
        max_depth: int = DEFAULT_CHAIN_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
    ) -> ToolEnvelope[CallChain]:
        """调用链。Agent 的核心场景。

        ⚠️ direction 只有 `DOWN` / `UP`，**没有 BOTH**（DOWN 是多层 BFS、UP 是单层反查）。
        传别的值 Java 会返回 `ok:false`。

        `method_uid` 可选：不传就从整个类出发，传了就只看这一个方法的下游。
        """
        args: dict[str, Any] = {
            "uid": uid,
            # 预算显式传 —— 约束 ③。不传的话 Java 用它的默认值，Agent 的预算形同虚设
            "maxDepth": max_depth,
            "maxNodes": max_nodes,
            "direction": direction,
        }
        if method_uid is not None:
            args["methodUid"] = method_uid
        return await self._post(_PATH_CALL_CHAIN, project_id, args, ToolEnvelope[CallChain])

    async def method_body(
        self,
        project_id: int,
        uid: str | None = None,
        method_uid: str | None = None,
        max_methods: int = DEFAULT_MAX_METHODS,
        max_lines: int = DEFAULT_MAX_LINES,
    ) -> ToolEnvelope[MethodBody]:
        """方法源码切片。「讲解深度」的唯一来源。

        `uid`（类）和 `method_uid`（方法）**二选一**。给的是接口方法 uid 时，
        Java 会顺 `IMPLEMENTS + signature` 自动跳到实现类，并在 `resolvedFrom` 里标明原 uid。
        """
        args: dict[str, Any] = {"maxMethods": max_methods, "maxLines": max_lines}
        if uid is not None:
            args["uid"] = uid
        if method_uid is not None:
            args["methodUid"] = method_uid
        return await self._post(_PATH_METHOD_BODY, project_id, args, ToolEnvelope[MethodBody])

    # ── R4：索引所需的两类读接口 ────────────────────────────────────────────

    async def classes_page(
        self, project_id: int, after_uid: str | None = None, limit: int = DEFAULT_PAGE_SIZE
    ) -> ToolEnvelope[ClassPage]:
        """按 uid 游标拉一页类。`after_uid=None` 是第一页；`next_cursor=None` 表示拉完了。"""
        params: dict[str, Any] = {"projectId": project_id, "limit": limit}
        if after_uid:
            params["afterUid"] = after_uid
        resp = await self._send("GET", _PATH_CLASSES_PAGE, params=params)
        return ToolEnvelope[ClassPage].model_validate(resp.json())

    async def describe_status(self, project_id: int) -> DescribeStatus:
        """描述覆盖情况。只有 `pending == 0` 且分析完成才允许建索引。"""
        resp = await self._send("GET", _PATH_DESCRIBE_STATUS, params={"projectId": project_id})
        payload = resp.json()
        if payload.get("code") != 0 or payload.get("data") is None:
            raise JavaClientError(f"describe/status 失败: {payload.get('message')}")
        return DescribeStatus.model_validate(payload["data"])

    # ── 唯一的 HTTP 出口 ────────────────────────────────────────────────────
    # 这里是Java client的唯一HTTP出口，所有工具方法全部复用这个发送方法完成网络请求
    async def _post(
        self,
        path: str,
        project_id: int,
        args: dict[str, Any],
        envelope: type[ToolEnvelope[TModel]],
    ) -> ToolEnvelope[TModel]:
        """
        发一个请求、收一个信封。所有 HTTP 细节只在这里出现。

        **不能只看 HTTP status**：Java 侧所有「工具答不了」都是 200 + `ok:false`
        （`ToolEnvelope` 的明确设计，避免把业务结果变成 500）。
        """
        # projectId 由 Java 注入，不是用户身份 —— 内部接口不收 JWT，租户边界靠这个字段守
        body = {"projectId": project_id, "args": args}
        resp = await self._send("POST", path, json=body)
        return envelope.model_validate(resp.json())

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """所有内部接口共用的发送与错误分类。"""
        try:
            resp = await self._client.request(method, path, json=json, params=params)
        except httpx.TransportError as e:
            # 连不上 / 超时 / 连接被重置 —— 都没拿到信封
            logger.warning("工具调用失败 %s %s: %s", method, path, e)
            raise ToolTransportError(f"{path} 调用失败: {e}") from e
        # 401 说明两边共享密钥对不上，这是配置错误，必须炸出来
        if resp.status_code == 401:
            logger.error("内部接口鉴权失败 path=%s —— 查两边 INTERNAL_TOKEN 是否一致", path)
            raise InternalAuthError(
                "Java 侧拒绝鉴权（401）：INTERNAL_TOKEN 与 application.yml 不一致"
            )
        if resp.status_code != 200:
            logger.warning("工具调用返回 HTTP %s path=%s", resp.status_code, path)
            raise ToolTransportError(
                f"{path} 返回 HTTP {resp.status_code}: {resp.text[:200]}"
            )
        # ★ 200 + ok:false 也走正常路径 —— 那是业务结果，交给调用方按 ok 分支
        return resp
