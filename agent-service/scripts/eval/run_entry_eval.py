"""R4 入口选择评测：检索 top-K → LLM1 选锚点，测「这一轮到底从哪个类开始查」。

**和 run_retrieval_eval.py 的区别**：
    检索评测只问「正确类进没进前 K」；
    入口评测问「LLM1 看完候选后，选中的是不是正确类」。

这两个数会差很多：检索 recall@5 到 95% 时，LLM1 仍可能因为候选窗口太窄、
或偏好泛化入口而选错。R4 的产品目标是「不点类也能问」，所以入口选择才是
更接近用户体感的指标。

同时跑负样例：域外问题拿到候选后，LLM1 是否仍然拒答（OUT_OF_SCOPE、entityRef=null）。
这是「候选不会把意图带偏」的必要证据。

用法（在 agent-service 目录下）：

    python scripts/eval/run_entry_eval.py \
        --project-id 2115334472151040002 \
        --dataset scripts/eval/retrieval_dataset.archmind.json \
        --embed-url http://172.21.160.203:8010 \
        --qdrant-url http://172.21.160.203:6333 \
        --redis-url redis://172.21.160.203:6379 \
        --top-k 5,10 \
        --out scripts/eval/out/entry-selection.json

只依赖 embed + Qdrant + DeepSeek，不需要 Java。
"""

import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as aioredis
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.core.intent import classify_intent  # noqa: E402
from app.core.llm import LLMClient  # noqa: E402
from app.core.retriever import to_candidate  # noqa: E402
from app.models.context import AgentContext  # noqa: E402
from app.models.intent import ANCHOR_MIN_SCORE, Intent  # noqa: E402
from app.store.index_store import IndexStore  # noqa: E402
from app.tools.embed_client import EmbedClient  # noqa: E402
from app.tools.qdrant_store import QdrantStore  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="R4 入口选择评测")
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--rev", type=int, default=None)
    p.add_argument("--top-k", default="5,10", help="逗号分隔，逐个跑")
    p.add_argument("--embed-url", default=settings.embed_url)
    p.add_argument("--qdrant-url", default=settings.qdrant_url)
    p.add_argument("--collection", default=settings.qdrant_collection)
    p.add_argument("--redis-url", default=settings.redis_url)
    p.add_argument("--redis-db", type=int, default=settings.redis_db)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--out", type=Path, required=True)
    return p.parse_args()


def resolve_ref(ref: str | None, candidates) -> str | None:
    """模型常只写短名；按候选唯一匹配还原成全限定名。"""
    if not ref:
        return None
    ref = ref.strip()
    uids = {c.uid for c in candidates}
    if ref in uids:
        return ref
    hits = [c.uid for c in candidates if c.simple_name == ref or c.uid.endswith("." + ref)]
    return hits[0] if len(hits) == 1 else ref


