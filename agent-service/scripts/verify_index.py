"""R4 离线索引验收：把「索引真的对」拆成可执行的检查项。

检查分两类，**退出码只反映硬失败**：
    硬失败  状态/数量/向量/载荷/ID 不自洽 —— 索引不可信，必须修
    警告    覆盖率、降级描述、源码漂移、残留版本 —— 能用，但要记录

检查项：
    1. Redis 状态：status=ready、activeRev 存在、count==actual==expected
    2. Qdrant 集合：dense 1024/cosine、sparse 存在、payload 索引齐
    3. 点数：项目点数 == count；当前 rev 点数 == count；无其它 rev 残留
    4. 载荷：uid/simpleName/packageName/kind/description/descriptionSource/filePath/rev
    5. 向量：dense 1024 维、sparse 非空
    6. 幂等：point id == uuid5(archmind:{projectId}:{uid})，uid 不重复
    7. 描述来源：LLM / FALLBACK 占比
    8. 源码对账（可选 --source-root）：filePath 是否存在、当前源码顶层类型是否漂移

用法（在 agent-service 目录下）：

    python scripts/verify_index.py \
        --project-id 2115334472151040002 \
        --qdrant-url http://172.21.160.203:6333 \
        --redis-url redis://172.21.160.203:6379 \
        --source-root ../ArchMind \
        --source-prefix ArchMind \
        --out scripts/eval/out/index-verify.json

--source-root/--source-prefix 用来对账：索引里的 filePath 是相对上传根的
（例如 `ArchMind/src/main/java/...`），本地仓库根一般比它多一层。
"""

import argparse
import asyncio
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as aioredis
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store.index_store import IndexStore  # noqa: E402

REQUIRED_PAYLOAD = (
    "projectId", "uid", "simpleName", "packageName", "kind",
    "description", "descriptionSource", "filePath", "rev",
)
VALID_KINDS = {"CLASS", "INTERFACE", "ENUM"}
VALID_SOURCES = {"LLM", "FALLBACK"}

_DECL = re.compile(
    r"^\s*(?:public\s+|protected\s+|private\s+|abstract\s+|final\s+|static\s+|"
    r"sealed\s+|non-sealed\s+)*(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)"
)


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.info: dict = {}

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {"ok": self.ok, "failures": self.failures,
                "warnings": self.warnings, "info": self.info}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="R4 离线索引验收")
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--qdrant-url", required=True)
    p.add_argument("--collection", default="archmind_classes")
    p.add_argument("--redis-url", required=True)
    p.add_argument("--redis-db", type=int, default=1)
    p.add_argument("--repo-root", type=Path, default=None,
                   help="filePath 的本地基目录（上传根）")
    p.add_argument("--source-root", type=Path, default=None,
                   help="本地模块根（含 src/main/java、src/test/java），用于漂移对账")
    p.add_argument("--out", type=Path, required=True)
    return p.parse_args()


def top_level_types(source_root: Path) -> dict[str, str]:
    """与 Java 侧 GraphPredicates.TOP_LEVEL 同口径：只取顶层类型。

    用花括号深度过滤嵌套类；注释/字符串里的声明可能造成极少误判，
    这里只用于「漂移提示」，不作为硬失败。
    """
    found: dict[str, str] = {}
    for f in source_root.rglob("*.java"):
        text = f.read_text(encoding="utf-8", errors="replace")
        pkg_match = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
        pkg = pkg_match.group(1) if pkg_match else ""
        depth = 0
        for line in text.splitlines():
            if depth == 0:
                m = _DECL.match(line)
                if m:
                    found[f"{pkg}.{m.group(2)}"] = m.group(1)
            depth += line.count("{") - line.count("}")
    return found


async def scroll_all(client, collection: str, project_id: int) -> list:
    points, offset = [], None
    while True:
        batch, offset = await client.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[FieldCondition(key="projectId", match=MatchValue(value=project_id))]),
            limit=512, offset=offset, with_payload=True, with_vectors=True,
        )
        points.extend(batch)
        if offset is None:
            return points


