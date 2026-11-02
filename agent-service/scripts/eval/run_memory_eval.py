"""R5 记忆离线指标报告（只读 MySQL）。

数据全部来自已有的持久化字段，不新增埋点表：

- 写入侧：记忆的类型/状态/信任分布、每个会话产出几条、同 subject 是否重复；
- 读取侧：`agent_turn.steps_json` 里的 `recall-memory(n:原因)` 步骤；
- 使用情况：`agent_memory.use_count` / `last_used_at`；
- 新鲜度：`stale` 比例、已蒸馏回合占比。

用法（在 agent-service 目录下）：

    python scripts/eval/run_memory_eval.py \
        --project-id 2115334472151040002 \
        --out scripts/eval/out/memory-report.json
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.store.db import Database  # noqa: E402
from app.store.orm import Memory, Turn  # noqa: E402

_RECALL_STEP = re.compile(r"^recall-memory\((\d+):([a-z_]+)\)$")


def parse_args():
    p = argparse.ArgumentParser(description="R5 记忆离线指标")
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--user-id", type=int, default=None)
    p.add_argument("--out", type=Path, required=True)
    return p.parse_args()


def _ratio(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


def _recall_events(turns: list[Turn]) -> tuple[Counter, list[int]]:
    """从 steps 里还原召回次数与每次命中条数。

    格式由 `core/loop.py::_recall_memories` 写入，改动它必须同步改这里的正则。
    """
    reasons: Counter = Counter()
    hit_counts: list[int] = []
    for turn in turns:
        for step in (turn.steps_json or []):
            detail = (step or {}).get("detail") or ""
            matched = _RECALL_STEP.match(detail)
            if matched:
                hit_counts.append(int(matched.group(1)))
                reasons[matched.group(2)] += 1
    return reasons, hit_counts


async def collect(args) -> dict:
    db = Database()
    try:
        async with db.session() as s:
            mem_stmt = select(Memory).where(Memory.project_id == args.project_id)
            if args.user_id is not None:
                mem_stmt = mem_stmt.where(Memory.user_id == args.user_id)
            memories = list((await s.execute(mem_stmt)).scalars().all())

            turn_stmt = select(Turn).where(Turn.project_id == args.project_id)
            if args.user_id is not None:
                turn_stmt = turn_stmt.where(Turn.user_id == args.user_id)
            turns = list((await s.execute(turn_stmt)).scalars().all())
    finally:
        await db.aclose()

    total = len(memories)
    status = Counter(m.status for m in memories)
    types = Counter(m.type for m in memories)
    trust = Counter(m.trust for m in memories)

    used = [m for m in memories if (m.use_count or 0) > 0]
    session_counts: Counter = Counter(
        m.source_conversation_id for m in memories if m.source_conversation_id
    )
    subject_groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for m in memories:
        if m.primary_subject_uid:
            subject_groups[(m.type, m.primary_subject_uid)].append(m.id)

    reasons, hit_counts = _recall_events(turns)
    distilled_turns = sum(1 for t in turns if t.memory_distilled_at is not None)

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "projectId": args.project_id,
        "userId": args.user_id,
        "memories": {
            "total": total,
            "byType": dict(types),
            "byStatus": dict(status),
            "byTrust": dict(trust),
            "staleRatio": _ratio(status.get("stale", 0), total),
            "activeRatio": _ratio(status.get("active", 0), total),
            "neverUsed": total - len(used),
            "neverUsedRatio": _ratio(total - len(used), total),
            "useCount": {
                "totalUses": sum(m.use_count or 0 for m in memories),
                "max": max((m.use_count or 0 for m in memories), default=0),
                "meanOnUsed": round(
                    statistics.mean(m.use_count for m in used), 2
                ) if used else 0.0,
            },
        },
        "sessions": {
            "withMemories": len(session_counts),
            "avgPerSession": round(
                statistics.mean(session_counts.values()), 2
            ) if session_counts else 0.0,
            "maxPerSession": max(session_counts.values(), default=0),
        },
        "duplicateSubjects": [
            {"type": key[0], "subjectUid": key[1], "count": len(ids), "ids": ids}
            for key, ids in subject_groups.items() if len(ids) > 1
        ],
        "recall": {
            "turns": len(turns),
            "triggeredTurns": sum(reasons.values()),
            "byReason": dict(reasons),
            "hits": {
                "total": sum(hit_counts),
                "zeroHit": sum(1 for n in hit_counts if n == 0),
                "min": min(hit_counts, default=0),
                "max": max(hit_counts, default=0),
                "mean": round(statistics.mean(hit_counts), 2) if hit_counts else 0.0,
            },
        },
        "distill": {
            "turns": len(turns),
            "distilled": distilled_turns,
            "distilledRatio": _ratio(distilled_turns, len(turns)),
        },
    }


def main() -> int:
    args = parse_args()
    report = asyncio.run(collect(args))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    m, r, d = report["memories"], report["recall"], report["distill"]
    print(f"memories={m['total']}  stale={m['staleRatio']:.0%}  "
          f"neverUsed={m['neverUsedRatio']:.0%}  sessions={report['sessions']['withMemories']}")
    print(f"recall: triggered={r['triggeredTurns']}  reasons={r['byReason']}  "
          f"hits mean={r['hits']['mean']} zero={r['hits']['zeroHit']}")
    print(f"distill: {d['distilled']}/{d['turns']} turns  "
          f"dupSubjects={len(report['duplicateSubjects'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
