from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 用**绝对路径**。默认的 ".env" 是相对「当前工作目录」解析的 ——
# 从别处（比如仓库根）启动服务就找不到它，于是**所有配置静默回落到默认值**：
# Redis 连成本机 localhost:6379、DEEPSEEK_API_KEY 变成空串，而且不报任何错。
# 从代码文件定位就没这个问题：无论从哪启动，都指向 agent-service/.env。
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """环境变量 → 配置对象。

    pydantic-settings 默认把环境变量名按「全大写」匹配字段名：
    AGENT_PORT → agent_port、JAVA_BASE_URL → java_base_url。
    """

    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8")

    agent_port: int = 8000
    java_base_url: str = "http://localhost:8080"
    internal_token: str = "dev-internal-token-change-me"

    # ── LLM（DeepSeek，走 OpenAI 兼容的 /chat/completions）──────────────────────
    # key 默认空串而不是必填：没配时应该在**第一次调用**时报出清楚的错，
    # 而不是在 import 期就炸 —— 契约测试和 /health 不该被一个没配的 key 拖死。
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    # 25s：设计 §8-C1 要求整轮 <60s。一轮最多两次 LLM 调用（Intent + 讲解），
    # 各 25s 仍留余量，且单次挂死也不会把整轮拖过网关那道 60s。
    llm_timeout: float = 25.0

    #     ——————————Redis db1：context——————————————
    redis_url:str = "redis://localhost:6379"
    redis_db:int = 1
    redis_password:str = ""
    context_ttl_seconds: int = 7200

    # ── 向量索引（R4）──────────────────────────────────────────────────────
    # 服务端容器默认地址；compose 里用 EMBED_URL / QDRANT_URL 覆盖成宿主机地址
    embed_url: str = "http://localhost:8010"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "archmind_classes"
    # 一页 200 个类、一次 embed 32 条、一次 upsert 64 个点 —— 都是保守值
    index_page_size: int = 200
    index_embed_batch: int = 32
    index_upsert_batch: int = 64
    # CPU 上 32 条文本可能跑十几秒，留足余量
    index_timeout: float = 120.0
    # 索引锁 TTL：防止进程崩溃后项目被永久锁死
    index_lock_ttl: int = 3600
    # 覆盖率低于这个比例就不认为索引可用。用比例不用绝对差 ——
    # 10 个类的项目差 1 个是 10%，1000 个类差 1 个是 0.1%，两者不该同一标准。
    index_min_coverage: float = 0.98

    # ── 检索（R4）──────────────────────────────────────────────────────────
    # 两路各取多少个进入融合。取少了融合没得挑，取多了白算。
    retrieve_prefetch_limit: int = 20
    # 融合后给 LLM 看几个候选。再多模型选不准，prompt 也会撑长。
    retrieve_top_k: int = 5
    # 检索是附加路径，**绝不能拖住主链路**：embed 挂住时不能用 index_timeout=120
    retrieve_timeout: float = 8.0


settings = Settings()

# 内部鉴权的请求头名。Java 侧 `InternalTokenFilter` 读的是同一个头，
# Python 这边发（java_client）和收（api/agent）也都要它 —— 写在一处，改的时候不会只改一半。
INTERNAL_TOKEN_HEADER = "X-Internal-Token"