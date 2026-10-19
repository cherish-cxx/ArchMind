FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 先拷依赖清单再装包：只要 requirements.txt 没变，这层就命中缓存
# 国内构建默认走阿里云源，可用 --build-arg PIP_INDEX_URL=... 覆盖
# （清华源曾按 IP 黑名单拒绝服务器出口 IP，返回 403；阿里云未出现此问题）
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
COPY requirements.txt .
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

COPY app ./app

EXPOSE 8000
# 必须 0.0.0.0：main.py 里的 __main__ 是 127.0.0.1，只适合本机调试
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]