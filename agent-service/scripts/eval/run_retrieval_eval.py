"""R4 在线检索评测：真实 embed + 真实 Qdrant，一键跑出可复现的数字。

评测的是**语义入口**这一层，不经过 LLM / Java 工具：
    问题文本 → embed（dense + sparse）→ Qdrant（projectId + rev 过滤）
    → 候选类 → 与人工标注的锚点比对

三种检索方式都测，用来回答「为什么是混合检索」：
    hybrid  生产路径（dense + sparse 各取 prefetch，RRF 融合）
    dense   只用稠密向量（语义）
    sparse  只用稀疏向量（词面）

指标：
    recall@1/3/5/10   每条问题的锚点有多少比例落在前 K（按问题取平均）
    hit@K             前 K 里是否至少命中一个锚点
    MRR@10            第一个命中锚点排名的倒数
    P50 / P95         端到端延迟（embed + Qdrant），以及分段延迟

用法（在 agent-service 目录下）：

    python scripts/eval/run_retrieval_eval.py \
        --project-id 2115334472151040002 \
        --dataset scripts/eval/retrieval_dataset.archmind.json \
        --embed-url http://172.21.160.203:8010 \
        --qdrant-url http://172.21.160.203:6333 \
        --redis-url redis://172.21.160.203:6379 \
        --out scripts/eval/out/retrieval-archmind.json

    # 只测生产路径（跳过消融），更快
    python scripts/eval/run_retrieval_eval.py ... --skip-ablation

rev 默认从 Redis 的 activeRev 读；也可以用 --rev 显式指定。
任何一条锚点不在索引里都直接失败 —— 评测集和索引不是同一版本时，数字没有意义。
"""

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as aioredis
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchValue,
    Prefetch,
    SparseVector,
)

# 允许从仓库任意位置以 `python scripts/eval/...` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.store.index_store import IndexStore  # noqa: E402
from app.tools.embed_client import EmbedClient  # noqa: E402
from app.tools.qdrant_store import QdrantStore  # noqa: E402

KS = (1, 3, 5, 10)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ArchMind 语义入口评测")
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--rev", type=int, default=None, help="默认从 Redis activeRev 读")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--embed-url", default=settings.embed_url)
    p.add_argument("--qdrant-url", default=settings.qdrant_url)
    p.add_argument("--collection", default=settings.qdrant_collection)
    p.add_argument("--redis-url", default=settings.redis_url)
    p.add_argument("--redis-db", type=int, default=settings.redis_db)
    p.add_argument("--timeout", type=float, default=settings.retrieve_timeout)
    p.add_argument("--skip-ablation", action="store_true", help="只测 hybrid")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--markdown", type=Path, default=None)
    return p.parse_args()


