"""DeepSeek 客户端 —— Python 侧**唯一会调 LLM 的地方**。

和 `tools/java_client.py` 同构：这之上是 prompt 与业务逻辑，这之下才是 HTTP。
好处也一样 —— 换模型、加重试、埋 token 计量都只动这一个文件。

**为什么用 httpx 裸 POST 而不是 `openai` SDK**：DeepSeek 就是 OpenAI 兼容的一个
`/chat/completions`，多引一个 SDK 换来的只是几个便利函数，却多一层版本耦合。
项目整体的调子也是「自研循环、不引框架」（见 9月19 §5）。

**关于代理**：`trust_env=False` 是有意设的。本机 7897 是注册表里的系统代理，
实测直连 `api.deepseek.com` 是通的（2026-10-01 验证过 trust_env 两种取值都 200），
所以这里关掉环境代理 —— 让外网调用不经过一个它并不需要的中间人，行为可预测。
"""

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import settings

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

    `tokens_in/out` 现在只是记在日志里 —— 但设计 §8-C4 要求「埋四个数」，
    计量从第一次调用就带上，比事后补便宜得多。
    """

    text: str
    tokens_in: int
    tokens_out: int


class Completer(Protocol):
    """`classify_intent` / `explain` 真正依赖的**最小接口**。

    这就是依赖注入的接缝：测试塞一个只实现 `complete` 的假对象进去，
    整条判定/讲解链路就能脱离网络跑。所以上层一律按 `Completer` 标注，
    而不是 `LLMClient` —— 标注成具体类会让人以为「必须传真的客户端」。
    """

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
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
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> LLMResult:
        """跑一次补全。

        `temperature=0.0` 是默认：Intent 判定要的是**可复现**，不是创造力。
        讲解环节想要一点措辞变化的话，调用方显式传。

        ⚠️ `json_mode=True` 时 DeepSeek（与 OpenAI 同规）**要求 prompt 里出现 "json" 字样**，
        否则返回 400。这条约束落在调用方（`core/prompt.py`）身上，不是这里。
        """
        if not settings.deepseek_api_key:
            raise LLMNotConfiguredError(
                "DEEPSEEK_API_KEY 未配置 —— 检查 agent-service/.env 或进程环境变量"
            )

        payload: dict[str, object] = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = await self._client.post(_CHAT_PATH, json=payload)
        except httpx.TransportError as e:
            # 超时和连不上合并成一种 —— 对调用方来说处置方式相同（降级），
            # 分开只会逼它写两个 except 做同一件事。
            raise LLMTransportError(f"{type(e).__name__}: {e}") from e

        if resp.status_code != 200:
            raise LLMResponseError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            body = resp.json()
            text = body["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise TypeError(f"content 不是字符串：{type(text).__name__}")
            usage = body.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMResponseError(f"响应形状不对：{type(e).__name__}: {e}") from e

        result = LLMResult(
            text=text,
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
        )
        logger.info(
            "LLM %s: in=%d out=%d json=%s",
            settings.llm_model, result.tokens_in, result.tokens_out, json_mode,
        )
        return result

    async def aclose(self) -> None:
        await self._client.aclose()
