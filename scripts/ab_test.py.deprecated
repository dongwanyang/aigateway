#!/usr/bin/env python3
"""
AI Gateway A/B Test & Load Benchmark
=====================================

用途：对同一批 prompt，分别在"关闭缓存/压缩"（baseline）和"开启所有优化"（optimized）
两种模式下压测 Gateway，量化 token / 延迟 / 成本节省。

用法：
    python scripts/ab_test.py \
        --base-url http://localhost:8000 \
        --api-key gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o \
        --model deepseek-v4-flash \
        --concurrency 32 --requests 300 \
        --scenario mixed \
        --output docs/qa-evidence-$(date +%Y%m%d)

场景 (--scenario)：
    exact     50% 完全重复（打 L1/L2）
    semantic  50% 近义改写（打 L3，需要 Qdrant）
    mixed     混合上述两种（默认）
    random    全新 prompt（对照组，无缓存机会）
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import httpx


# --------------------------------------------------------------------------
# Prompt 池
# --------------------------------------------------------------------------

# 20 条基础 prompt —— 覆盖问答/代码/摘要/闲聊，模拟真实业务分布。
BASE_PROMPTS = [
    "用一句话解释什么是缓存",
    "Python 中列表和元组的区别是什么",
    "简要介绍 HTTP/2 相比 HTTP/1.1 的改进",
    "写一个 3 行的 Python 函数计算斐波那契数列第 n 项",
    "解释一下什么是 CAP 定理",
    "TCP 三次握手的过程是什么",
    "介绍一下 RESTful API 的 6 个约束",
    "什么是分布式系统中的最终一致性",
    "React 的 useState 和 useReducer 什么时候用哪个",
    "SQL 中 JOIN 和子查询的性能差异",
    "什么是 CI/CD，各自解决什么问题",
    "Docker 和虚拟机的核心区别是什么",
    "解释一下什么是零信任安全模型",
    "写一句问候语",
    "介绍一下 Rust 的所有权系统",
    "什么是 CSRF 攻击，如何防范",
    "解释 Kubernetes 中 Pod、Deployment、Service 的关系",
    "gRPC 和 REST 的适用场景对比",
    "什么是幂等接口，怎么设计",
    "简述数据库索引的 B+ 树结构",
]

# 每条基础 prompt 对应一条"改写"（同义），用于测语义缓存。
# 语义等价但字面不同 —— 期望 L3 命中。
SEMANTIC_REWRITES = [
    "什么叫做缓存？简单说说",
    "元组和列表在 Python 里到底有啥不一样",
    "HTTP/2 相比 HTTP/1.1 主要好在哪",
    "帮我写个求斐波那契的 Python 函数，3 行搞定",
    "CAP 定理是什么意思，展开讲一下",
    "TCP 建立连接时候的三次握手是怎么走的",
    "REST 风格 API 有哪 6 条约束，说说看",
    "分布式系统里最终一致性是个什么概念",
    "React 里 useState 跟 useReducer 分别啥时候用",
    "SQL 里用 JOIN 和子查询哪个性能好",
    "CI 是什么？CD 又是什么？分别解决哪些问题",
    "Docker 容器和虚拟机比起来核心不同点在哪",
    "零信任安全模型是啥",
    "跟我打个招呼",
    "Rust 的 ownership 机制怎么理解",
    "CSRF 是什么攻击，一般怎么防",
    "K8s 里的 Pod、Deployment 和 Service 各自是啥关系",
    "gRPC 和 REST 各自适合什么场景",
    "什么是幂等的接口，实践中怎么设计出来",
    "数据库索引底层的 B+ 树长什么样",
]

# 随机 prompt 用来做无缓存对照 —— 拼组合词随机生成。
RANDOM_WORDS = ["蓝色", "编程", "算法", "架构", "云计算", "网络", "数据", "安全", "并发", "存储"]


def make_prompt_stream(
    scenario: str, total: int, repeat_ratio: float, seed: int = 42
) -> list[str]:
    """按场景生成 total 条 prompt。repeat_ratio 控制重复/命中比例。"""
    rng = random.Random(seed)
    out: list[str] = []
    for _ in range(total):
        r = rng.random()
        if scenario == "exact":
            # 高 ratio → 重复；低 → 随机
            if r < repeat_ratio:
                out.append(rng.choice(BASE_PROMPTS))
            else:
                out.append(_random_prompt(rng))
        elif scenario == "semantic":
            if r < repeat_ratio:
                # 先塞原句一次 → 再塞对应改写去命中
                idx = rng.randrange(len(BASE_PROMPTS))
                if rng.random() < 0.5:
                    out.append(BASE_PROMPTS[idx])
                else:
                    out.append(SEMANTIC_REWRITES[idx])
            else:
                out.append(_random_prompt(rng))
        elif scenario == "mixed":
            if r < repeat_ratio * 0.6:
                out.append(rng.choice(BASE_PROMPTS))  # 精确命中
            elif r < repeat_ratio:
                idx = rng.randrange(len(BASE_PROMPTS))
                out.append(SEMANTIC_REWRITES[idx])  # 语义命中
            else:
                out.append(_random_prompt(rng))
        elif scenario == "random":
            out.append(_random_prompt(rng))
        else:
            raise ValueError(f"unknown scenario: {scenario}")
    return out


def _random_prompt(rng: random.Random) -> str:
    a, b, c = rng.sample(RANDOM_WORDS, 3)
    return f"用一句话说说 {a}、{b} 和 {c} 之间有什么关系"


# --------------------------------------------------------------------------
# 指标收集
# --------------------------------------------------------------------------


@dataclass
class Sample:
    ok: bool
    status: int
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit: bool = False
    cache_tier: str = ""
    error: str = ""


@dataclass
class GroupStats:
    label: str
    n: int
    ok: int
    fail: int
    latency_p50: float
    latency_p95: float
    latency_p99: float
    latency_avg: float
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    cache_hits: int
    cache_hit_rate: float
    tier_l1: int
    tier_l2: int
    tier_l3: int
    est_cost_usd: float
    samples: list[Sample] = field(default_factory=list, repr=False)


def compute_stats(label: str, samples: list[Sample], price_prompt: float, price_completion: float) -> GroupStats:
    ok_samples = [s for s in samples if s.ok]
    lats = [s.latency_ms for s in ok_samples] or [0.0]
    total_p = sum(s.prompt_tokens for s in ok_samples)
    total_c = sum(s.completion_tokens for s in ok_samples)
    total_t = sum(s.total_tokens for s in ok_samples)
    hits = sum(1 for s in ok_samples if s.cache_hit)
    tier_counts = {"L1": 0, "L2": 0, "L3": 0}
    for s in ok_samples:
        if s.cache_tier in tier_counts:
            tier_counts[s.cache_tier] += 1

    # 只对未命中缓存的部分计费（缓存命中不消耗上游 token）
    billable = [s for s in ok_samples if not s.cache_hit]
    cost = sum(
        s.prompt_tokens / 1000 * price_prompt + s.completion_tokens / 1000 * price_completion
        for s in billable
    )

    return GroupStats(
        label=label,
        n=len(samples),
        ok=len(ok_samples),
        fail=len(samples) - len(ok_samples),
        latency_p50=_pct(lats, 50),
        latency_p95=_pct(lats, 95),
        latency_p99=_pct(lats, 99),
        latency_avg=statistics.mean(lats),
        total_prompt_tokens=total_p,
        total_completion_tokens=total_c,
        total_tokens=total_t,
        cache_hits=hits,
        cache_hit_rate=hits / len(ok_samples) if ok_samples else 0.0,
        tier_l1=tier_counts["L1"],
        tier_l2=tier_counts["L2"],
        tier_l3=tier_counts["L3"],
        est_cost_usd=cost,
        samples=samples,
    )


def _pct(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


# --------------------------------------------------------------------------
# Gateway 交互
# --------------------------------------------------------------------------


PLUGINS_TO_TOGGLE = ["prompt_cache", "semantic_cache", "prompt_compress"]


async def set_plugins(client: httpx.AsyncClient, headers: dict, enabled: bool, verbose: bool) -> None:
    """打开或关闭 3 个优化插件（一次一个，走 PUT /admin/plugins-config）。"""
    for name in PLUGINS_TO_TOGGLE:
        r = await client.put(
            "/admin/plugins-config", headers=headers, json={"name": name, "enabled": enabled}
        )
        if verbose:
            print(f"  set {name} enabled={enabled} → HTTP {r.status_code}")
        if r.status_code >= 400:
            raise RuntimeError(f"failed to toggle plugin {name}: {r.status_code} {r.text[:200]}")
    # 给热重载一点时间生效
    await asyncio.sleep(1.0)


async def flush_cache(client: httpx.AsyncClient, headers: dict) -> None:
    """清 L1/L2/L3 的入口：L3 走 admin API，L1/L2 靠重启进程；这里只清 L3。
    L1/L2 我们在 baseline 里通过 disable prompt_cache 避免它们污染。"""
    try:
        await client.post("/admin/cache/l3/cleanup?force=true", headers=headers, timeout=15)
    except Exception as e:
        print(f"  (L3 cleanup skipped: {e})", file=sys.stderr)


async def one_request(
    client: httpx.AsyncClient, headers: dict, model: str, prompt: str, streaming: bool
) -> Sample:
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if streaming:
        payload["stream"] = True

    t0 = time.perf_counter()
    try:
        if streaming:
            prompt_toks = comp_toks = total_toks = 0
            async with client.stream("POST", "/v1/chat/completions", headers=headers, json=payload) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "ignore")
                    return Sample(False, r.status_code, (time.perf_counter() - t0) * 1000, error=body[:200])
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[6:]
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                        u = obj.get("usage") or {}
                        prompt_toks = u.get("prompt_tokens", prompt_toks)
                        comp_toks = u.get("completion_tokens", comp_toks)
                        total_toks = u.get("total_tokens", total_toks)
                    except Exception:
                        pass
            latency = (time.perf_counter() - t0) * 1000
            return Sample(True, 200, latency, prompt_toks, comp_toks, total_toks)
        else:
            r = await client.post("/v1/chat/completions", headers=headers, json=payload)
            latency = (time.perf_counter() - t0) * 1000
            if r.status_code != 200:
                return Sample(False, r.status_code, latency, error=r.text[:200])
            body = r.json().get("data", {})
            u = body.get("usage") or {}
            return Sample(
                True, 200, latency,
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
            )
    except Exception as e:
        return Sample(False, 0, (time.perf_counter() - t0) * 1000, error=str(e)[:200])


async def fetch_recent_logs(client: httpx.AsyncClient, headers: dict, limit: int) -> list[dict]:
    r = await client.get(f"/admin/logs?limit={limit}", headers=headers)
    if r.status_code != 200:
        return []
    return r.json().get("data", {}).get("items", [])


def match_cache_hits_from_logs(samples: list[Sample], logs: list[dict]) -> None:
    """response 本身不带 cache_hit 字段，从 /admin/logs 回填最近 len(samples) 条。
    日志按时间倒序，配对靠近的 request 采样（近似匹配）。"""
    # 只考虑 /v1/chat/completions 的日志
    chat_logs = [l for l in logs if l.get("endpoint", "").endswith("/chat/completions")][: len(samples)]
    # 时间反序 → 与 samples 顺序对齐（samples 也是时间顺序），只需 reversed
    chat_logs = list(reversed(chat_logs))
    n = min(len(samples), len(chat_logs))
    for i in range(n):
        # 从后往前对齐
        s = samples[len(samples) - n + i]
        l = chat_logs[i]
        s.cache_hit = bool(l.get("cache_hit"))
        s.cache_tier = l.get("tier") or ""


async def run_group(
    client: httpx.AsyncClient,
    headers: dict,
    label: str,
    prompts: list[str],
    concurrency: int,
    model: str,
    streaming: bool,
) -> list[Sample]:
    print(f"\n=== Running {label}: {len(prompts)} requests, concurrency={concurrency} ===")
    sem = asyncio.Semaphore(concurrency)
    samples: list[Sample] = [Sample(False, 0, 0.0)] * len(prompts)
    done = 0
    start = time.perf_counter()

    async def worker(i: int, p: str) -> None:
        nonlocal done
        async with sem:
            s = await one_request(client, headers, model, p, streaming)
        samples[i] = s
        done += 1
        if done % max(1, len(prompts) // 10) == 0 or done == len(prompts):
            elapsed = time.perf_counter() - start
            print(
                f"  [{label}] {done}/{len(prompts)}  "
                f"({done/elapsed:.1f} req/s, ok={sum(1 for x in samples if x.ok)})"
            )

    await asyncio.gather(*(worker(i, p) for i, p in enumerate(prompts)))
    return samples


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


def write_csv(path: Path, samples: list[Sample]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["idx", "ok", "status", "latency_ms", "prompt_tokens",
                    "completion_tokens", "total_tokens", "cache_hit", "cache_tier", "error"])
        for i, s in enumerate(samples):
            w.writerow([i, s.ok, s.status, f"{s.latency_ms:.1f}",
                        s.prompt_tokens, s.completion_tokens, s.total_tokens,
                        s.cache_hit, s.cache_tier, s.error])


def _pct_change(base: float, opt: float) -> str:
    if base == 0:
        return "N/A"
    delta = (opt - base) / base * 100
    return f"{delta:+.1f}%"


def _saving(base: float, opt: float) -> str:
    if base == 0:
        return "N/A"
    saved = (base - opt) / base * 100
    return f"{saved:+.1f}%"


def render_markdown(baseline: GroupStats, optimized: GroupStats, args) -> str:
    lines = []
    lines.append(f"# AI Gateway A/B 压测报告\n")
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- Base URL：`{args.base_url}`")
    lines.append(f"- 模型：`{args.model}`")
    lines.append(f"- 场景：`{args.scenario}` · 重复比例 `{args.repeat_ratio}` · 流式 `{args.streaming}`")
    lines.append(f"- 并发：{args.concurrency}  · 每组请求数：{args.requests}\n")

    lines.append("## 概览对比\n")
    lines.append("| 指标 | Baseline（关缓存/压缩） | Optimized（全开） | 变化 |")
    lines.append("|------|------------------------|------------------|------|")
    lines.append(f"| 成功率 | {baseline.ok}/{baseline.n} | {optimized.ok}/{optimized.n} | — |")
    lines.append(f"| 总 tokens | {baseline.total_tokens:,} | {optimized.total_tokens:,} | **节省 {_saving(baseline.total_tokens, optimized.total_tokens)}** |")
    lines.append(f"| prompt tokens | {baseline.total_prompt_tokens:,} | {optimized.total_prompt_tokens:,} | {_saving(baseline.total_prompt_tokens, optimized.total_prompt_tokens)} |")
    lines.append(f"| completion tokens | {baseline.total_completion_tokens:,} | {optimized.total_completion_tokens:,} | {_saving(baseline.total_completion_tokens, optimized.total_completion_tokens)} |")
    lines.append(f"| 估算成本 (USD) | ${baseline.est_cost_usd:.4f} | ${optimized.est_cost_usd:.4f} | **节省 {_saving(baseline.est_cost_usd, optimized.est_cost_usd)}** |")
    lines.append(f"| p50 延迟 | {baseline.latency_p50:.0f} ms | {optimized.latency_p50:.0f} ms | {_pct_change(baseline.latency_p50, optimized.latency_p50)} |")
    lines.append(f"| p95 延迟 | {baseline.latency_p95:.0f} ms | {optimized.latency_p95:.0f} ms | {_pct_change(baseline.latency_p95, optimized.latency_p95)} |")
    lines.append(f"| p99 延迟 | {baseline.latency_p99:.0f} ms | {optimized.latency_p99:.0f} ms | {_pct_change(baseline.latency_p99, optimized.latency_p99)} |")
    lines.append(f"| 缓存命中率 | — | {optimized.cache_hit_rate*100:.1f}% | +{optimized.cache_hits} 次 |")
    lines.append(f"| L1 命中 | — | {optimized.tier_l1} | |")
    lines.append(f"| L2 命中 | — | {optimized.tier_l2} | |")
    lines.append(f"| L3 命中 | — | {optimized.tier_l3} | |\n")

    lines.append("## 结论\n")
    if baseline.total_tokens > 0:
        saved_pct = (baseline.total_tokens - optimized.total_tokens) / baseline.total_tokens * 100
        if saved_pct > 30:
            verdict = f"✅ **Token 节省 {saved_pct:.1f}%**，满足预期（>30%）。"
        elif saved_pct > 5:
            verdict = f"⚠️ Token 节省 {saved_pct:.1f}%，低于预期。建议检查缓存命中率、场景配置。"
        else:
            verdict = f"❌ Token 几乎无节省（{saved_pct:.1f}%）。可能原因：缓存插件未启用/命中失败/prompt 完全随机。"
        lines.append(verdict + "\n")

    lines.append("## 原始数据\n")
    lines.append("- `baseline.csv` — 每条请求的 latency / tokens / cache 状态")
    lines.append("- `optimized.csv` — 同上")
    lines.append("- `summary.json` — 机器可读的汇总数字\n")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    prompts = make_prompt_stream(args.scenario, args.requests, args.repeat_ratio, args.seed)
    print(f"Generated {len(prompts)} prompts | scenario={args.scenario} repeat_ratio={args.repeat_ratio}")
    print(f"Unique prompts in stream: {len(set(prompts))}")

    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "Content-Type": "application/json",
    }

    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)
    timeout = httpx.Timeout(connect=10.0, read=args.timeout, write=30.0, pool=30.0)
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, limits=limits, timeout=timeout) as client:
        # 健康检查
        r = await client.get("/health")
        if r.status_code != 200:
            print(f"❌ Gateway /health returned HTTP {r.status_code}", file=sys.stderr)
            return 2
        health = r.json().get("data", {})
        print(f"Gateway health: redis={health.get('dependencies',{}).get('redis',{}).get('status')}, "
              f"qdrant={health.get('dependencies',{}).get('qdrant',{}).get('status')}")

        # === Baseline：关闭 prompt_cache / semantic_cache / prompt_compress ===
        print("\n>>> Disabling optimization plugins for baseline...")
        await set_plugins(client, headers, enabled=False, verbose=args.verbose)
        await flush_cache(client, headers)
        baseline_samples = await run_group(
            client, headers, "baseline", prompts, args.concurrency, args.model, args.streaming
        )

        # === Optimized：全部启用 ===
        print("\n>>> Enabling optimization plugins for optimized run...")
        await set_plugins(client, headers, enabled=True, verbose=args.verbose)
        await flush_cache(client, headers)
        optimized_samples = await run_group(
            client, headers, "optimized", prompts, args.concurrency, args.model, args.streaming
        )

        # 从 admin logs 回填 cache_hit
        print("\n>>> Fetching cache hit info from /admin/logs...")
        try:
            logs = await fetch_recent_logs(client, headers, limit=len(optimized_samples) * 2)
            match_cache_hits_from_logs(optimized_samples, logs)
        except Exception as e:
            print(f"  (log fetch failed: {e})", file=sys.stderr)

    # 计价（从 config 里读，或用参数）
    price_p = args.price_prompt
    price_c = args.price_completion
    baseline = compute_stats("baseline", baseline_samples, price_p, price_c)
    optimized = compute_stats("optimized", optimized_samples, price_p, price_c)

    # 输出
    write_csv(output_dir / "baseline.csv", baseline_samples)
    write_csv(output_dir / "optimized.csv", optimized_samples)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {"baseline": {k: v for k, v in asdict(baseline).items() if k != "samples"},
             "optimized": {k: v for k, v in asdict(optimized).items() if k != "samples"},
             "args": vars(args)},
            ensure_ascii=False, indent=2
        ), encoding="utf-8"
    )
    md = render_markdown(baseline, optimized, args)
    (output_dir / "ab-report.md").write_text(md, encoding="utf-8")

    print(f"\n✅ Report written to {output_dir.resolve()}")
    print("\n" + "=" * 70)
    print(md.split("## 原始数据")[0])
    print("=" * 70)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI Gateway A/B token savings benchmark")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--api-key", default="gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o")
    p.add_argument("--model", default="deepseek-v4-flash")
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--requests", type=int, default=200)
    p.add_argument("--scenario", choices=["exact", "semantic", "mixed", "random"], default="mixed")
    p.add_argument("--repeat-ratio", type=float, default=0.5, help="重复/命中比例 [0,1]")
    p.add_argument("--streaming", action="store_true", help="使用 SSE 流式接口")
    p.add_argument("--timeout", type=float, default=120.0, help="单请求读超时（秒）")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--price-prompt", type=float, default=0.02, help="USD per 1K prompt tokens")
    p.add_argument("--price-completion", type=float, default=1.0, help="USD per 1K completion tokens")
    p.add_argument("--output", default="docs/qa-evidence-ab", help="输出目录")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    try:
        rc = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
