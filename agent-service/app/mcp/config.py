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
    #: 缓存只省往返、不承担授权结论：吊销 token 必须**立即** 401，所以默认关闭。
    #: 确有往返压力时用 MCP_CONTEXT_CACHE_SECONDS 显式开启，并自行接受窗口内的延迟。
    context_cache_seconds: int = 0


mcp_settings = McpSettings()
