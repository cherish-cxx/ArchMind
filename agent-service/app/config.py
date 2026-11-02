from pathlib import Path
from urllib.parse import quote_plus

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

    #     ——————————MySQL：持久化（R5）——————————————
    # 和 Redis 一样：地址与密码分开，避免密码出现在 /health 里。
    # 用 AGENT_MYSQL_* 前缀，和 Java 侧的 MYSQL_PASSWORD（Spring 用）区分开 ——
    # 两边是两个不同权限的账号。
    agent_mysql_host: str = "localhost"
    agent_mysql_port: int = 3306
    agent_mysql_database: str = "archmind"
    agent_mysql_user: str = "archmind_agent"
    agent_mysql_password: str = ""
    agent_mysql_pool_size: int = 5
    agent_mysql_max_overflow: int = 10
    agent_mysql_echo: bool = False

    # 上下文工作窗口上限。**持久化的是完整 transcript，窗口只决定喂给模型多少。**
    message_window_max_messages: int = 40
    message_window_max_chars: int = 48000
    # 单轮落库的硬上限。超了就把工具返回体置空并置 truncated，绝不静默丢。
    agent_turn_max_payload_bytes: int = 512 * 1024

    @property
    def mysql_dsn(self) -> str:
        pwd = quote_plus(self.agent_mysql_password)
        return (
            f"mysql+aiomysql://{self.agent_mysql_user}:{pwd}"
            f"@{self.agent_mysql_host}:{self.agent_mysql_port}"
            f"/{self.agent_mysql_database}?charset=utf8mb4"
        )

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
    # 融合后给 LLM 看几个候选。R4 实测：正确入口经常排在第 6–10 名，
    # 5 个候选会把它们截断（Q04/Q22）；放到 10 后任意锚点命中率 90% → 97.5%。
    # 再多收益递减，prompt 也会明显变长。
    retrieve_top_k: int = 10
    # 检索是附加路径，**绝不能拖住主链路**：embed 挂住时不能用 index_timeout=120
    retrieve_timeout: float = 8.0

    # ── 记忆（R5）──────────────────────────────────────────────────────────
    # 总开关：关掉时既不写也不读，已有记忆保留。默认关，灰度用。
    memory_enabled: bool = False
    # 记忆用**独立集合**：和类索引共用时，重新分析按 rev 清理会把记忆一起删掉
    qdrant_memory_collection: str = "archmind_memories"
    memory_top_k: int = 3
    # 比类检索的 8s 更短：记忆挂了应该像没做记忆一样，不能拖住这一轮
    memory_recall_timeout: float = 3.0
    # 会话结算：空闲多久才认为"这段对话结束了"
    memory_settle_idle_minutes: int = 30
    # 少于这个轮数的会话不结算（一轮的会话没有"结论"可留）
    memory_settle_min_turns: int = 2
    # 一次结算最多读多少轮，防止超长会话把 prompt 撑爆
    memory_distill_max_turns: int = 30
    # 只结算最近 N 天结束的会话：首次打开开关时，不该把几个月的历史全部蒸一遍
    memory_distill_max_age_days: int = 7
    # 扫描间隔
    memory_scan_interval_seconds: int = 300
    # 单会话蒸馏锁 TTL：要大于一次 LLM 调用 + 写入的时间
    memory_distill_lock_ttl: int = 600
    # 容量上限（方案 §1.1 D13）
    memory_fact_cap: int = 200
    memory_open_cap: int = 20


settings = Settings()

# 内部鉴权的请求头名。Java 侧 `InternalTokenFilter` 读的是同一个头，
# Python 这边发（java_client）和收（api/agent）也都要它 —— 写在一处，改的时候不会只改一半。
INTERNAL_TOKEN_HEADER = "X-Internal-Token"