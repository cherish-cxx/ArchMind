"""会话结算蒸馏（R5 阶段 3）。

一段对话空闲 30 分钟后，把它的 `agent_turn` 流水读出来，交给一个**独立的**
LLM 调用判断"下次还值得想起什么"，写成 1–3 条事实/未决问题。

## 为什么是独立的 LLM 调用

回答模型写的是给用户看的话：完整、有说服力、会补全。提取需要的是相反的目标——
"这轮里有什么是下周还值得想起、且能验证的"。用同一个模型自省，会把它的措辞
习惯和错误一起固化。

## 为什么从 agent_turn 读

`ctx` 会被 `prune_window` 删掉 TOOL/RETRIEVAL，`ctx.turn` 不进快照；
`AgentResponse` 没有 evidence / rev / outcome。只有 `agent_turn` 是完整、只追加的。

## 幂等

`memory_distilled_at` 就是状态：处理成功才写，LLM 失败保持 NULL 等下次扫。
多副本靠 Redis 单会话锁 + 唯一键合并；`SKIP LOCKED` 没用上是因为
LLM 调用不该放在事务里。
"""

import asyncio
import json
import logging
from typing import Any

import redis.asyncio

from app.config import settings
from app.core.llm import Completer, LLMError
from app.core.memory import MemoryService
from app.models.memory import (
    MAX_TEXT_CHARS,
    MemoryDraft,
    MemoryTrust,
    MemoryType,
)
from app.store.conversation_repo import ConversationRepo

logger = logging.getLogger(__name__)

#: 一个会话最多产几条。多于此说明模型在复述流水，不是在提炼结论。
MAX_MEMORIES_PER_SESSION = 3

_QUESTION_CHARS = 200
_ANSWER_CHARS = 600
_EVIDENCE_PER_TURN = 30

_SYSTEM = """\
你在整理一次已经结束的代码问答会话，任务是提炼"下次开新会话时值得带过来"的信息。

会话发生在一个 Java 项目的代码图谱问答系统里。用户问、系统查图、系统回答。

规则（违反任何一条，这条记忆就是错的）：
1. 只记**下次不查图就得不到**的东西：这次调查的结论、查到哪、哪里的边界或断点没解决。
   图里能重新查到的事实**不要记**。
2. 每条是一句原子断言，不超过 120 字。
3. subjectUids 只能从下面各轮给出的「证据」里挑，一个字符都不要改。
   如果你给了 uid 但全都不在证据里，这条会被系统丢弃。
4. 不记偏好、不记纠正、不记用户个人的事。
5. 最多 3 条。没有值得记的就返回空数组 —— 这是正常结果。
6. 只输出一个 JSON 对象，不要解释、不要 markdown：

{"memories": [{"type": "FACT", "text": "一句结论", "subjectUids": ["com.x.Y"], "confidence": 0.8}]}

type 只能是 FACT（这次查到的结论）或 OPEN_QUESTION（这次没查完、下次该继续的）。\
"""


def _render_turn(turn: Any) -> str:
    evidence = turn.evidence_json or []
    if evidence:
        shown = "、".join(evidence[:_EVIDENCE_PER_TURN])
    else:
        shown = "（无）"
    return (
        f"--- 第 {turn.turn_no} 轮（{turn.outcome}，graphRev={turn.graph_rev}）\n"
        f"问：{(turn.question or '')[:_QUESTION_CHARS]}\n"
        f"答：{(turn.answer_message or '')[:_ANSWER_CHARS]}\n"
        f"证据：{shown}"
    )


def build_distill_messages(turns: list[Any]) -> list[dict[str, str]]:
    """把流水渲染成蒸馏 prompt。纯函数，不碰 IO，可以单独测。"""
    body = "\n\n".join(_render_turn(t) for t in turns)
    user = f"以下是这次会话的全部轮次：\n\n{body}\n\n请输出 JSON。"
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]


def parse_memories(
    raw: str,
    *,
    allowed_uids: set[str],
    graph_rev: int,
    conversation_id: str,
    turn_no: int,
) -> list[MemoryDraft]:
    """把模型输出变成 `MemoryDraft` 列表。

    丢弃规则：
      - 类型不是 FACT / OPEN_QUESTION
      - 给了 subjectUids 但**一个都不在证据里**（B5）
      - 文本为空或超长（`MemoryDraft` 自己会拒绝）

    一条都不合法时返回空列表，**不是错误** —— 空是这个任务的正常结果。
    """
    data = json.loads(raw)
    items = data.get("memories") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("蒸馏输出缺少 memories 数组")

    drafts: list[MemoryDraft] = []
    for item in items[:MAX_MEMORIES_PER_SESSION]:
        if not isinstance(item, dict):
            continue
        try:
            memory_type = MemoryType(str(item.get("type") or ""))
        except ValueError:
            continue
        if memory_type not in (MemoryType.FACT, MemoryType.OPEN_QUESTION):
            continue

        raw_subjects = item.get("subjectUids") or []
        if not isinstance(raw_subjects, list):
            raw_subjects = []
        subjects = tuple(
            u for u in raw_subjects if isinstance(u, str) and u in allowed_uids
        )
        if raw_subjects and not subjects:
            continue

        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if len(text) > MAX_TEXT_CHARS:
            continue

        try:
            confidence = float(item.get("confidence") or 0.5)
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = min(1.0, max(0.0, confidence))

        drafts.append(MemoryDraft(
            type=memory_type,
            text=text,
            subject_uids=subjects,
            graph_rev=graph_rev,
            confidence=confidence,
            trust=MemoryTrust.DISTILLED,
            source_conversation_id=conversation_id,
            source_turn_no=turn_no,
        ))
    return drafts


