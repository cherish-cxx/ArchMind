"""P4.1 验收 —— Context 是否真落进了 Redis，且跨 Python 重启存活。

用法（在 agent-service 目录下跑）：

    python scripts/verify_p4_1.py step1     # 发第一轮，记下 conversationId
    #  ← 手动 Ctrl-C 停掉 uvicorn，再重新起
    python scripts/verify_p4_1.py step2     # 用同一个 conversationId 再问一句
    python scripts/verify_p4_1.py step3     # evict 清理

为什么要写成脚本而不是 curl：Windows 的 PowerShell 里 `curl` 是 `Invoke-WebRequest`
的别名，语法不一样；而且 `Invoke-RestMethod` 发 `-Body` 默认按 ISO-8859-1 编码，
中文会被搅乱。脚本把这两坑一次绕开。

输出里 [OK] / [!!] 是判定；用 ASCII 标记是因为 Windows 控制台默认 GBK，
打 ✓ 会直接 UnicodeEncodeError。
"""

import json
import os
import pathlib
import sys

import httpx
import redis

AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8000")
TOKEN = os.getenv("INTERNAL_TOKEN", "dev-internal-token-change-me")
REDIS_HOST = os.getenv("REDIS_HOST", "172.21.160.203")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "1"))
PROJECT_ID = int(os.getenv("PROJECT_ID", "2101954967776358402"))
FOCUS_UID = os.getenv("FOCUS_UID", "com.hmall.trade.controller.OrderController")

_STATE = pathlib.Path(__file__).with_name(".verify_p4_1_state.json")

# trust_env=False 是必须的：本机 7897 是注册表里的系统代理，httpx 默认会把
# 连 localhost 的请求也丢给它 —— 表现成超时，极难排查（见 java_client.py 的注释）
_http = httpx.Client(
    base_url=AGENT_URL,
    timeout=40.0,
    trust_env=False,
    headers={"X-Internal-Token": TOKEN},
)
_rds = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)


def _ask(payload: dict) -> dict:
    resp = _http.post("/internal/agent/ask", json=payload)
    if resp.status_code != 200:
        print(f"[!!] /ask 返回 HTTP {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)
    return resp.json()


def step1() -> None:
    print("== step1：发第一轮，看 Context 有没有落 Redis ==\n")
    body = _ask(
        {
            "projectId": PROJECT_ID,
            "message": "这个项目是干嘛的",
            "focus": {"type": "CLASS", "uid": FOCUS_UID},
        }
    )
    cid = body["conversationId"]
    _STATE.write_text(json.dumps({"conversationId": cid}), encoding="utf-8")

    print(f"   conversationId = {cid}")
    print(f"   intent         = {body['meta']['intent']}")
    print(f"   message        = {body['message'][:60]}...\n")

    key = f"agent:ctx:{PROJECT_ID}:{cid}"
    if not _rds.exists(key):
        print(f"[!!] Redis 里没有 {key}")
        print("     → 说明 store.save 没执行。查 agent.py 的 ask() 里那行 await store.save(ctx)")
        sys.exit(1)

    ttl = _rds.ttl(key)
    raw = json.loads(_rds.get(key))
    print(f"[OK] key 存在：{key}")
    print(f"     ttl = {ttl} 秒（期望接近 7200）")
    print(f"     存了 history {len(raw.get('history', []))} 条、focusStack {len(raw.get('focusStack', []))} 项")
    print(f"     当前 focus = {raw.get('focus')}")

    if ttl < 0:
        print("[!!] ttl < 0 → -1 表示没设过期（ex= 没生效），-2 表示 key 不存在")
        sys.exit(1)

    print("\n>>> 现在 Ctrl-C 停掉 uvicorn，重新起，然后跑 step2")


def step2() -> None:
    print("== step2：重启后，用同一个 conversationId 再问一句（故意不传 focus）==\n")
    if not _STATE.exists():
        print("[!!] 找不到 state 文件，先跑 step1")
        sys.exit(1)
    cid = json.loads(_STATE.read_text(encoding="utf-8"))["conversationId"]

    key = f"agent:ctx:{PROJECT_ID}:{cid}"
    if not _rds.exists(key):
        print(f"[!!] 重启后 Redis 里 key 没了：{key}")
        sys.exit(1)

    before = json.loads(_rds.get(key)).get("focus")
    print(f"   重启后 Redis 里的 focus = {before}")
    if before is None:
        print("[!!] step1 存的 focus 本就不在，先回去查 step1")
        sys.exit(1)

    body = _ask(
        {
            "projectId": PROJECT_ID,
            "conversationId": cid,          # 回传，不新建
            "message": "它的调用链是什么样的",
            # 刻意不传 focus —— 焦点只能来自 Redis 里那份 Context
        }
    )
    print(f"   本轮 message     = {body['message'][:70]}")
    print(f"   本轮 suggestions = {[s['label'] for s in body.get('suggestions', [])]}\n")

    # ★ 主判据：再看一次 Redis 里的 focus。
    #   这一轮没传 focus，所以它还能在，只可能来自「load 读回了 step1 存的那份 Context」。
    #   如果 load 读失败（当成新会话），写回的 focus 就是 None —— 这个判据
    #   不依赖 Java 起没起，也不依赖 LLM 怎么分类，是确定性的。
    after = json.loads(_rds.get(key)).get("focus")
    print(f"   本轮结束后 Redis 里的 focus = {after}")
    if after is not None:
        print("\n[OK] focus 跨重启存活 → Context 真的从 Redis 读回来了（P4.1 验收通过）")
    else:
        print("\n[!!] focus 变成 None → 重启后被当成新会话，load 没读回 step1 那份")
        print("     → 查 store.load 的 key 拼接，或确认两个进程连的是同一个 Redis db")


def step3() -> None:
    print("== step3：evict 清干净 ==\n")
    resp = _http.post("/internal/agent/admin/evict", json={"projectId": PROJECT_ID})
    print(f"   evict 返回 = {resp.json()}")

    left = _rds.keys(f"agent:ctx:{PROJECT_ID}:*")
    if left:
        print(f"[!!] 还剩 {len(left)} 个 key：{left}")
        sys.exit(1)
    print("[OK] 该项目下的 Context 已清空")
    _STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    steps = {"step1": step1, "step2": step2, "step3": step3}
    if len(sys.argv) != 2 or sys.argv[1] not in steps:
        print(__doc__)
        sys.exit(1)
    steps[sys.argv[1]]()
