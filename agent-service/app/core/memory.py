"""记忆的写入与召回（R5 阶段 1）。

两条路径各一个入口：

- `MemoryService.remember` —— 用户显式"记住…"时写入；**语言触发**的判定在
  `explicit_draft`，是纯函数，可以脱离数据库和 LLM 测。
- `MemoryService.recall` —— 冷启动时按第一问召回。

**记忆永远是附加路径**：embed / Qdrant / MySQL 任何一步失败都退化成"这轮没有记忆"，
绝不让 `/ask` 多一个新的 500 来源。

写入侧的硬约束（B5）：`subject_uids` 必须是当轮 `evidence_json` 的子集。
这里只做"从 evidence 里挑"，不做"造一个 uid"—— 挑不到就不写。
"""

import asyncio
import logging
import re

from qdrant_client.models import PointStruct, SparseVector

from app.config import settings
from app.models.memory import (
    MAX_TEXT_CHARS,
    MemoryDraft,
    MemoryHit,
    MemoryTrust,
    MemoryType,
    normalize_text,
)
from app.store.memory_repo import MemoryRepo
from app.tools.embed_client import EmbedClient, EmbedError
from app.tools.memory_store import MemoryStore, point_id

# : 日志logging来自上面的import logging
#: 这里的logging.getlogger是使用工具箱里的getLogger工具
#：（。。。）是交给logging的参数
#：__name__是当前文件的名字
#：从文件中获取的日志就赋值给logger
logger = logging.getLogger(__name__)

#: 触发词。只做"用户让我记"这一类，不做"这句话看起来值得记"——
#: 后者属于蒸馏，不让模型在回答时顺手写记忆。
_EXPLICIT_MARKERS = ("记住", "记一下", "记下来", "备注一下", "别忘了")
_TRIM = "：:，,。.、;；!！?？ \t\r\n"

#: 注入 prompt 的条数上限。再多就不是"提示"而是"新的一份上下文"。
PROMPT_MEMORY_LIMIT = 5

#: 指代表达。`(?<!其)它` 是为了不把「其他」当指代；「这个」故意不收 ——
#: 「这个项目」在概览问题里太常见，收了等于每轮都查记忆。
_REFERRAL = re.compile(r"(?<!其)它|那个|上次|刚才|之前|接着|继续")

#: 明确指向"窗口之外"的说法。这几类不要求窗口为空 —— 它们本来就在说以前的事。
_CROSS_SESSION = re.compile(r"上次|之前|刚才")


def has_referral(message: str) -> bool:
    return bool(_REFERRAL.search(message))

#:def定义一个函数：has_cross_session_reference，参数是message，返回值是bool 目标：回答这句话中是否有“上次”、“之前”、“刚才”这类词
def has_cross_session_reference(message: str) -> bool:
    return bool(_CROSS_SESSION.search(message))

#：*的意思是：后面这些参数必须写明名字才能够传，否则会报错
#：补充**的意思是：后面这些参数可以不写明名字，但是必须按照顺序传，否则会报错
#：message参数：用户的回答
#：turns参数：用户的回答的轮数
#：last_outcome参数：用户的回答的上一轮的结果
#：has_anchor参数：用户的回答中是否有锚点
#：candidate_count参数：用户的回答的候选实体的数量
#：window_entities参数：用户的回答的窗口实体的集合
#：返回值是str或None 目标：根据不同的触发条件返回不同的触发原因
def should_recall(
    *,
    message: str,
    turns: int,
    last_outcome: str | None,
    has_anchor: bool,
    candidate_count: int,
    window_entities: set[str],
) -> str | None:
    """这一轮要不要查记忆。返回触发原因（写进 steps），`None` 表示不查。

    顺序即优先级：冷启动 → 上一轮澄清 → 指代无着落 → 类检索空手。
    **有锚点一律不查** —— 和 `_retrieve` 同一条规则：明确的指代不需要再猜。
    1.因为明确指代不是代词，所以不需要再猜，锚点直接进入上下文不需要格外的记忆支撑。
    2.turns参数：用户的回答的轮数，turns参数的值为0，说明是冷启动，所以不查询记忆。
    3.last_outcome参数：用户的回答的上一轮的结果，last_outcome参数的值为"CLARIFY"，说明是上一轮澄清，所以不查询记忆。（但是后续需要完善，即使是澄清的问题也可以有记忆支撑优化用户体验）
    5.candidate_count参数：用户的回答的候选实体的数量，candidate_count参数的值为0，说明没有候选实体，所以不查询记忆。
    6.window_entities参数：用户的回答的窗口实体的集合，window_entities参数的值为空集合，说明没有窗口实体，所以不查询记忆。
    记忆是附加路径，这里宁多勿漏：漏掉一条纠正的代价，远大于多花一次召回。
    """
    if has_anchor:
        return None
    if turns == 0:
        return "cold_start"
    if last_outcome == "CLARIFY":
        return "after_clarify"
    # 「上次 / 之前」本身就在说窗口之外，不用等窗口变空，利用上面定义的两个小函数进行判断
    if has_cross_session_reference(message):
        return "cross_session_reference"
    if has_referral(message) and not window_entities:
        return "unresolved_reference"
    if candidate_count == 0:
        return "no_candidates"
    return None