def percentile(values: list[float], pct: float) -> float:
    """最近秩法百分位；样本为空返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def rank_of(results: list, anchors: list[str]) -> dict[str, int | None]:
    """每个锚点第一次出现的位置（1-based），没出现为 None。

    **并列分按 uid 稳定排序**：RRF 的分数是「名次的和」，并列非常常见，
    Qdrant 对同分点的返回顺序不保证稳定。评测要可复现，所以这里显式定序。
    """
    ordered = sorted(results, key=lambda p: (-float(p.score or 0.0), p.payload.get("uid") or ""))
    positions = {r.payload.get("uid"): i + 1 for i, r in enumerate(ordered)}
    return {a: positions.get(a) for a in anchors}


def score_query(ranks: dict[str, int | None]) -> dict:
    present = [r for r in ranks.values() if r is not None]
    first = min(present) if present else None
    row = {
        "mrr": 1.0 / first if first else 0.0,
        "first_rank": first,
    }
    for k in KS:
        hits = sum(1 for r in present if r <= k)
        row[f"recall@{k}"] = hits / len(ranks) if ranks else 0.0
        row[f"hit@{k}"] = 1.0 if hits else 0.0
    # 主锚点（数据集里排在第一位、最该命中的类）单独统计：
    # 多锚点问题的严格 recall 会把「次要相关类没进前 5」也算成失败，
    # 与「自由提问能不能定位到入口」不是同一个问题，必须分开看。
    primary = next(iter(ranks.values())) if ranks else None
    row["primary_rank"] = primary
    for k in KS:
        row[f"primary_hit@{k}"] = 1.0 if primary is not None and primary <= k else 0.0
    return row


def summarize(rows: list[dict], latencies: list[dict]) -> dict:
    def mean(key: str) -> float:
        return statistics.fmean(r[key] for r in rows) if rows else 0.0

    summary = {"queries": len(rows)}
    for k in KS:
        summary[f"recall@{k}"] = round(mean(f"recall@{k}"), 4)
        summary[f"hit@{k}"] = round(mean(f"hit@{k}"), 4)
        summary[f"primary_hit@{k}"] = round(mean(f"primary_hit@{k}"), 4)
    summary["mrr@10"] = round(mean("mrr"), 4)
    summary["anchor_recall@5"] = round(
        sum(
            sum(1 for v in r["ranks"].values() if v is not None and v <= 5)
            for r in rows
        )
        / max(1, sum(len(r["ranks"]) for r in rows)),
        4,
    )
    summary["embed_ms"] = {
        "p50": round(percentile([x["embed_ms"] for x in latencies], 50), 1),
        "p95": round(percentile([x["embed_ms"] for x in latencies], 95), 1),
    }
    summary["query_ms"] = {
        "p50": round(percentile([x["query_ms"] for x in latencies], 50), 1),
        "p95": round(percentile([x["query_ms"] for x in latencies], 95), 1),
    }
    summary["total_ms"] = {
        "p50": round(percentile([x["total_ms"] for x in latencies], 50), 1),
        "p95": round(percentile([x["total_ms"] for x in latencies], 95), 1),
    }
    return summary


async def scroll_project_uids(client: AsyncQdrantClient, collection: str, project_id: int) -> list[dict]:
    """拉全该项目 payload，用来校验锚点存在性和跨项目隔离。"""
    points: list[dict] = []
    offset = None
    while True:
        batch, offset = await client.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[FieldCondition(key="projectId", match=MatchValue(value=project_id))]),
            limit=512,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(p.payload or {} for p in batch)
        if offset is None:
            return points


async def dense_query(client, collection, dense, flt, k):
    resp = await client.query_points(
        collection_name=collection, query=dense, using="dense",
        query_filter=flt, limit=k, with_payload=True,
    )
    return resp.points


async def sparse_query(client, collection, sparse, flt, k):
    if not sparse:
        return []
    indices = sorted(sparse)
    resp = await client.query_points(
        collection_name=collection,
        query=SparseVector(indices=indices, values=[sparse[i] for i in indices]),
        using="sparse", query_filter=flt, limit=k, with_payload=True,
    )
    return resp.points


async def fused_query(client, collection, dense, sparse, flt, k, fusion):
    """RRF / DBSF 对照。prefetch 与生产路径保持一致，只换融合函数。"""
    indices = sorted(sparse)
    prefetches = [Prefetch(query=dense, using="dense",
                           limit=settings.retrieve_prefetch_limit, filter=flt)]
    if sparse:
        prefetches.append(Prefetch(
            query=SparseVector(indices=indices, values=[sparse[i] for i in indices]),
            using="sparse", limit=settings.retrieve_prefetch_limit, filter=flt,
        ))
    resp = await client.query_points(
        collection_name=collection, prefetch=prefetches,
        query=FusionQuery(fusion=fusion), query_filter=flt,
        limit=k, with_payload=True,
    )
    return resp.points


async def main() -> int:
    args = parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    questions = dataset["questions"]
    negatives = dataset.get("negatives", [])

    if dataset.get("projectId") not in (None, args.project_id):
        print(f"[!!] 数据集 projectId={dataset['projectId']} 与 --project-id={args.project_id} 不一致")
        return 2

    # -------- rev：优先命令行，其次 Redis 状态 --------
    rds = aioredis.from_url(args.redis_url, db=args.redis_db, decode_responses=True)
    store = IndexStore(rds)
    state = await store.get_state(args.project_id)
    rev = args.rev if args.rev is not None else (state or {}).get("activeRev")
    if rev is None:
        print(f"[!!] 项目 {args.project_id} 没有 activeRev，索引不可用: {state}")
        await rds.aclose()
        return 2
    print(f"[OK] projectId={args.project_id} rev={rev} state={state.get('status') if state else None}")

    collection = args.collection
    qclient = AsyncQdrantClient(url=args.qdrant_url, timeout=60, check_compatibility=False)
    if not await qclient.collection_exists(collection):
        print(f"[!!] Qdrant 集合不存在: {collection}")
        await qclient.close(); await rds.aclose()
        return 2

    project_payloads = await scroll_project_uids(qclient, collection, args.project_id)
    indexed_uids = {p.get("uid") for p in project_payloads if p.get("rev") == rev}
    indexed_uids.discard(None)
    if not indexed_uids:
        print(f"[!!] rev={rev} 在 Qdrant 里没有任何点")
        await qclient.close(); await rds.aclose()
        return 2
    print(f"[OK] 本版索引点数 {len(indexed_uids)}，Qdrant 总点数 "
          f"{sum(1 for p in project_payloads if p.get('rev') == rev)}")

    missing = sorted({a for q in questions for a in q["anchors"]} - indexed_uids)
    if missing:
        print(f"[!!] 评测集有 {len(missing)} 个锚点不在索引里（版本不一致），先修数据集或重建索引：")
        for uid in missing:
            print("   -", uid)
        await qclient.close(); await rds.aclose()
        return 2

    flt = Filter(must=[
        FieldCondition(key="projectId", match=MatchValue(value=args.project_id)),
        FieldCondition(key="rev", match=MatchValue(value=rev)),
    ])

    embed = EmbedClient(base_url=args.embed_url)
    qstore = QdrantStore(url=args.qdrant_url, collection=collection)

    # -------- 逐题检索；一次 embed，三种方式复用 --------
    per_query: list[dict] = []
    latencies: list[dict] = []
    ablations = {} if args.skip_ablation else {"dense": [], "sparse": [], "dbsf": []}

    # 负样例不做召回评分，只看分数分布 —— 检索层没有"拒答"阈值
    neg_rows: list[dict] = []

    async def run_one(q: dict) -> tuple[dict, dict]:
        t0 = time.perf_counter()
        e0 = time.perf_counter()
        vectors = await asyncio.wait_for(embed.embed([q["question"]], timeout=args.timeout), timeout=args.timeout + 1)
        embed_ms = (time.perf_counter() - e0) * 1000
        dense, sparse = vectors[0]

        q0 = time.perf_counter()
        hybrid = await asyncio.wait_for(
            qstore.query(args.project_id, dense, sparse, rev=rev, limit=args.top_k),
            timeout=args.timeout,
        )
        query_ms = (time.perf_counter() - q0) * 1000
        total_ms = (time.perf_counter() - t0) * 1000

        ranks = rank_of(hybrid, q["anchors"])
        scored = score_query(ranks)
        row = {
            "id": q["id"],
            "question": q["question"],
            "category": q["category"],
            "difficulty": q.get("difficulty"),
            "anchors": q["anchors"],
            "ranks": ranks,
            **scored,
            "top": [
                {"uid": p.payload.get("uid"), "score": round(float(p.score), 4)}
                for p in hybrid
            ],
        }

        # 结果范围校验：过滤条件必须真的生效
        for p in hybrid:
            if p.payload.get("projectId") != args.project_id or p.payload.get("rev") != rev:
                row.setdefault("scope_violations", []).append(p.payload)

        if not args.skip_ablation:
            d = await dense_query(qclient, collection, dense, flt, args.top_k)
            s = await sparse_query(qclient, collection, sparse, flt, args.top_k)
            f = await fused_query(qclient, collection, dense, sparse, flt, args.top_k, Fusion.DBSF)
            for name, points in (("dense", d), ("sparse", s), ("dbsf", f)):
                ab_ranks = rank_of(points, q["anchors"])
                ablations[name].append({
                    "id": q["id"], "ranks": ab_ranks, **score_query(ab_ranks),
                })
            row["dense_top"] = [p.payload.get("uid") for p in d[:5]]
        return row, {"embed_ms": embed_ms, "query_ms": query_ms, "total_ms": total_ms}

    for q in questions:
        row, latency = await run_one(q)
        per_query.append(row)
        latencies.append(latency)
        print(f"  {row['id']:>4} recall@5={row['recall@5']:.2f} mrr={row['mrr']:.2f} "
              f"first={row['first_rank']}  {row['question'][:28]}")

    for n in negatives:
        try:
            vectors = await asyncio.wait_for(embed.embed([n["question"]], timeout=args.timeout), timeout=args.timeout + 1)
            pts = await asyncio.wait_for(
                qstore.query(args.project_id, vectors[0][0], vectors[0][1], rev=rev, limit=args.top_k),
                timeout=args.timeout,
            )
            neg_rows.append({
                "id": n["id"], "question": n["question"],
                "top1_score": round(float(pts[0].score), 4) if pts else None,
                "top1_uid": pts[0].payload.get("uid") if pts else None,
            })
        except Exception as e:  # 负样例失败不影响主指标
            neg_rows.append({"id": n["id"], "question": n["question"], "error": str(e)})

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "projectId": args.project_id,
        "rev": rev,
        "collection": collection,
        "dataset": str(args.dataset),
        "datasetVersion": dataset.get("version"),
        "indexState": state,
        "indexedPoints": len(indexed_uids),
        "topK": args.top_k,
        "metrics": {"hybrid": summarize(per_query, latencies)},
        "perCategory": {},
        "perQuery": per_query,
        "negatives": neg_rows,
        "checks": {
            "missingAnchors": missing,
            "scopeViolations": sum(len(r.get("scope_violations", [])) for r in per_query),
            "emptyResults": sum(1 for r in per_query if not r["top"]),
        },
    }

    if ablations:
        for name, rows in ablations.items():
            report["metrics"][name] = summarize(rows, latencies)

    # 分类汇总（只用 hybrid）
    for cat in sorted({q["category"] for q in per_query}):
        rows = [r for r in per_query if r["category"] == cat]
        report["perCategory"][cat] = {
            "queries": len(rows),
            "recall@5": round(statistics.fmean(r["recall@5"] for r in rows), 4),
            "mrr@10": round(statistics.fmean(r["mrr"] for r in rows), 4),
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 结果已写入 {args.out}")

    md_path = args.markdown or args.out.with_suffix(".md")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"[OK] 报告已写入 {md_path}")

    h = report["metrics"]["hybrid"]
    print(f"\nhybrid recall@5={h['recall@5']:.1%}  MRR@10={h['mrr@10']:.3f}  "
          f"P50={h['total_ms']['p50']}ms  P95={h['total_ms']['p95']}ms")
    if "dense" in report["metrics"]:
        d, s = report["metrics"]["dense"], report["metrics"]["sparse"]
        print(f"dense  recall@5={d['recall@5']:.1%}  MRR={d['mrr@10']:.3f}")
        print(f"sparse recall@5={s['recall@5']:.1%}  MRR={s['mrr@10']:.3f}")
    if "dbsf" in report["metrics"]:
        db = report["metrics"]["dbsf"]
        print(f"dbsf   recall@5={db['recall@5']:.1%}  MRR={db['mrr@10']:.3f}")
    print(f"主锚点 hit@5={h.get('primary_hit@5', 0):.1%}  问题级 hit@5={h['hit@5']:.1%}")
    print("判定:", "PASS" if h["recall@5"] >= 0.80 else f"FAIL (recall@5 {h['recall@5']:.1%} < 80%)")

    await embed.aclose()
    await qstore.aclose()
    await qclient.close()
    await rds.aclose()
    return 0 if h["recall@5"] >= 0.80 else 1


def render_markdown(r: dict) -> str:
    lines = [
        "# R4 语义入口评测报告",
        "",
        f"- 生成时间：{r['generatedAt']}",
        f"- 项目：`{r['projectId']}`，rev `{r['rev']}`，索引点数 {r['indexedPoints']}",
        f"- 数据集：`{r['dataset']}`",
        f"- 候选数：top-{r['topK']}",
        "",
        "## 总览",
        "",
        "| 检索方式 | recall@1 | recall@3 | recall@5 | recall@10 | MRR@10 | hit@5 | P50 | P95 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in ("hybrid", "dense", "sparse", "dbsf"):
        m = r["metrics"].get(name)
        if not m:
            continue
        lines.append(
            f"| {name} | {m['recall@1']:.1%} | {m['recall@3']:.1%} | "
            f"{m['recall@5']:.1%} | {m['recall@10']:.1%} | {m['mrr@10']:.3f} | "
            f"{m['hit@5']:.1%} | {m['total_ms']['p50']}ms | {m['total_ms']['p95']}ms |"
        )
    lines += [
        "",
        f"> 锚点级 recall@5：{r['metrics']['hybrid'].get('anchor_recall@5', 0):.1%}"
        f"（所有锚点里落在前 5 的比例）",
        f"> 主锚点 hit@5：{r['metrics']['hybrid'].get('primary_hit@5', 0):.1%}"
        f"（每条问题第一个锚点落在前 5 的比例）",
        f"> 检索层延迟为生产 hybrid 路径的 embed + Qdrant 总耗时；融合对照共用同一次 embed。",
        "",
        "## 分类",
        "",
        "| 分类 | 题数 | recall@5 | MRR@10 |",
        "| --- | --- | --- | --- |",
    ]
    for cat, m in r["perCategory"].items():
        lines.append(f"| {cat} | {m['queries']} | {m['recall@5']:.1%} | {m['mrr@10']:.3f} |")
    lines += ["", "## 未命中前 5 的问题", ""]
    fails = [q for q in r["perQuery"] if q["recall@5"] < 1.0]
    if not fails:
        lines.append("无。")
    else:
        for q in fails:
            ranks = ", ".join(f"{a.rsplit('.', 1)[-1]}={v}" for a, v in q["ranks"].items())
            lines.append(f"- `{q['id']}` {q['question']}（{ranks}）")
    lines += ["", "## 负样例（域外问题）", "", "| id | 问题 | top1 分 |", "| --- | --- | --- |"]
    for n in r["negatives"]:
        lines.append(f"| {n['id']} | {n['question']} | {n.get('top1_score')} |")
    lines += [
        "",
        "## 结论",
        "",
        f"- 严格锚点 recall@5 = {r['metrics']['hybrid']['recall@5']:.1%}（验收线 80%）"
        f" —— {'PASS' if r['metrics']['hybrid']['recall@5'] >= 0.8 else 'FAIL'}",
        f"- 主锚点 hit@5 = {r['metrics']['hybrid'].get('primary_hit@5', 0):.1%}；"
        f"问题级 hit@5 = {r['metrics']['hybrid']['hit@5']:.1%}",
        f"- 范围越界：{r['checks']['scopeViolations']} 条；空结果：{r['checks']['emptyResults']} 条",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