async def main() -> int:
    args = parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    ks = [int(x) for x in args.top_k.split(",") if x.strip()]

    rds = aioredis.from_url(args.redis_url, db=args.redis_db, decode_responses=True)
    state = await IndexStore(rds).get_state(args.project_id)
    rev = args.rev if args.rev is not None else (state or {}).get("activeRev")
    if rev is None:
        print(f"[!!] 项目 {args.project_id} 没有 activeRev")
        await rds.aclose()
        return 2

    embed = EmbedClient(base_url=args.embed_url)
    store = QdrantStore(url=args.qdrant_url, collection=args.collection)
    llm = LLMClient()
    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def run_question(q: dict, k: int) -> dict:
        try:
            vectors = await embed.embed([q["question"]])
            dense, sparse = vectors[0]
            points = await store.query(args.project_id, dense, sparse, rev=rev, limit=k)
            candidates = [c for c in (to_candidate(p) for p in points) if c]
        except Exception as e:
            return {"id": q["id"], "k": k, "error": f"retrieval: {e}"}

        async with sem:
            try:
                result = await classify_intent(
                    AgentContext(conversation_id="entry-eval", project_id=args.project_id),
                    q["question"], llm, candidates,
                )
            except Exception as e:
                return {"id": q["id"], "k": k, "error": f"llm: {type(e).__name__}: {e}"}

        raw = result.slots.entity_ref
        chosen = resolve_ref(raw, candidates)
        score = result.slots.anchor_score
        gated = bool(chosen and score is not None and score < ANCHOR_MIN_SCORE)
        if gated:
            # 与生产 loop 的 _ground_entity_ref 保持一致：分数不够就按无锚点处理。
            chosen = None
        return {
            "id": q["id"], "k": k, "question": q["question"],
            "intent": result.intent.value, "confidence": result.confidence,
            "anchor_score": score, "gated_by_score": gated,
            "raw_ref": raw, "chosen": chosen,
            "anchors": q["anchors"], "primary": q["anchors"][0],
            "in_candidates": bool(chosen and chosen in {c.uid for c in candidates}),
            "is_primary": chosen == q["anchors"][0],
            "is_anchor": chosen in q["anchors"],
            "top1": candidates[0].uid if candidates else None,
        }

    rows: list[dict] = []
    for k in ks:
        got = await asyncio.gather(*[run_question(q, k) for q in dataset["questions"]])
        rows.extend(got)
        ok = [r for r in got if "error" not in r]
        n = len(ok)
        print(f"k={k}: primary={sum(r['is_primary'] for r in ok)}/{n} "
              f"anyAnchor={sum(r['is_anchor'] for r in ok)}/{n} "
              f"wrong={sum(1 for r in ok if r['chosen'] and not r['is_anchor'])} "
              f"null={sum(1 for r in ok if not r['chosen'])} "
              f"errors={len(got) - len(ok)}")

    negatives: list[dict] = []
    for n in dataset.get("negatives", []):
        try:
            vectors = await embed.embed([n["question"]])
            points = await store.query(args.project_id, vectors[0][0], vectors[0][1], rev=rev, limit=max(ks))
            candidates = [c for c in (to_candidate(p) for p in points) if c]
            async with sem:
                result = await classify_intent(
                    AgentContext(conversation_id="entry-eval", project_id=args.project_id),
                    n["question"], llm, candidates,
                )
            negatives.append({
                "id": n["id"], "question": n["question"],
                "intent": result.intent.value, "confidence": result.confidence,
                "entityRef": result.slots.entity_ref,
                "refused": result.intent is Intent.OUT_OF_SCOPE,
                "top1": candidates[0].uid if candidates else None,
            })
        except Exception as e:
            negatives.append({"id": n["id"], "question": n["question"],
                              "error": f"{type(e).__name__}: {e}"})

    summary = {}
    for k in ks:
        ok = [r for r in rows if r["k"] == k and "error" not in r]
        summary[str(k)] = {
            "questions": len(ok),
            "primary_accuracy": round(sum(r["is_primary"] for r in ok) / max(1, len(ok)), 4),
            "any_anchor_accuracy": round(sum(r["is_anchor"] for r in ok) / max(1, len(ok)), 4),
            "wrong_selection": sum(1 for r in ok if r["chosen"] and not r["is_anchor"]),
            "null_ref": sum(1 for r in ok if not r["chosen"]),
            "gated_by_score": sum(1 for r in ok if r.get("gated_by_score")),
            "errors": sum(1 for r in rows if r["k"] == k and "error" in r),
            "mean_confidence": round(statistics.fmean(r["confidence"] for r in ok), 3) if ok else 0,
            "mean_anchor_score": round(
                statistics.fmean(r["anchor_score"] for r in ok if r.get("anchor_score") is not None), 2
            ) if any(r.get("anchor_score") is not None for r in ok) else None,
        }

    refused = sum(1 for n in negatives if n.get("refused"))
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "projectId": args.project_id, "rev": rev,
        "dataset": str(args.dataset), "topK": ks,
        "indexState": state,
        "summary": summary,
        "negatives": {
            "count": len(negatives), "refused": refused,
            "refusal_rate": round(refused / max(1, len(negatives)), 4),
            "items": negatives,
        },
        "perQuestion": rows,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 结果已写入 {args.out}")
    print(f"负样例拒答: {refused}/{len(negatives)}")

    await embed.aclose(); await store.aclose(); await llm.aclose(); await rds.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
