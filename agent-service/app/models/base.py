"""契约模型的公共地基。

单独成文件是因为 `tool.py` / `response.py` / `protocol.py` 三个模块都要用它 ——
同一个理由（「Java 发的是驼峰，Python 想写蛇形」）而变化的代码，应该住在一起。
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """契约模型基类：Python 侧写蛇形，JSON 里自动是驼峰。

    `populate_by_name=True` 让蛇形字段名也能直接构造（测试和内部代码方便）。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
