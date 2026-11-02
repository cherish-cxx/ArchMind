"""R5 跨会话召回评测：种子记忆 → 自然语言问题 → recall@1/3。

和 R4 的 `run_retrieval_eval.py` 同一个位置：**只依赖 embed + Qdrant + MySQL**，
不需要 Java，也不需要 LLM。它回答的是"记忆检索本身好不好"，
不测"模型会不会用记忆"。

用法（在 agent-service 目录下）：

    python scripts/eval/run_memory_recall_eval.py \
        --dataset scripts/eval/memory_cases.json \
        --project-id 999900095 --user-id 9001 \
        --out scripts/eval/out/memory-recall.json

⚠️ 脚本会**先清空该项目下的记忆**再做种子写入。默认只允许测试项目
（`project-id >= 900000000`，和 R5 测试用的 999_900_xxx 一致），
要动真实项目必须显式加 `--allow-live`。
"""

import argparse
import asyncio
import contextlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.core.memory import MemoryService  # noqa: E402
from app.models.memory import MemoryDraft, MemoryType, MemoryTrust  # noqa: E402
from app.store.db import Database  # noqa: E402
from app.store.memory_repo import MemoryRepo  # noqa: E402
from app.tools.embed_client import EmbedClient  # noqa: E402
from app.tools.memory_store import MemoryStore  # noqa: E402

TEST_PROJECT_FLOOR = 900_000_000


def parse_args():
    p = argparse.ArgumentParser(description="R5 跨会话记忆召回评测")
    p.add_argument("--dataset", type=Path,
                   default=Path(__file__).with_name("memory_cases.json"))
    p.add_argument("--project-id", type=int, default=999_900_095)
    p.add_argument("--user-id", type=int, default=9001)
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--embed-url", default=settings.embed_url)
    p.add_argument("--qdrant-url", default=settings.qdrant_url)
    p.add_argument("--collection", default=settings.qdrant_memory_collection)
    p.add_argument("--allow-live", action="store_true",
                   help="允许对真实项目跑（会先清空该项目的记忆）")
    p.add_argument("--keep", action="store_true",
                   help="跑完不清理，方便紧接着看指标报告")
    p.add_argument("--out", type=Path, required=True)
    return p.parse_args()


