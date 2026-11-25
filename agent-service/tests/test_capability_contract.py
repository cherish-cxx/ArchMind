"""能力卡片契约：路径、camelCase 参数、null 字段解析。"""

import json

import httpx
import pytest

from app.config import INTERNAL_TOKEN_HEADER
from app.tools.java_client import JavaClient

_ENVELOPE = {
    "ok": True,
    "data": {
        "rev": 7,
        "items": [{
            "uid": "POST /orders",
            "name": "POST /orders",
            "description": "下单并扣减库存",
            "areaUid": "order",
            "areaName": "订单",
            "areaDescription": None,
            "entryUid": "a.OrderController#create()",
            "entryKind": "HTTP",
            "memberCount": 2,
            "members": [
                {"uid": "a.OrderController", "name": "OrderController", "role": "ENTRY", "depth": 0},
                {"uid": "a.OrderService", "name": "OrderService", "role": "BUSINESS", "depth": 1},
            ],
            "membersTruncated": False,
        }],
        "nextCursor": None,
    },
    "evidence": [],
    "truncated": False,
    "graphRev": 7,
}


async def test_capability_cards_page_is_get_with_camel_case_params():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["params"] = dict(req.url.params)
        return httpx.Response(200, json=_ENVELOPE)

    c = JavaClient()
    c._client = httpx.AsyncClient(
        base_url="http://mock", transport=httpx.MockTransport(handler),
        headers={INTERNAL_TOKEN_HEADER: "t"}, trust_env=False)

    page = await c.capability_cards_page(7, after_uid="POST /orders", limit=50)

    assert captured["method"] == "GET"
    assert captured["path"] == "/internal/tools/capabilities/cards"
    assert captured["params"] == {"projectId": "7", "afterUid": "POST /orders", "limit": "50"}
    assert page.data.items[0].uid == "POST /orders"
    assert page.data.items[0].members[1].role == "BUSINESS"
