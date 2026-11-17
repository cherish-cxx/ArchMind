"""R7 MCP 端到端验收：真连一个运行中的 MCP 服务，把五个工具走一遍。

用法（在 agent-service/ 下）：

    ARCHMIND_PAT=pat_xxx ARCHMIND_PROJECT_ID=1 python scripts/e2e_mcp.py

环境变量：
    ARCHMIND_MCP_URL    默认 http://127.0.0.1:8001/mcp（必须带 /mcp）
    ARCHMIND_PAT        必填，网页上创建的 PAT
    ARCHMIND_PROJECT_ID 必填，已分析完成、有索引的项目

退出码：0 全部通过；1 断言失败或连接/协议错误；2 缺少必填环境变量。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_URL = "http://127.0.0.1:8001/mcp"

EXPECTED_TOOLS = {
    "archmind_list_projects",
    "archmind_overview",
    "archmind_search_entities",
    "archmind_trace_flow",
    "archmind_project_brief",
}


class CheckFailed(AssertionError):
    """断言失败（含工具返回 ok=false）。"""


def _payload(result):
    """把 CallToolResult 解析成 dict；isError 或非 JSON 文本都算失败。"""
    if getattr(result, "isError", False):
        raise CheckFailed(f"工具返回 isError：{_text_of(result)}")
    text = _text_of(result)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CheckFailed(f"工具返回不是 JSON：{text[:300]}") from exc
    if not isinstance(data, dict):
        raise CheckFailed(f"工具返回不是 JSON 对象：{type(data).__name__}")
    return data


def _text_of(result) -> str:
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


def _require_ok(payload, step):
    if payload.get("ok") is not True:
        raise CheckFailed(
            f"{step}: ok != true，error={payload.get('error')!r} "
            f"hint={payload.get('hint')!r}"
        )
    return payload


async def run(url: str, pat: str, project_id: int) -> None:
    headers = {"Authorization": f"Bearer {pat}"}
    async with streamablehttp_client(url, headers=headers) as (
        read_stream,
        write_stream,
        _get_session_id,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            missing = EXPECTED_TOOLS - names
            if missing:
                raise CheckFailed(f"缺少工具：{sorted(missing)}；实际={sorted(names)}")
            print(f"tools={len(EXPECTED_TOOLS)}")

            projects = _require_ok(
                _payload(
                    await session.call_tool("archmind_list_projects", {})
                ),
                "list_projects",
            )
            project_items = projects.get("data") or []
            print(f"projects={len(project_items)}")

            search = _require_ok(
                _payload(
                    await session.call_tool(
                        "archmind_search_entities",
                        {"projectId": project_id, "query": "下单扣库存", "k": 10},
                    )
                ),
                "search_entities",
            )
            candidates = search.get("data") or []
            if not candidates:
                raise CheckFailed(
                    "search_entities: data 为空（项目可能没有索引，或查询未命中）"
                )
            uid = candidates[0].get("uid")
            if not uid:
                raise CheckFailed(f"search_entities: 第一项没有 uid={candidates[0]!r}")
            print(f"search_candidates={len(candidates)}")

            trace = _require_ok(
                _payload(
                    await session.call_tool(
                        "archmind_trace_flow",
                        {
                            "projectId": project_id,
                            "uid": uid,
                            "maxDepth": 3,
                            "maxNodes": 60,
                        },
                    )
                ),
                "trace_flow",
            )
            stages = trace.get("stages") or []
            if not stages:
                raise CheckFailed("trace_flow: stages 为空")
            print(f"stages={len(stages)}")

            brief = _require_ok(
                _payload(
                    await session.call_tool(
                        "archmind_project_brief",
                        {"projectId": project_id, "target": "claude"},
                    )
                ),
                "project_brief",
            )
            content = (brief.get("data") or {}).get("content") or ""
            if "projectId" not in content:
                raise CheckFailed("project_brief: content 里没有 projectId")
            print(f"brief_chars={len(content)}")

            print(f"OK url={url} project={project_id} uid={uid}")


def main() -> int:
    missing = [k for k in ("ARCHMIND_PAT", "ARCHMIND_PROJECT_ID") if not os.environ.get(k)]
    if missing:
        print(
            "缺少环境变量：" + ", ".join(missing) + "\n"
            "用法：ARCHMIND_PAT=pat_xxx ARCHMIND_PROJECT_ID=1 "
            "python scripts/e2e_mcp.py\n"
            f"可选：ARCHMIND_MCP_URL（默认 {DEFAULT_URL}）",
            file=sys.stderr,
        )
        return 2

    url = os.environ.get("ARCHMIND_MCP_URL", DEFAULT_URL)
    pat = os.environ["ARCHMIND_PAT"]
    try:
        project_id = int(os.environ["ARCHMIND_PROJECT_ID"])
    except ValueError:
        print("ARCHMIND_PROJECT_ID 必须是整数", file=sys.stderr)
        return 2

    try:
        asyncio.run(run(url, pat, project_id))
    except CheckFailed as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 连接失败、鉴权失败、协议错误等
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