async def run(args) -> dict:
    if args.project_id < TEST_PROJECT_FLOOR and not args.allow_live:
        raise SystemExit(
            "拒绝在真实项目上跑：脚本会清空该项目的全部记忆。"
            "换一个测试 project-id，或显式加 --allow-live。"
        )

    data = json.loads(args.dataset.read_text(encoding="utf-8"))
    settings.memory_enabled = True

    db = Database()
    repo = MemoryRepo(db)
    store = MemoryStore(url=args.qdrant_url, collection=args.collection)
    embed = EmbedClient(base_url=args.embed_url, timeout=30)
    service = MemoryService(repo, store, embed)

    try:
        # 从干净状态开始：上一次失败留下的点会让 recall 命中错误条目
        await repo.purge_project(args.project_id)
        try:
            await store.delete_project(args.project_id)
        except Exception:  # 集合还不存在时会抛，忽略
            pass

        key_by_id: dict[int, str] = {}
        for seed in data["seed"]:
            draft = MemoryDraft(
                type=MemoryType(seed["type"]),
                text=seed["text"],
                subject_uids=tuple(seed["subjectUids"]),
                graph_rev=7,
                confidence=0.9,
                trust=MemoryTrust.DISTILLED,
            )
            row, _ = await service.remember(args.user_id, args.project_id, draft)
            key_by_id[row.id] = seed["key"]

        results = []
        for case in data["cases"]:
            hits = await service.recall(
                args.user_id, args.project_id, case["question"], k=args.top_k
            )
            keys = [key_by_id.get(h.id) for h in hits]
            expected = case["expectKey"]
            must_not = case.get("mustNotRecall") or []

            # RRF 同分时 Qdrant 的先后是不保证的。同分算"并列第一"，
            # 否则同一个问题的 hit@1 会在两次运行之间来回跳。
            scores = [h.score for h in hits]
            expected_idx = keys.index(expected) if expected in keys else None
            tied_first = (
                expected_idx is not None
                and bool(scores)
                and abs(scores[0] - scores[expected_idx]) < 1e-9
            )

            results.append({
                "id": case["id"],
                "question": case["question"],
                "expected": expected,
                "recalled": keys,
                "hitAt1": bool(keys) and (keys[0] == expected or tied_first),
                "tiedAt1": tied_first,
                f"hitAt{args.top_k}": expected in keys,
                # 第 1 名错是真正的错；前 K 名里出现同域干扰项算上下文噪声，
                # 分开计：前者决定退出码，后者只作为基线观察。
                "forbiddenAt1": bool(keys) and keys[0] in must_not,
                "forbiddenHits": [k for k in keys if k in must_not],
                "hits": [
                    {"key": key_by_id.get(h.id), "score": round(h.score, 4),
                     "text": h.text}
                    for h in hits
                ],
            })

        rev_results = []
        for case in data.get("revCases") or []:
            seed = next(s for s in data["seed"] if s["key"] == case["seedKey"])

            # 图谱翻版：旧 rev 的事实必须从召回里消失
            await service.mark_stale(args.project_id, case["staleRev"])
            stale_hits = await service.recall(
                args.user_id, args.project_id, case["question"], k=args.top_k
            )
            stale_keys = [key_by_id.get(h.id) for h in stale_hits]

            # 新 rev 下同一条结论被重新推导出来 → 复活并重新可召回
            draft = MemoryDraft(
                type=MemoryType(seed["type"]),
                text=seed["text"],
                subject_uids=tuple(seed["subjectUids"]),
                graph_rev=case["reviveRev"],
                confidence=0.9,
                trust=MemoryTrust.DISTILLED,
            )
            row, action = await service.remember(
                args.user_id, args.project_id, draft
            )
            key_by_id[row.id] = seed["key"]

            revived_hits = await service.recall(
                args.user_id, args.project_id, case["question"], k=args.top_k
            )
            revived_keys = [key_by_id.get(h.id) for h in revived_hits]

            rev_results.append({
                "id": case["id"],
                "seedKey": seed["key"],
                "notRecalledWhileStale": seed["key"] not in stale_keys,
                "staleRecalled": stale_keys,
                "reviveAction": action,
                "revivedAndRecalled": seed["key"] in revived_keys,
                "revivedRecalled": revived_keys,
            })

        total = len(results)
        rev_passed = sum(
            1 for r in rev_results
            if r["notRecalledWhileStale"]
            and r["revivedAndRecalled"]
            and r["reviveAction"] == "revived"
        )
        return {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "projectId": args.project_id,
            "userId": args.user_id,
            "topK": args.top_k,
            "summary": {
                "cases": total,
                "hitAt1": sum(r["hitAt1"] for r in results) / total,
                f"hitAt{args.top_k}": sum(r[f"hitAt{args.top_k}"] for r in results) / total,
                "forbiddenHits": sum(len(r["forbiddenHits"]) for r in results),
                "forbiddenAt1": sum(1 for r in results if r["forbiddenAt1"]),
                "revCases": len(rev_results),
                "revPassed": rev_passed,
            },
            "results": results,
            "revResults": rev_results,
        }
    finally:
        # 评测数据不留在库里；索引点和行一起清。--keep 时保留，供指标报告读取。
        if not args.keep:
            with contextlib.suppress(Exception):
                await repo.purge_project(args.project_id)
            with contextlib.suppress(Exception):
                await store.delete_project(args.project_id)
        await embed.aclose()
        await store.aclose()
        await repo.aclose()


def main() -> int:
    args = parse_args()
    report = asyncio.run(run(args))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    s = report["summary"]
    k = report["topK"]
    hit_key = f"hitAt{k}"
    print(f"cases={s['cases']}  hit@1={s['hitAt1']:.0%}  "
          f"hit@{k}={s[hit_key]:.0%}  forbidden@1={s['forbiddenAt1']}  "
          f"forbidden@{k}={s['forbiddenHits']}")
    for r in report["results"]:
        mark = "OK " if r[hit_key] else "MISS"
        print(f"  [{mark}] {r['id']} expected={r['expected']} recalled={r['recalled']}")

    if report["revResults"]:
        print(f"rev: {s['revPassed']}/{s['revCases']} passed")
        for r in report["revResults"]:
            ok = (r["notRecalledWhileStale"] and r["revivedAndRecalled"]
                  and r["reviveAction"] == "revived")
            print(f"  [{'OK ' if ok else 'FAIL'}] {r['id']} stale={r['staleRecalled']} "
                  f"revive={r['reviveAction']} revived={r['revivedRecalled']}")

    return 0 if s["forbiddenAt1"] == 0 and s["revPassed"] == s["revCases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
