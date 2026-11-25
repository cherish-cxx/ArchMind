"""R4 索引接口（Java → Python）。

    POST /internal/index        触发某个项目的索引（幂等，异步执行）
    GET  /internal/index/status 查索引状态

和 `/internal/agent/*` 一样只认共享密钥；projectId 一律由 Java 注入。
触发只是「尽快开始」的优化：任务开工前会自己复验描述状态与 rev，
所以重复触发、丢失触发都不会产生错索引。

R8 起一次触发同时启动类索引与能力索引：两者共用同一个 embed 信号量，
「检索可用」因此只有一个时刻，不会出现能力能搜、类还不能搜的半开状态。
"""

from fastapi import APIRouter, Depends, Query, Request

from app.api.agent import require_internal_token
from app.core.capability_indexer import CapabilityIndexer
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


def get_capability_indexer(request: Request) -> CapabilityIndexer:
    return request.app.state.capability_indexer


def get_capability_index_store(request: Request) -> IndexStore:
    return request.app.state.capability_index_store


@router.post("", dependencies=[Depends(require_internal_token)])
async def start_index(
    req: IndexRequest,
    indexer: Indexer = Depends(get_indexer),
    capability_indexer: CapabilityIndexer = Depends(get_capability_indexer),
) -> dict:
    """返回 accepted / running / up_to_date，真正的索引进后台任务。

    类索引的返回字段在前，能力索引结果放在 `capability` 下 —— 两套任务
    各自独立判定 up_to_date / running，响应形状对旧调用方保持不变。
    """
    classes = await indexer.start(req.project_id, req.rev)
    capabilities = await capability_indexer.start(req.project_id, req.rev)
    return {**classes, "capability": capabilities}


@router.get("/status", dependencies=[Depends(require_internal_token)])
async def index_status(
    #: `alias` 不能省：内部接口对外一律 camelCase（`AskRequest`、`java_client`
    #: 的 body 都是），漏了它只会认 `?project_id=`，camelCase 请求直接 422。
    project_id: int = Query(..., alias="projectId"),
    #: 默认查类索引；`kind=capabilities` 时读能力索引的独立状态命名空间。
    kind: str = Query("classes"),
    store: IndexStore = Depends(get_index_store),
    capability_store: IndexStore = Depends(get_capability_index_store),
) -> dict:
    if kind == "capabilities":
        store = capability_store
    state = await store.get_state(project_id)
    return state or {"status": "absent", "projectId": project_id}
