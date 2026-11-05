"""R6 多步循环评测：同一批问题跑 `/ask` 现在唯一的链路（tools）。

R6 第二步的 A/B 已经在 2026-11-04 跑完（结论见 `tests/` 与记忆）：
legacy 腿随六选一删除，这里只保留 tools 的度量。
保留这个脚本是因为**20 题 golden question 的验收口径**还要继续用。

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

MODES = ("tools",)

#: 断点披露的判据。和 prompt 里要求模型用的措辞对齐 —— 它在讲边界时
#: 必须说「没解析到 / 跨服务」，而不是讲成「到此结束」。
GAP_WORDS = ("断点", "没解析", "没有解析", "未解析", "跨服务", "边界")


def parse_args():
    p = argparse.ArgumentParser(description="R6 多步循环评测")
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--dataset", type=Path,
                   default=Path("tests/golden_questions.json"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--user-id", type=int, default=0)
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 题，0 = 全部")
    p.add_argument("--ids", default="", help="只跑这些题号，逗号分隔（H02,H03）")
    p.add_argument("--repeats", type=int, default=1,
                   help="每题重复跑几次。>1 时汇总里会带 drift（工具选择稳定性）")
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


def _offered_citations(turn) -> list[str]:
    """取**模型原始输出**里的引用，而不是落库的 `resp.citations`。

    落库那份已经被 `split_citations` 过滤过，越界的都删了 —— 数它永远是 0。
    """
    calls = turn.llm_calls
    for call in reversed(calls):
        for tool_call in call.get("toolCalls") or []:
            if tool_call.get("name") == "answer":
                return _cited_uids(tool_call.get("arguments") or "")
    return []


def _metrics(question: dict, ctx: AgentContext, resp) -> dict:
    # 「越界」= 模型提出、但**后端真的丢掉了**的引用。
    # 不能拿原始引用去和 `ctx.turn.evidence` 比 —— 循环的白名单已经放宽到
    # 「图上证据 ∪ 源码里出现过的类」，那样比会把留下来的也数成越界。
    offered = set(_offered_citations(ctx.turn))
    kept = {uid for section in resp.sections for uid in section.cited_uids}
    ungrounded = sorted(offered - kept)

    disclosed = None
    if question.get("mustDiscloseGap"):
        disclosed = any(word in resp.message for word in GAP_WORDS)

    llm_calls = ctx.turn.llm_calls
    return {
        "id": question.get("id"),
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
    """跑 `--repeats` 次，返回**第一次成功的指标** + 全部原始 run。

    为什么顶层保留一份完整指标：`_line` 和旧报告读的是它；
    汇总则改用 `runs`，否则重复跑时 token 只会算一次。
    """
    runs: list[dict] = []
    for i in range(max(1, args.repeats)):
        ctx = AgentContext(
            conversation_id=f"ab-{mode}-{question['id']}-{i}",
            project_id=args.project_id,
            user_id=args.user_id,
        )
        try:
            resp = await run_agent_turn(
                ctx, question["question"], java, llm, retriever, memory
            )
        except Exception as e:  # noqa: BLE001 —— 一题失败不该终止整轮评测
            runs.append({"error": f"{type(e).__name__}: {e}"})
            continue
        runs.append(_metrics(question, ctx, resp))

    good = [r for r in runs if "error" not in r]
    if not good:
        return {"error": runs[0]["error"] if runs else "no runs"}
    return {**good[0], "runs": runs}


# ==================== 汇总 ====================


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 1) if values else None


def _runs_of(metrics: dict) -> list[dict]:
    """一次问题的所有 run。老报告没有 `runs`，就当只跑了一次。"""
    return metrics.get("runs") or [metrics]


def jaccard(a: list[str], b: list[str]) -> float:
    """工具选择相似度。两边都空算完全一致 —— 那说明两次都没查，不是漂移。"""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def _drift(per_question: list[dict]) -> dict:
    """同题多次运行，前 3 个工具名的两两 Jaccard 均值。

    比「序列完全相等」现实（多步本来就有顺序自由度），
    比「集合相等」严格（换掉一个工具会被扣分）。
    """
    values: list[float] = []
    detail: dict[str, float] = {}
    for metrics in per_question:
        seqs = [r.get("toolNames", [])[:3] for r in _runs_of(metrics) if "error" not in r]
        pairs = [
            jaccard(seqs[i], seqs[j])
            for i in range(len(seqs))
            for j in range(i + 1, len(seqs))
        ]
        if not pairs:
            continue
        detail[str(metrics.get("id") or len(detail))] = round(statistics.fmean(pairs), 3)
        values.extend(pairs)

    return {
        "meanJaccard": round(statistics.fmean(values), 3) if values else None,
        "perQuestion": detail,
    }


def summarize(rows: list[dict], mode: str) -> dict:
    per_question = [r[mode] for r in rows if "error" not in r[mode]]
    ok = [
        run
        for metrics in per_question
        for run in _runs_of(metrics)
        if "error" not in run
    ]
    offered = sum(r["citationsOffered"] for r in ok)
    ungrounded = sum(r["citationsUngrounded"] for r in ok)
    return {
        "questions": len(per_question),
        "runs": len(ok),
        "errors": sum(1 for r in rows if "error" in r[mode])
                  + sum(1 for metrics in per_question
                        for run in _runs_of(metrics) if "error" in run),
        "answers": sum(1 for r in ok if r["outcome"] == "OK"),
        "degrades": sum(1 for r in ok if r["outcome"] in ("DEGRADED", "TOOL_DOWN", "TOOL_NO")),
        "clarifies": sum(
            1 for r in ok if r["outcome"] in ("CLARIFY", "NEED_ANCHOR", "REFUSE")
        ),
        "meanLatencyMs": _mean([r["latencyMs"] for r in ok]),
        "meanLlmCalls": _mean([r["llmCalls"] for r in ok]),
        "meanToolCalls": _mean([r["toolCalls"] for r in ok]),
        "maxToolCalls": max((r["toolCalls"] for r in ok), default=0),
        "totalTokensIn": sum(r["tokensIn"] for r in ok),
        "totalTokensOut": sum(r["tokensOut"] for r in ok),
        "citationsOffered": offered,
        "citationsUngrounded": ungrounded,
        "ungroundedRate": round(ungrounded / offered, 4) if offered else None,
        "gapRequired": sum(1 for r in ok if r["disclosedGap"] is not None),
        "gapDisclosed": sum(1 for r in ok if r["disclosedGap"] is True),
        "drift": _drift(per_question),
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

    tools = summarize(rows, "tools")
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "projectId": args.project_id,
        "dataset": str(args.dataset),
        "memoryEnabled": bool(args.with_memory),
        "summary": {"tools": tools},
        "perQuestion": rows,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"tools: {tools}")
    print(f"drift: {tools['drift']}")
    print(f"[OK] 结果已写入 {args.out}")
    return 0


def _build_memory(embed: EmbedClient) -> MemoryService:
    return MemoryService(MemoryRepo(Database()), MemoryStore(), embed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
