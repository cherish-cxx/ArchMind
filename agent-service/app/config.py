from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """环境变量 → 配置对象。

    pydantic-settings 默认把环境变量名按「全大写」匹配字段名：
    AGENT_PORT → agent_port、JAVA_BASE_URL → java_base_url。
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

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


settings = Settings()

# 内部鉴权的请求头名。Java 侧 `InternalTokenFilter` 读的是同一个头，
# Python 这边发（java_client）和收（api/agent）也都要它 —— 写在一处，改的时候不会只改一半。
INTERNAL_TOKEN_HEADER = "X-Internal-Token"