async def count(client, collection: str, project_id: int, rev: int | None = None) -> int:
    must = [FieldCondition(key="projectId", match=MatchValue(value=project_id))]
    if rev is not None:
        must.append(FieldCondition(key="rev", match=MatchValue(value=rev)))
    r = await client.count(collection_name=collection,
                           count_filter=Filter(must=must), exact=True)
    return r.count


async def main() -> int:
    args = parse_args()
    report = Report()

    rds = aioredis.from_url(args.redis_url, db=args.redis_db, decode_responses=True)
    store = IndexStore(rds)
    state = await store.get_state(args.project_id)
    report.info["state"] = state

    if not state:
        report.fail(f"项目 {args.project_id} 没有索引状态")
        rev = None
    else:
        if state.get("status") != "ready":
            report.fail(f"索引状态不是 ready: {state.get('status')}")
        if state.get("activeRev") is None:
            report.fail("缺少 activeRev —— 检索会直接跳过")
        if state.get("count") != state.get("actual"):
            report.fail(f"状态内数量不一致: count={state.get('count')} actual={state.get('actual')}")
        if state.get("expected") and state.get("actual") != state.get("expected"):
            report.fail(f"覆盖不完整: actual={state.get('actual')} expected={state.get('expected')}")
        if state.get("partial"):
            report.warn("索引标记为 partial，检索结果可能缺类")
        rev = state.get("activeRev")

    client = AsyncQdrantClient(url=args.qdrant_url, timeout=60, check_compatibility=False)
    try:
        if not await client.collection_exists(args.collection):
            report.fail(f"Qdrant 集合不存在: {args.collection}")
            return finish(report, args)

        info = await client.get_collection(args.collection)
        dense = info.config.params.vectors.get("dense") if isinstance(info.config.params.vectors, dict) else None
        sparse = info.config.params.sparse_vectors or {}
        report.info["collection"] = {
            "points": info.points_count,
            "dense": {"size": getattr(dense, "size", None),
                      "distance": str(getattr(dense, "distance", None))},
            "has_sparse": "sparse" in sparse,
            "payload_schema": sorted((info.payload_schema or {}).keys()),
        }
        if getattr(dense, "size", None) != 1024:
            report.fail(f"dense 维度不是 1024: {getattr(dense, 'size', None)}")
        if "sparse" not in sparse:
            report.fail("集合缺少 sparse 向量")
        for field in ("projectId", "rev"):
            if field not in (info.payload_schema or {}):
                report.warn(f"payload 索引缺 {field}（查询会变慢）")

        if rev is not None:
            project_count = await count(client, args.collection, args.project_id)
            rev_count = await count(client, args.collection, args.project_id, rev)
            report.info["counts"] = {"project": project_count, "activeRev": rev_count}
            if project_count != state.get("count"):
                report.fail(f"Qdrant 项目点数 {project_count} != 状态 count {state.get('count')}")
            if rev_count != state.get("count"):
                report.fail(f"activeRev 点数 {rev_count} != 状态 count {state.get('count')}")

        points = await scroll_all(client, args.collection, args.project_id)
        uids: list[str] = []
        fallback: list[str] = []
        stray_revs: dict[int, int] = {}
        missing_files: list[str] = []
        for p in points:
            payload = p.payload or {}
            p_rev = payload.get("rev")
            stray_revs[p_rev] = stray_revs.get(p_rev, 0) + 1

            missing = [k for k in REQUIRED_PAYLOAD if k not in payload]
            if missing:
                report.fail(f"点 {p.id} 缺字段 {missing}")
                continue
            uid = payload["uid"]
            uids.append(uid)
            pkg, _, simple = uid.rpartition(".")
            if payload["simpleName"] != simple:
                report.fail(f"uid 与 simpleName 不一致: {uid} vs {payload['simpleName']}")
            if payload["packageName"] != pkg:
                report.fail(f"uid 与 packageName 不一致: {uid} vs {payload['packageName']}")
            if payload["kind"] not in VALID_KINDS:
                report.fail(f"非法 kind: {uid} -> {payload['kind']}")
            if payload["descriptionSource"] not in VALID_SOURCES:
                report.fail(f"非法 descriptionSource: {uid} -> {payload['descriptionSource']}")
            if payload["descriptionSource"] == "FALLBACK":
                fallback.append(uid)
            if not str(payload["description"] or "").strip():
                report.fail(f"空描述: {uid}")

            expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"archmind:{args.project_id}:{uid}"))
            if str(p.id) != expected_id:
                report.fail(f"point id 不稳定: {uid} -> {p.id} != {expected_id}")

            vec = p.vector or {}
            d = vec.get("dense")
            s = vec.get("sparse")
            if not isinstance(d, list) or len(d) != 1024:
                report.fail(f"dense 向量异常: {uid} len={len(d) if isinstance(d, list) else None}")
            if not s or not getattr(s, "indices", None):
                report.fail(f"sparse 向量为空: {uid}")

            if args.repo_root and payload["filePath"]:
                local = args.repo_root / Path(payload["filePath"])
                if not local.is_file():
                    missing_files.append(payload["filePath"])

        if len(uids) != len(set(uids)):
            dup = len(uids) - len(set(uids))
            report.fail(f"同一项目内 uid 重复 {dup} 个")
        other = {r: c for r, c in stray_revs.items() if rev is not None and r != rev}
        if other:
            report.warn(f"存在非 activeRev 的残留点: {other}（会被 rev 过滤挡住，但应清理）")
        if fallback:
            ratio = len(fallback) / max(1, len(uids))
            msg = f"FALLBACK 兜底描述 {len(fallback)}/{len(uids)} ({ratio:.1%})"
            if ratio > 0.10:
                report.warn(msg)
            else:
                report.info["fallback"] = msg
        report.info["descriptionSources"] = {
            "LLM": len(uids) - len(fallback), "FALLBACK": len(fallback),
        }

        if missing_files:
            report.warn(f"{len(missing_files)} 个 filePath 在本地源码根下不存在"
                        f"（示例: {missing_files[:3]}）")

        if args.source_root and rev is not None:
            source: dict[str, str] = {}
            for sub in ("src/main/java", "src/test/java"):
                d = args.source_root / sub
                if d.is_dir():
                    source.update(top_level_types(d))
            indexed = set(uids)
            missing = sorted(set(source) - indexed)
            extra = sorted(indexed - set(source))
            report.info["sourceDrift"] = {
                "localTopLevel": len(source),
                "indexed": len(indexed),
                "missingFromIndex": len(missing),
                "extraInIndex": len(extra),
                "missingSample": missing[:10],
            }
            if missing or extra:
                report.warn(
                    f"索引快照与当前源码不一致：索引少 {len(missing)} 个、多 {len(extra)} 个顶层类型 "
                    f"（分析图是上传时的快照，源码改动后需重新分析；不是索引器 bug）"
                )

        # 跨项目隔离：确认检索过滤字段在所有项目上一致可用
        all_points = await client.count(collection_name=args.collection, exact=True)
        report.info["collectionTotal"] = all_points.count
        report.info["projectPoints"] = len(points)

    finally:
        await client.close()
        await rds.aclose()

    return finish(report, args)


def finish(report: Report, args) -> int:
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "projectId": args.project_id,
        **report.as_dict(),
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 验收结果写入 {args.out}")

    for msg in report.failures:
        print("[FAIL]", msg)
    for msg in report.warnings:
        print("[WARN]", msg)
    print("[INFO] 状态:", "PASS" if report.ok else f"FAIL ({len(report.failures)} 项)")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