#：给下面的pick_subjects当翻译器
# 用户说"记住 LoginService 的缓存要清"，可证据里存的是 com.example.LoginService#login 这种长编号。直接拿长编号去用户那句话里找，肯定找不到。
# 所以要先用 simple_name 把它削成 LoginService，再问一句：这个短名有没有出现在用户原话里？ 出现了，就认为"用户点名的是这个类"，把它挑出来
def simple_name(uid: str) -> str:
    # 按#这个字符把字符串切开，切完得到一个列表，1的意思是最多只切一刀
    head = uid.split("#", 1)[0]
    return head.rsplit(".", 1)[-1]


def detect_explicit(message: str) -> str | None:
    """命中"记住…"时返回要去记的正文，未命中返回 None。

    支持前后两种位置：`记住：X` 取 `X`；`X，记住` 取 `X`。
    返回空串是合法的 —— 它表示"记住这个结论"，正文由回答的 headline 兜底。
    """
    for marker in _EXPLICIT_MARKERS:
        idx = message.find(marker)
        if idx < 0:
            continue
        rest = message[idx + len(marker):].strip(_TRIM)
        if len(rest) < 2:
            rest = message[:idx].strip(_TRIM)
        return rest
    return None

#：message：用户的原话
#：evidence_uids：证据的uid列表
#：focus_uid：焦点实体的uid
def pick_subjects(
    message: str, evidence_uids: list[str], focus_uid: str | None
) -> tuple[str, ...]:
    """从**本轮 evidence** 里挑出这条记忆指向的实体。

    优先级：消息里点名的类 → 当前焦点（且它在 evidence 里）→ 不写。
    不写比瞎指好：一条指错类的记忆会在下一次会话里被当成事实注入。
    """
    # 约等于准备了一个空名单，把整理里的编号一个个通过循环拿出来，通过simple_name削成短名
    # 然后再通过循环把短名一个个拿出来，通过if判断短名是否在用户的原话里
    # 如果在用户的原话里，就把短名加入到这个名单里
    # 如果不在用户的原话里，就跳过这个短名
    # 最后把这个名单返回给pick_subjects函数

    '''展开版的完整写法
    matched_list = []                       # 1. 先准备一个空名单
    for uid in evidence_uids:               # 2. 把证据里的编号，一个一个拿出来
        short = simple_name(uid)            # 3. 削成短名（上一轮讲的那个函数）
        if short and short in message:      # 4. 短名非空，而且出现在用户原话里
            matched_list.append(uid)        # 5. 符合 → 把这个【完整 uid】收进名单
    matched = tuple(matched_list)           # 6. 名单定稿，转成不可改的元组

    '''
    matched = tuple(
        uid for uid in evidence_uids if simple_name(uid) and simple_name(uid) in message
    )
    if matched:
        return matched
    if focus_uid and focus_uid in evidence_uids:
        return (focus_uid,)
    return ()


def _truncate(text: str) -> str:
    """超过单条上限时，优先在句末截断，找不到句末才硬截。"""
    head = text[:MAX_TEXT_CHARS]
    for sep in "。！？；;":
        idx = head.rfind(sep)
        if idx >= 20:
            return head[: idx + 1]
    return head[: MAX_TEXT_CHARS - 1] + "…"


