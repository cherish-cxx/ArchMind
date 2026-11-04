"""DeepSeek 客户端 —— Python 侧**唯一会调 LLM 的地方**。

和 `tools/java_client.py` 同构：这之上是 prompt 与业务逻辑，这之下才是 HTTP。
好处也一样 —— 换模型、加重试、埋 token 计量都只动这一个文件。

**为什么用 httpx 裸 POST 而不是 `openai` SDK**：DeepSeek 就是 OpenAI 兼容的一个
`/chat/completions`，多引一个 SDK 换来的只是几个便利函数，却多一层版本耦合。
项目整体的调子也是「自研循环、不引框架」（见 9月19 §5）。

**关于代理**：`trust_env=False` 是有意设的。本机 7897 是注册表里的系统代理，
实测直连 `api.deepseek.com` 是通的（2026-10-01 验证过 trust_env 两种取值都 200），
所以这里关掉环境代理 —— 让外网调用不经过一个它并不需要的中间人，行为可预测。

**R6 第二步：function calling**。`complete()` 多了 `tools` / `tool_choice`，
`LLMResult` 从「一段文本」变成「一条 assistant 消息」—— 模型可以不说话、改成
返回 `tool_calls`。老的 `text` 保留成属性，`intent` / `explain` 两条链路一行不用改。

请求体组装（`completion_payload`）与响应解析（`parse_completion`）都拆成纯函数，
理由和 `parse_intent` 一样：**不调网络就能测**。这两处恰恰是最会静默写错的地方 ——
把 `tools` 写成 `tool`，接口不报错，只是模型永远不调工具。
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import settings
from app.models.context import ToolCall

logger = logging.getLogger(__name__)

_CHAT_PATH = "/chat/completions"


class LLMError(Exception):
    """LLM 客户端抛出的所有异常的基类。"""


class LLMNotConfiguredError(LLMError):
    """没配 `DEEPSEEK_API_KEY`。

    **必须和「LLM 暂时不可用」区分开** —— 前者是配置错误（该 500、该炸出来），
    后者是外部依赖抖动（该降级成模板讲解，C2）。混在一起会让配置问题被静默吞掉，
    这正是 P2 在 java_client 里为 401 特意做的区分，这里保持一致。
    """


class LLMTransportError(LLMError):
    """连不上 / 超时。循环接住它 → 讲解降级（设计 §8-C2）。"""


class LLMResponseError(LLMError):
    """拿到 HTTP 响应但形状不对（非 200 / 没有 choices / content 不是字符串）。"""


@dataclass(frozen=True)
class LLMResult:
    """一次 LLM 调用的结果。

    `content` 与 `tool_calls` **可能只有其一**：普通回复只有 content；
    调工具时 content 通常是 null（DeepSeek 与 OpenAI 同规）。

    `text` 保留成属性而不是字段 —— `intent` / `explain` 只读 `raw.text`，
    改名会把它们一起打穿，而它们跟新循环没有关系。

    `tokens_in/out` 现在只是记在日志里 —— 但设计 §8-C4 要求「埋四个数」，
    计量从第一次调用就带上，比事后补便宜得多。
    """

    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def text(self) -> str:
        return self.content or ""

    def to_message(self) -> dict[str, Any]:
        """还原成 OpenAI 的 assistant 消息，供循环原样塞回 `messages`。

        **放在这里而不是循环里**：它是 `parse_completion` 的逆运算，
        两处分开写必然漂移（少一个 `type`、多个字段），而 400 要到下一轮才炸。
        """
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


def completion_payload(
    messages: list[dict[str, Any]],
    *,
    json_mode: bool = False,
    temperature: float = 0.0,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> dict[str, object]:
    """组装 `/chat/completions` 的请求体。纯函数，参数错在这里就报，不用等一次 400。

    `tool_choice` 不给时显式填 `"auto"`：OpenAI 的默认值本来就是这个，
    但写出来之后「模型可以自己选工具」在代码里是**可见的**，而不是一个隐含默认。

    ⚠️ `json_mode` 与 `tools` 同时为真时**不报错但也不该这么用**：结构已经由
    `answer` 工具的 schema 保证，再逼中间步骤（`search` / `call-chain`）输出 JSON
    只会让它们没法正常回话。约束落在调用方。
    """
    if tool_choice is not None and not tools:
        raise ValueError("tool_choice 需要同时提供 tools —— 没有工具时它无意义")

    payload: dict[str, object] = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def parse_completion(body: dict[str, Any]) -> LLMResult:
    """HTTP 响应体 → `LLMResult`。纯函数，单独拆出来是为了不调网络也能测。

    形状不对一律抛 `LLMResponseError`，**不做猜补**：缺 id 就把这条调用丢掉，
    等于让模型的一次调用凭空消失 —— 那比报错难查得多。`arguments` 是
    dict 时也拒绝而不是替它 `json.dumps`，与 `tools/args.py` 同一条原则：
    语义有歧义的就拒绝。
    """
    try:
        message = body["choices"][0]["message"]
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise TypeError(f"content 不是字符串：{type(content).__name__}")
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
        usage = body.get("usage") or {}
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMResponseError(f"响应形状不对：{type(e).__name__}: {e}") from e

    if not content and not tool_calls:
        raise LLMResponseError("响应里既没有 content 也没有 tool_calls")

    return LLMResult(
        content=content,
        tool_calls=tool_calls,
        tokens_in=int(usage.get("prompt_tokens", 0)),
        tokens_out=int(usage.get("completion_tokens", 0)),
    )


def _parse_tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TypeError(f"tool_calls 不是数组：{type(raw).__name__}")

    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError(f"tool_calls 里有非对象元素：{type(item).__name__}")
        function = item.get("function")
        if not isinstance(function, dict):
            raise TypeError("tool_call 缺少 function 对象")
        call_id = item.get("id")
        name = function.get("name")
        # 空串是合法的：overview 这类无参函数就该传 ""。
        arguments = function.get("arguments", "")
        if not isinstance(call_id, str) or not call_id:
            raise TypeError("tool_call 缺少 id")
        if not isinstance(name, str) or not name:
            raise TypeError("tool_call 缺少 name")
        if not isinstance(arguments, str):
            raise TypeError(f"arguments 不是字符串：{type(arguments).__name__}")
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return tuple(calls)


"""
这个类只是写接口契约，不写业务代码
真正调用大模型网络的代码，在LLMClient类里，Completer只是永安里做类型约束
"""
class Completer(Protocol):
    """`classify_intent` / `explain` 真正依赖的**最小接口**。

    这就是依赖注入的接缝：测试塞一个只实现 `complete` 的假对象进去，
    整条判定/讲解链路就能脱离网络跑。所以上层一律按 `Completer` 标注，
    而不是 `LLMClient` —— 标注成具体类会让人以为「必须传真的客户端」。
    """

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResult: ...


class LLMClient:
    """共享一个 `httpx.AsyncClient` —— 和 `JavaClient` 同样的理由。

    连接复用与连接池上限都是**实例级**的，每次调用新建就等于两条都放弃。
    """

    def __init__(self) -> None:
        headers = {"Content-Type": "application/json"}
        if settings.deepseek_api_key:
            headers["Authorization"] = f"Bearer {settings.deepseek_api_key}"

        self._client = httpx.AsyncClient(
            base_url=settings.deepseek_base_url,
            timeout=settings.llm_timeout,
            headers=headers,
            trust_env=False,
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResult:
        """跑一次补全。

        `temperature=0.0` 是默认：工具规划要的是**可复现**，不是创造力。
        讲解环节想要一点措辞变化的话，调用方显式传。

        `tools` 给了就是 function calling：模型可以返回 `tool_calls` 而不说话，
        `content` 这时是 null。`tool_choice` 传 `{"type":"function","function":
        {"name":"answer"}}` 可以**逼**它调某个工具 —— 预算耗尽时强制收尾就靠这个。

        ⚠️ `json_mode=True` 时 DeepSeek（与 OpenAI 同规）**要求 prompt 里出现 "json" 字样**，
        否则返回 400。这条约束落在调用方（`core/prompt.py`）身上，不是这里。
        """

        # 校验密钥
        if not settings.deepseek_api_key:
            raise LLMNotConfiguredError(
                "DEEPSEEK_API_KEY 未配置 —— 检查 agent-service/.env 或进程环境变量"
            )

        # 组装请求体payload
        payload = completion_payload(
            messages,
            json_mode=json_mode,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )

        #发送POST请求
        try:
            resp = await self._client.post(_CHAT_PATH, json=payload)
        except httpx.TransportError as e:
            # 超时和连不上合并成一种 —— 对调用方来说处置方式相同（降级），
            # 分开只会逼它写两个 except 做同一件事。
            raise LLMTransportError(f"{type(e).__name__}: {e}") from e

        # Http状态码
        if resp.status_code != 200:
            raise LLMResponseError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        # 解析返回JSON，组装LLMResult对象（形状校验在纯函数里，能单独测）
        try:
            body = resp.json()
        except ValueError as e:
            raise LLMResponseError(f"响应不是 JSON：{e}") from e

        result = parse_completion(body)
        logger.info(
            "LLM %s: in=%d out=%d json=%s tools=%d",
            settings.llm_model, result.tokens_in, result.tokens_out,
            json_mode, len(result.tool_calls),
        )
        return result

    async def aclose(self) -> None:
        await self._client.aclose()
