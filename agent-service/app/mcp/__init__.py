"""ArchMind MCP 进程（R7）。

与 agent-service 主进程**刻意分开**：MCP 面向外部 agent，只读；
主进程的 /ask 链路和内部工具契约不因它改变。
"""