def explicit_draft(
    *,
    message: str,
    evidence_uids: list[str],
    focus_uid: str | None,
    graph_rev: int,
    conversation_id: str,
    turn_no: int,
    headline: str | None = None,
) -> MemoryDraft | None:
    """用户显式记忆 → `MemoryDraft`；不满足条件返回 None。

    三个不写的条件：没有触发词、没有 evidence、挑不出 subject。
    """
    rest = detect_explicit(message)
    if rest is None:
        return None

    text = normalize_text(rest if len(rest) >= 2 else (headline or ""))
    if len(text) < 2:
        return None
    if len(text) > MAX_TEXT_CHARS:
        text = _truncate(text)

    subjects = pick_subjects(message, evidence_uids, focus_uid)
    if not subjects:
        return None

    return MemoryDraft(
        type=MemoryType.FACT,
        text=text,
        subject_uids=subjects,
        graph_rev=graph_rev,
        trust=MemoryTrust.EXPLICIT,
        source_conversation_id=conversation_id,
        source_turn_no=turn_no,
    )

# 把向量召回出来的记忆条目，睥睨改装成一段直接塞进prompt的文本，控制上限条数，没召回就返回空字符串避免污染prompt
def render_memories(hits: list[MemoryHit] | None) -> str:
    """召回结果 → prompt 里的一段。空列表返回空串（保证没记忆时 prompt 不变）。"""
    if not hits:
        return ""
    lines = ["=== 记忆参考（过去会话的结论，未查证，不是本轮证据）==="]
    for hit in hits[:PROMPT_MEMORY_LIMIT]:
        lines.append(f"- [{hit.label}] {hit.text}")
    return "\n".join(lines)