class MemoryDistiller:
    def __init__(
        self,
        repo: ConversationRepo,
        memory: MemoryService,
        llm: Completer,
        redis_client: redis.asyncio.Redis | None = None,
    ) -> None:
        self._repo = repo
        self._memory = memory
        self._llm = llm
        self._redis = redis_client or redis.asyncio.from_url(
            settings.redis_url,
            db=settings.redis_db,
            password=settings.redis_password or None,
            decode_responses=True,
        )

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def run_forever(self) -> None:
        """扫描循环。未启用记忆时直接返回，不占一个后台任务。"""
        if not settings.memory_enabled:
            return
        while True:
            try:
                done = await self.distill_once()
                if done:
                    logger.info("记忆蒸馏：本轮结算了 %d 个会话", done)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 扫描失败不能把服务带下去
                logger.warning("记忆蒸馏扫描失败", exc_info=True)
            await asyncio.sleep(settings.memory_scan_interval_seconds)

    async def distill_once(self) -> int:
        """扫一轮，返回成功结算的会话数。"""
        if not settings.memory_enabled:
            return 0
        sessions = await self._repo.idle_conversations_for_distill(
            settings.memory_settle_idle_minutes,
            max_age_days=settings.memory_distill_max_age_days,
        )
        done = 0
        for conversation_id, user_id, project_id in sessions:
            if not await self._acquire(conversation_id):
                continue
            try:
                if await self._distill_session(
                    conversation_id, user_id, project_id
                ):
                    done += 1
            except Exception:  # noqa: BLE001 —— 一个会话失败不该影响其它会话
                logger.warning(
                    "结算会话 %s 失败，保留待重试", conversation_id, exc_info=True
                )
            finally:
                await self._release(conversation_id)
        return done

    async def _distill_session(
        self, conversation_id: str, user_id: int, project_id: int
    ) -> bool:
        turns = await self._repo.load_undistilled_turns(
            user_id, project_id, conversation_id,
            limit=settings.memory_distill_max_turns,
        )
        if not turns:
            return False

        ids = [t.id for t in turns]
        if len(turns) < settings.memory_settle_min_turns:
            # 一轮的会话没有"结论"可留。标掉，避免永远被扫到。
            await self._repo.mark_turns_distilled(ids)
            return False

        try:
            raw = await self._llm.complete(
                build_distill_messages(turns), json_mode=True
            )
        except LLMError as e:
            logger.warning("蒸馏 LLM 不可用，%s 保留待下次：%s", conversation_id, e)
            return False

        allowed = {u for t in turns for u in (t.evidence_json or [])}
        try:
            drafts = parse_memories(
                raw.text,
                allowed_uids=allowed,
                graph_rev=max(int(t.graph_rev or 0) for t in turns),
                conversation_id=conversation_id,
                turn_no=max(int(t.turn_no) for t in turns),
            )
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("蒸馏输出不可解析，%s 保留待下次：%s", conversation_id, e)
            return False

        for draft in drafts:
            try:
                await self._memory.remember(user_id, project_id, draft)
            except Exception as e:  # noqa: BLE001 —— 一条写失败不影响其它条
                logger.warning("记忆写入失败（%s）：%s", conversation_id, e)

        await self._memory.enforce_capacity(user_id, project_id)
        await self._repo.mark_turns_distilled(ids)
        if drafts:
            logger.info(
                "会话 %s 蒸馏出 %d 条记忆", conversation_id, len(drafts)
            )
        return True

    # ── 单会话锁 ────────────────────────────────────────────────────────────

    @staticmethod
    def _lock_key(conversation_id: str) -> str:
        return f"archmind:memory:distill:{conversation_id}"

    async def _acquire(self, conversation_id: str) -> bool:
        try:
            ok = await self._redis.set(
                self._lock_key(conversation_id), "1",
                nx=True, ex=settings.memory_distill_lock_ttl,
            )
            return bool(ok)
        except Exception as e:  # noqa: BLE001
            # 拿不到锁就跳过：多副本重复蒸馏的代价是一次多余的 LLM 调用
            logger.warning("记忆蒸馏锁不可用，跳过本轮：%s", e)
            return False

    async def _release(self, conversation_id: str) -> None:
        try:
            await self._redis.delete(self._lock_key(conversation_id))
        except Exception as e:  # noqa: BLE001
            # 锁有 TTL，删不掉最多等十分钟
            logger.warning("释放记忆蒸馏锁失败：%s", e)
