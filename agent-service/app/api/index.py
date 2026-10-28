"""R4 索引接口（Java → Python）。

    POST /internal/index        触发某个项目的索引（幂等，异步执行）
    GET  /internal/index/status 查索引状态

和 `/internal/agent/*` 一样只认共享密钥；projectId 一律由 Java 注入。
触发只是「尽快开始」的优化：任务开工前会自己复验描述状态与 rev，
所以重复触发、丢失触发都不会产生错索引。
"""

from fastapi import APIRouter, Depends, Query, Request

from app.api.agent import require_internal_token
from app.core.indexer import Indexer
from app.models.base import CamelModel
from app.store.index_store import IndexStore

router = APIRouter(prefix="/internal/index", tags=["index"])


class IndexRequest(CamelModel):
    project_id: int
    #: Java 侧的图版本。用于「已索引且 rev 相同」快速返回 up_to_date
    rev: int | None = None


def get_indexer(request: Request) -> Indexer:
    return request.app.state.indexer


def get_index_store(request: Request) -> IndexStore:
    return request.app.state.index_store


@router.post("", dependencies=[Depends(require_internal_token)])
async def start_index(
    req: IndexRequest,
    indexer: Indexer = Depends(get_indexer),
) -> dict:
    """返回 accepted / running / up_to_date，真正的索引进后台任务。"""
    return await indexer.start(req.project_id, req.rev)


@router.get("/status", dependencies=[Depends(require_internal_token)])
async def index_status(
    project_id: int = Query(...),
    store: IndexStore = Depends(get_index_store),
) -> dict:
    state = await store.get_state(project_id)
    return state or {"status": "absent", "projectId": project_id}