#：定义一个类：MemoryService，参数是repo，store，embed
class MemoryService:
    def __init__(
        self, repo: MemoryRepo, store: MemoryStore, embed: EmbedClient
    ) -> None:
        self._repo = repo
        self._store = store
        self._embed = embed

    @property
    def enabled(self) -> bool:
        """总开关。关掉时连 steps 都不该多一条 —— 审计里不该出现空跑的召回。"""
        return settings.memory_enabled

    # ==================== 写 ====================

    async def remember(
        self, user_id: int, project_id: int, draft: MemoryDraft
    ) -> tuple[object, str] | None:
        """写一条记忆并建索引。返回 `(行, 动作)`；未启用时返回 None。

        索引失败**不回滚 MySQL**：行是真相，索引可以重建。
        """
        if not settings.memory_enabled:
            return None
        row, action = await self._repo.upsert(user_id, project_id, draft)
        if action != "ignored":
            await self._index(row)
        return row, action

    async def _index(self, row) -> None:
        try:
            # 异步调用嵌入服务Embed，把这条记忆的文本row.txt转成向量，并为这次请求单独指定超时时间
            vectors = await self._embed.embed(
                [row.text], timeout=settings.memory_recall_timeout
            )
            # 取向量失败，只记一条警告并直接返回不把异常抛给上层
        except EmbedError as e:
            logger.warning("记忆 %s 已写入，但取向量失败（未建索引）：%s", row.id, e)
            return
        # 返回空列表时同样直接返回（没有向量就没法建立索引）
        if not vectors:
            return
        # 取出结构列表的第一条，解包成dense（稠密向量）和sparse（稀疏向量），再用sorted把稀疏向量的下标按升序排行，得到indices
        dense, sparse = vectors[0]
        indices = sorted(sparse)
        # 构造一个Qdrant点PointStruct并写入
        try:
            await self._store.upsert([PointStruct(
                # 由MySQL行id经UUID5生成确定的点ID，同一行重复写入始终是同一个点，因此upset幂等
                id=point_id(row.id),
                # vector："dense" 直接放稠密向量；
                # "sparse" 用 SparseVector(indices=indices, values=[sparse[i] for i in indices])，权重按排好序的下标顺序一一对应（Qdrant 要求稀疏向量下标递增）。
                vector={
                    "dense": dense,
                    "sparse": SparseVector(
                        indices=indices, values=[sparse[i] for i in indices]
                    ),
                },
                # 把记忆行的元数据一并存入，供召回时过滤与回显
                payload={
                    "memoryId": row.id,
                    "userId": row.user_id,
                    "projectId": row.project_id,
                    "type": row.type,
                    "status": row.status,
                    "primarySubjectUid": row.primary_subject_uid,
                    "subjectUids": list(row.subject_uids or []),
                    "graphRev": row.graph_rev,
                    "text": row.text,
                },
            )])
        except Exception as e:  # noqa: BLE001 —— 索引是附加路径
            logger.warning("记忆 %s 写入 Qdrant 失败：%s", row.id, e)

    async def enforce_capacity(self, user_id: int, project_id: int) -> None:
        """按类型上限淘汰。失败只记日志 —— 容量是卫生，不是正确性。

        **淘汰必须删点**：Qdrant 的 payload 仍是 `status=active`，
        只改 MySQL 的话召回过滤会把刚淘汰的记忆继续放出来。
        """
        if not settings.memory_enabled:
            return
        try:
            expired = await self._repo.expire_capacity(
                user_id, project_id, MemoryType.FACT, settings.memory_fact_cap
            )
            expired += await self._repo.expire_capacity(
                user_id, project_id, MemoryType.OPEN_QUESTION,
                settings.memory_open_cap,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆容量淘汰失败：%s", e)
            return
        if expired:
            await self._drop_points(expired)

    async def _drop_points(self, memory_ids: list[int]) -> None:
        try:
            await self._store.delete_ids(memory_ids)
        except Exception as e:  # noqa: BLE001 —— 删点失败最多留下一条查不到的脏点
            logger.warning("删除失效记忆索引失败 ids=%s：%s", memory_ids[:10], e)

    async def mark_stale(self, project_id: int, current_rev: int) -> int:
        """图谱翻版后把旧 rev 的事实置失效，并删掉索引点。返回处理条数。

        **调用方还没有接上**：重新分析成功后的 Java 网关还没调它。
        在那之前，跨 rev 的失效只能靠这里手动触发（评测脚本就是这么做的）。
        """
        if not settings.memory_enabled:
            return 0
        try:
            ids = await self._repo.mark_stale(project_id, current_rev)
        except Exception as e:  # noqa: BLE001
            logger.warning("标记记忆 stale 失败 project=%s：%s", project_id, e)
            return 0
        if ids:
            await self._drop_points(ids)
        return len(ids)

    # ==================== 读 ====================

    async def recall(
        self, user_id: int, project_id: int, query: str, *, k: int | None = None
    ) -> list[MemoryHit]:
        """按一句话召回记忆。任何一步失败都返回空列表，绝不抛到主链路。"""
        if not settings.memory_enabled or not query.strip():
            return []
        try:
            vectors = await self._embed.embed(
                [query], timeout=settings.memory_recall_timeout
            )
        except EmbedError as e:
            logger.warning("记忆召回取向量失败，本轮不注入：%s", e)
            return []
        if not vectors:
            return []
        dense, sparse = vectors[0]

        try:
            points = await asyncio.wait_for(
                self._store.query(user_id, project_id, dense, sparse, limit=k),
                timeout=settings.memory_recall_timeout,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆召回查询失败，本轮不注入：%s", e)
            return []

        hits = [_to_hit(p) for p in points]
        ids = [h.id for h in hits if h.id]
        if ids:
            try:
                await self._repo.mark_used(ids)
            except Exception as e:  # noqa: BLE001 —— 统计失败不影响本轮
                logger.warning("回写记忆使用次数失败：%s", e)
        return hits


def _to_hit(point) -> MemoryHit:
    payload = point.payload or {}
    raw_type = payload.get("type") or MemoryType.FACT.value
    try:
        memory_type = MemoryType(raw_type)
    except ValueError:
        memory_type = MemoryType.FACT
    return MemoryHit(
        id=int(payload.get("memoryId") or 0),
        text=str(payload.get("text") or ""),
        type=memory_type,
        subject_uids=tuple(payload.get("subjectUids") or ()),
        score=float(point.score or 0.0),
    )
