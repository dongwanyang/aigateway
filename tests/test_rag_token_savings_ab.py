"""A/B 测试:代码知识库检索(RAG)是否减少输入 token(集成测试).

论点:有代码知识库后,大模型不用反复 read/sed 文件,净输入 token 更少。

A 组(关 RAG):模拟 agent 多轮 read —— 为回答一个代码问题,逐轮把相关源文件
             内容贴进 messages(模拟 read 工具把文件灌进上下文),累加各轮 prompt_tokens。
B 组(开 RAG):用户只问一句问题,gateway 自动注入精准代码上下文,单轮。记 prompt_tokens。

判定:B 组 prompt_tokens < A 组累加 prompt_tokens → 代码知识库减少了输入 token。

这是真实上游集成测试:需要运行中的 gateway(:8000)+ 可用 API key + 真实 provider 配额。
默认跳过,显式运行:

    RUN_RAG_AB=1 python3 -m pytest tests/test_rag_token_savings_ab.py -v -s

环境变量(可选):
    AIGATEWAY_URL / AIGATEWAY_ADMIN_KEY / AIGATEWAY_USER_KEY / AIGATEWAY_TEST_MODEL
    AIGATEWAY_REPO_ROOT(A 组读取源文件的仓库根,默认 /home/ubuntu/aigateway)

绕缓存:每个 prompt 带唯一随机后缀,保证不命中缓存(缓存命中时 prompt_tokens 是
       len//4 估算而非真实值,会污染数据)。
"""
import json
import os
import random
import string
import time
import urllib.request

import pytest

GATEWAY = os.environ.get("AIGATEWAY_URL", "http://localhost:8000")
# admin key 同时用于切插件和发请求;可用普通 key 替代 USER_KEY 以避开 admin 配额
ADMIN_KEY = os.environ.get("AIGATEWAY_ADMIN_KEY", "gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o")
USER_KEY = os.environ.get("AIGATEWAY_USER_KEY", ADMIN_KEY)
MODEL = os.environ.get("AIGATEWAY_TEST_MODEL", "agnes-2.0-flash")

CODE_QUESTION = "这个仓库的 dispatcher 里,三层缓存的 cache key 是由哪些字段拼接生成的?请具体说明。"

# A 组模拟 agent 为答这个问题需要 read 的文件(真实路径,内容逐轮灌进上下文)
FILES_TO_READ = [
    "aigateway-core/src/aigateway_core/dispatch/dispatcher.py",
    "aigateway-core/src/aigateway_core/prefix/cache/cache_keys/__init__.py",
    "aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py",
]

# 默认跳过:此测试会切插件配置 + 打真实上游 + 消耗真实 token/配额。
# 显式启用:RUN_RAG_AB=1 python3 -m pytest tests/test_rag_token_savings_ab.py -v -s
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_RAG_AB"),
    reason="集成测试:需运行中的 gateway + 真实 key/配额。设 RUN_RAG_AB=1 启用。",
)


def _rand() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def _req(path, method="GET", body=None, key=USER_KEY, timeout=120):
    headers = {"Authorization": f"Bearer {key}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{GATEWAY}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:300]}


def _set_rag(enabled: bool) -> None:
    """热切换 rag_retriever,轮询 health 确认生效。"""
    _req("/admin/plugins-config", "PUT", {"name": "rag_retriever", "enabled": enabled}, key=ADMIN_KEY)
    for _ in range(10):
        time.sleep(1)
        h = _req("/health")
        st = h["data"]["plugins"].get("rag_retriever", {})
        if st.get("enabled") == enabled:
            return
    raise RuntimeError(f"rag_retriever 未切换到 enabled={enabled}")


def _chat(messages) -> dict:
    """发一次 chat completion,返回 {prompt_tokens, completion_tokens, total_tokens, text, error}."""
    body = {"model": MODEL, "messages": messages, "max_tokens": 200, "temperature": 0.2}
    d = _req("/v1/chat/completions", "POST", body)
    if "_http_error" in d:
        return {"error": f"HTTP {d['_http_error']}: {d['_body']}", "prompt_tokens": 0}
    if "error" in d:
        return {"error": d["error"], "prompt_tokens": 0}
    usage = d.get("usage", {})
    text = d.get("choices", [{}])[0].get("message", {}).get("content", "")
    return {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "text": text[:120],
    }


@pytest.fixture
def restore_rag():
    """测试结束无论成败都恢复 rag_retriever=enabled."""
    yield
    try:
        _set_rag(True)
    except Exception:
        pass


def test_rag_reduces_input_tokens(restore_rag):
    """A/B:开 RAG 单轮注入 vs 关 RAG 多轮 read,对比净输入 token."""
    # --- B 组:开 RAG,用户只问问题 ---
    _set_rag(True)
    b = _chat([{"role": "user", "content": f"{CODE_QUESTION}\n[trace:{_rand()}]"}])
    assert "error" not in b, f"B 组请求失败(可能配额超限): {b.get('error')}"
    assert b["prompt_tokens"] > 0, f"B 组 prompt_tokens=0,可能命中缓存或上游无 usage: {b}"
    print(f"\n[B 组 开RAG] prompt_tokens={b['prompt_tokens']}  total={b['total_tokens']}")
    print(f"  回答预览: {b['text']}")

    # --- A 组:关 RAG,模拟 agent 多轮 read ---
    _set_rag(False)
    msgs = [{"role": "user", "content": f"{CODE_QUESTION}\n[trace:{_rand()}]"}]
    cumulative_prompt = 0
    rounds = []
    for i, fp in enumerate(FILES_TO_READ, 1):
        repo_root = os.environ.get("AIGATEWAY_REPO_ROOT", "/home/ubuntu/aigateway")
        try:
            with open(os.path.join(repo_root, fp), "r", errors="ignore") as f:
                content = f.read()
        except FileNotFoundError:
            content = f"(文件 {fp} 不存在)"
        msgs.append({"role": "assistant", "content": f"我读一下 {fp}。"})
        msgs.append({"role": "user", "content": f"这是 {fp} 的内容:\n```\n{content}\n```\n继续。[trace:{_rand()}]"})
        r = _chat(msgs)
        assert "error" not in r, f"A 组轮{i}请求失败: {r.get('error')}"
        rounds.append({"file": fp, "chars": len(content), **r})
        cumulative_prompt += r["prompt_tokens"]
        print(f"  [A 组轮{i}] read {fp} ({len(content)} chars) -> prompt={r['prompt_tokens']}  累加={cumulative_prompt}")

    saved = cumulative_prompt - b["prompt_tokens"]
    pct = (saved / cumulative_prompt * 100) if cumulative_prompt else 0

    print(f"\n=== A/B 结果 ===")
    print(f"A 组(关RAG,多轮read)累计 prompt_tokens: {cumulative_prompt}")
    print(f"B 组(开RAG,单轮注入)      prompt_tokens: {b['prompt_tokens']}")
    print(f"净节省输入 token: {saved}  ({pct:.1f}%)")

    # 断言:开 RAG 应比模拟多轮 read 省输入 token
    assert saved > 0, f"未观察到 token 减少(净节省 {saved}):代码知识库未体现省 token 收益"
