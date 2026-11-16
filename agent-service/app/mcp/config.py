"""MCP 进程自己的配置。

**不塞进 `app/config.py`**：那个文件承载主进程的全部运行时配置，
MCP 只需要限流与缓存两个旋钮；分开也不会让主进程的配置面变宽。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_")

    #: 每个 PAT 每分钟允许的请求数（固定窗口）。
    rate_limit_per_minute: int = 60
    #: authenticate / context 结果缓存秒数。PAT 的吊销与项目归属由 Java 每次校验，
    #: 这里的缓存只省往返，不承担授权结论 —— 所以窗口必须短。
    context_cache_seconds: int = 60


mcp_settings = McpSettings()
