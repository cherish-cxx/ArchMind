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


settings = Settings()