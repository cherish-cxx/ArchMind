"""R6 第二步 A/B：同一批问题，legacy（六选一）vs tools（多步循环）。

**两条链路在一个进程里依次跑**，不用重启、不用改 `AGENT_MODE` ——
问题集合、Java、检索、模型都是同一份，唯一的变量就是链路本身。
改成两次部署去比，环境抖动会混进来，结论就不可信了。

比四个数（路线图 §R6 的验收口径）：
  - **工具调用次数**：多步到底多查了几次
  - **token**：变好是不是靠变贵换来的
  - **citation 越界数**：从**原始模型输出**里数，落库的 citations 已经过滤过了
  - **端到端延迟**

用法（在 agent-service 目录下）：

    python scripts/eval/run_ab_eval.py \
        --project-id 2115334472151040002 \
        --out scripts/eval/out/ab-r6-step2.json

    # 换一套自己的问题（`{"questions": [{"id","question"}]}` 或裸数组都行）
    python scripts/eval/run_ab_eval.py --project-id 1 --dataset my-questions.json --out out.json

需要 Java + DeepSeek + embed + Qdrant + Redis。
**记忆默认不参与** —— 那是另一条变量，先把链路本身比清楚。
要连记忆一起比就加 `--with-memory`（此时还需要 MySQL 与 `MEMORY_ENABLED=true`）。
"""

import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as aioredis

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.core.agent_loop import run_agent_turn  # noqa: E402
from app.core.loop import run_turn  # noqa: E402
from app.core.llm import LLMClient  # noqa: E402
from app.core.memory import MemoryService  # noqa: E402
from app.core.retriever import Retriever  # noqa: E402
from app.models.context import AgentContext  # noqa: E402
from app.store.db import Database  # noqa: E402
from app.store.index_store import IndexStore  # noqa: E402
from app.store.memory_repo import MemoryRepo  # noqa: E402
from app.tools.embed_client import EmbedClient  # noqa: E402
from app.tools.java_client import JavaClient  # noqa: E402
from app.tools.memory_store import MemoryStore  # noqa: E402
from app.tools.qdrant_store import QdrantStore  # noqa: E402

MODES = ("legacy", "tools")

#: 断点披露的判据。和 prompt 里要求模型用的措辞对齐 —— 它在讲边界时
#: 必须说「没解析到 / 跨服务」，而不是讲成「到此结束」。
GAP_WORDS = ("断点", "没解析", "没有解析", "未解析", "跨服务", "边界")


