
import uvicorn
from fastapi import FastAPI

from app.config import settings

app = FastAPI(title="ArchMind Agent Service", version="0.1.0")


@app.get("/health")
def health() -> dict:
    """存活探针。P6 容器化后 compose 靠它判断服务起没起。"""
    return {"status": "ok", "javaBaseUrl": settings.java_base_url}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.agent_port, reload=True)