#!/usr/bin/env bash
# 手动更新部署：拉取后端/前端两个仓库，有变化才重建对应镜像
#
# 服务器目录约定（可用环境变量覆盖）：
#   ~/ArchMind       后端仓库（本脚本所在仓库）
#   ~/ArchMind-react 前端仓库（与后端仓库平级）
#
# 用法：./deploy/update.sh
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${BACKEND_DIR:-$(dirname "$DEPLOY_DIR")}"
FRONTEND_DIR="${FRONTEND_DIR:-$(dirname "$(dirname "$DEPLOY_DIR")")/ArchMind-react}"

if [ ! -f "$DEPLOY_DIR/.env" ]; then
  echo "!! 缺少 $DEPLOY_DIR/.env（参考 .env.example）" >&2
  exit 1
fi

# 兼容 docker-compose v1 / docker compose v2
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "!! 未找到 docker compose" >&2
  exit 1
fi

# 返回 0=有更新，1=无更新；拉取失败直接终止，不盲目重建
sync_repo() {
  local dir=$1 name=$2 before after
  if [ ! -d "$dir/.git" ]; then
    echo "!! 找不到 git 仓库：$dir" >&2
    exit 1
  fi

  before=$(git -C "$dir" rev-parse HEAD)
  echo "==> 拉取${name}：$dir"
  if ! git -C "$dir" pull --ff-only; then
    echo "!! ${name}拉取失败（本地有改动或分支分叉），请先处理后重跑" >&2
    exit 1
  fi
  after=$(git -C "$dir" rev-parse HEAD)

  if [ "$before" = "$after" ]; then
    echo "    ${name}无更新"
    return 1
  fi
  echo "    ${name}更新：${before:0:7} -> ${after:0:7}"
  return 0
}

backend_changed=false
services=()
if sync_repo "$BACKEND_DIR" "后端"; then
  backend_changed=true
  # migrate 必须一起重建：它的镜像里带的是 alembic/versions，
  # 新增迁移后不重建会报 "Can't locate revision"，agent-service 因为
  # depends_on 它而根本起不来。
  # mcp / mcp-migrate 也必须带上：mcp 复用 agent-service 镜像且不在默认
  # 重建列表里，漏掉就永远不启动；mcp-migrate 是一次性容器，负责用
  # mysql:8 跑 ArchMind/sql 下 Java 侧的表（如 mcp_pat），SQL 幂等可重跑。
  services+=(backend agent-service migrate mcp mcp-migrate)
fi
if sync_repo "$FRONTEND_DIR" "前端"; then
  services+=(frontend)
fi

if [ ${#services[@]} -eq 0 ]; then
  echo "==> 全部无更新，跳过重建"
  exit 0
fi

echo "==> 重建并启动：${services[*]}"
cd "$DEPLOY_DIR"
"${DC[@]}" up -d --build "${services[@]}"
# nginx 在启动时解析一次 mcp 的容器 IP；mcp 被重建后 IP 可能变化，
# 不重启 frontend 会让 /mcp 打到旧地址。只在后端有更新时做，避免无谓重启。
if [ "$backend_changed" = true ]; then
  "${DC[@]}" restart frontend
fi
"${DC[@]}" ps