def parse_args():
    p = argparse.ArgumentParser(description="R6 legacy vs tools A/B")
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--dataset", type=Path,
                   default=Path("tests/golden_questions.json"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--user-id", type=int, default=0)
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 题，0 = 全部")
    p.add_argument("--ids", default="", help="只跑这些题号，逗号分隔（H02,H03）")
    p.add_argument("--with-memory", action="store_true")
    p.add_argument("--embed-url", default=settings.embed_url)
    p.add_argument("--qdrant-url", default=settings.qdrant_url)
    p.add_argument("--collection", default=settings.qdrant_collection)
    p.add_argument("--redis-url", default=settings.redis_url)
    p.add_argument("--redis-db", type=int, default=settings.redis_db)
    return p.parse_args()


def load_questions(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data["questions"] if isinstance(data, dict) else data
    return [q for q in questions if q.get("question")]


# ==================== 指标 ====================


def _cited_uids(text: str) -> list[str]:
    """从一段模型输出里取 citedUids。兼容 `{sections[]}` 与旧的 `{citedUids}`。"""
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(obj, dict):
        return []

    if isinstance(obj.get("sections"), list):
        return [
            uid
            for section in obj["sections"]
            if isinstance(section, dict)
            for uid in (section.get("citedUids") or [])
            if isinstance(uid, str)
        ]
    return [uid for uid in (obj.get("citedUids") or []) if isinstance(uid, str)]


def _offered_citations(mode: str, turn) -> list[str]:
    """取**模型原始输出**里的引用，而不是落库的 `resp.citations`。

    落库那份已经被 `split_citations` 过滤过，越界的都删了 —— 数它永远是 0。
    """
    calls = turn.llm_calls
    if mode == "legacy":
        for call in reversed(calls):
            if call.get("purpose") == "explain" and call.get("rawResponse"):
                return _cited_uids(call["rawResponse"])
        return []

    for call in reversed(calls):
        for tool_call in call.get("toolCalls") or []:
            if tool_call.get("name") == "answer":
                return _cited_uids(tool_call.get("arguments") or "")
    return []


def _metrics(mode: str, question: dict, ctx: AgentContext, resp) -> dict:
    # 「越界」= 模型提出、但**后端真的丢掉了**的引用。
    # 不能拿原始引用去和 `ctx.turn.evidence` 比 —— 循环的白名单已经放宽到
    # 「图上证据 ∪ 源码里出现过的类」，那样比会把留下来的也数成越界。
    offered = set(_offered_citations(mode, ctx.turn))
    kept = {uid for section in resp.sections for uid in section.cited_uids}
    ungrounded = sorted(offered - kept)

    disclosed = None
    if question.get("mustDiscloseGap"):
        disclosed = any(word in resp.message for word in GAP_WORDS)

    llm_calls = ctx.turn.llm_calls
    return {
        "outcome": ctx.last_outcome,
        "latencyMs": resp.meta.latency_ms,
        "llmCalls": len(llm_calls),
        "tokensIn": sum(int(c.get("tokensIn") or 0) for c in llm_calls),
        "tokensOut": sum(int(c.get("tokensOut") or 0) for c in llm_calls),
        "toolCalls": ctx.budget.tool_calls_this_turn,
        "toolNames": [r.name for r in ctx.turn.tool_results],
        "evidenceUids": len(ctx.turn.evidence),
        "sections": len(resp.sections),
        "citations": len(resp.citations),
        "citationsOffered": len(offered),
        "citationsUngrounded": len(ungrounded),
        # 有 uid 才查得动：只报个数的话，只能猜是模型编的、还是校验口径太窄
        "citationsUngroundedUids": ungrounded[:10],
        "uiCommands": [c.op.value for c in resp.ui_commands],
        "disclosedGap": disclosed,
        # 只留一段预览：报告是用来扫一眼找异常的，不是存档讲解原文。
        "messagePreview": resp.message[:300],
    }


# ==================== 跑一轮 ====================


async def run_mode(
    mode: str, question: dict, *, args, java, llm, retriever, memory
) -> dict:
    ctx = AgentContext(
        conversation_id=f"ab-{mode}-{question['id']}",
        project_id=args.project_id,
        user_id=args.user_id,
    )
    try:
        if mode == "legacy":
            resp = await run_turn(ctx, question["question"], java, llm, retriever, memory)
        else:
            resp = await run_agent_turn(ctx, question["question"], java, llm, retriever, memory)
    except Exception as e:  # noqa: BLE001 —— 一题失败不该终止整轮评测
        return {"error": f"{type(e).__name__}: {e}"}
    return _metrics(mode, question, ctx, resp)


# ==================== 汇总 ====================


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 1) if values else None


def summarize(rows: list[dict], mode: str) -> dict:
    ok = [r[mode] for r in rows if "error" not in r[mode]]
    offered = sum(r["citationsOffered"] for r in ok)
    ungrounded = sum(r["citationsUngrounded"] for r in ok)
    return {
        "questions": len(ok),
        "errors": sum(1 for r in rows if "error" in r[mode]),
        "answers": sum(1 for r in ok if r["outcome"] == "OK"),
        "degrades": sum(1 for r in ok if r["outcome"] in ("DEGRADED", "TOOL_DOWN", "TOOL_NO")),
        "clarifies": sum(
            1 for r in ok if r["outcome"] in ("CLARIFY", "NEED_ANCHOR", "REFUSE")
        ),
        "meanLatencyMs": _mean([r["latencyMs"] for r in ok]),
        "meanLlmCalls": _mean([r["llmCalls"] for r in ok]),
        "meanToolCalls": _mean([r["toolCalls"] for r in ok]),
        "totalTokensIn": sum(r["tokensIn"] for r in ok),
        "totalTokensOut": sum(r["tokensOut"] for r in ok),
        "citationsOffered": offered,
        "citationsUngrounded": ungrounded,
        "ungroundedRate": round(ungrounded / offered, 4) if offered else None,
        "gapRequired": sum(1 for r in ok if r["disclosedGap"] is not None),
        "gapDisclosed": sum(1 for r in ok if r["disclosedGap"] is True),
    }


def _line(row: dict) -> str:
    parts = [f"{row['id']:<8}"]
    for mode in MODES:
        m = row[mode]
        if "error" in m:
            parts.append(f"{mode}: ERROR")
            continue
        parts.append(
            f"{mode}: {m['latencyMs']}ms "
            f"llm={m['llmCalls']} tool={m['toolCalls']} "
            f"tok={m['tokensIn']}+{m['tokensOut']} "
            f"cite={m['citations']}(越界{m['citationsUngrounded']}) "
            f"{m['outcome']}"
        )
    return " | ".join(parts)


# ==================== 入口 ====================


async def main() -> int:
    args = parse_args()
    questions = load_questions(args.dataset)
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        questions = [q for q in questions if q.get("id") in wanted]
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        print("[!!] 数据集里没有可跑的问题")
        return 2

    java = JavaClient()
    llm = LLMClient()
    embed = EmbedClient(base_url=args.embed_url)
    qdrant = QdrantStore(url=args.qdrant_url, collection=args.collection)
    rds = aioredis.from_url(args.redis_url, db=args.redis_db, decode_responses=True)
    retriever = Retriever(embed, qdrant, IndexStore(rds))
    memory = _build_memory(embed) if args.with_memory else None

    rows: list[dict] = []
    try:
        for question in questions:
            row = {"id": question["id"], "question": question["question"]}
            for mode in MODES:
                row[mode] = await run_mode(
                    mode, question, args=args, java=java, llm=llm,
                    retriever=retriever, memory=memory,
                )
            rows.append(row)
            print(_line(row))
    finally:
        await java.aclose()
        await llm.aclose()
        await embed.aclose()
        await qdrant.aclose()
        await rds.aclose()

    legacy = summarize(rows, "legacy")
    tools = summarize(rows, "tools")
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "projectId": args.project_id,
        "dataset": str(args.dataset),
        "memoryEnabled": bool(args.with_memory),
        "summary": {
            "legacy": legacy,
            "tools": tools,
            "delta": {
                "latencyMs": _delta(legacy["meanLatencyMs"], tools["meanLatencyMs"]),
                "toolCalls": _delta(legacy["meanToolCalls"], tools["meanToolCalls"]),
                "tokens": _delta(
                    legacy["totalTokensIn"] + legacy["totalTokensOut"],
                    tools["totalTokensIn"] + tools["totalTokensOut"],
                ),
                "ungrounded": tools["citationsUngrounded"] - legacy["citationsUngrounded"],
                "answers": tools["answers"] - legacy["answers"],
            },
        },
        "perQuestion": rows,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"legacy: {legacy}")
    print(f"tools : {tools}")
    print(f"[OK] 结果已写入 {args.out}")
    return 0


def _build_memory(embed: EmbedClient) -> MemoryService:
    return MemoryService(MemoryRepo(Database()), MemoryStore(), embed)


def _delta(before, after):
    if before is None or after is None:
        return None
    return round(after - before, 1)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
