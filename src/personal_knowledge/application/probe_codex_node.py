"""压测 codex-for.me 节点的实际并发上限、延迟、错误率。

梯度并发测试:1 / 2 / 4 / 8 路,每档发 N 个短请求,统计:
- 成功率
- 平均/P50/P95 延迟
- 实际吞吐 QPS
- 错误类型分布(429/超时/其他)

用法:
  set OPENAI_API_KEY=clp_xxx
  set OPENAI_BASE_URL=https://api-vip.codex-for.me/v1
  python -m personal_knowledge.application.probe_codex_node --model gpt-5.4 --per-level 8
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

CONCURRENCY_LEVELS = [1, 2, 4, 8]


def make_client():
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("[error] 未安装 openai 库")
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    if not api_key or not base_url:
        sys.exit("[error] 需设置 OPENAI_API_KEY 和 OPENAI_BASE_URL")
    # 显式注入代理 + 伪装 UA(openai 库默认带 X-Stainless 指纹头,
    # 部分中转站据此拦截,改 UA 为 curl 绕过)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    kw = {"base_url": base_url, "api_key": api_key, "timeout": 60,
          "default_headers": {"User-Agent": "curl/8.0"}}
    if proxy:
        import httpx
        kw["http_client"] = httpx.Client(
            proxy=proxy, timeout=60, headers={"User-Agent": "curl/8.0"})
        print(f"[config] 走代理 {proxy}")
    return OpenAI(**kw)


def one_call(client, model: str, idx: int) -> dict:
    """发一个最短请求,返回延迟和结果。"""
    prompt = f"回复一个字:好(测试 #{idx})"
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=5,
        )
        dt = time.time() - t0
        content = (resp.choices[0].message.content or "").strip()
        # 探测是否套壳:返回模型名
        ret_model = getattr(resp, "model", "?")
        return {"ok": True, "lat": dt, "content": content, "model": ret_model}
    except Exception as exc:
        dt = time.time() - t0
        name = type(exc).__name__
        msg = str(exc)[:120]
        return {"ok": False, "lat": dt, "err_type": name, "err_msg": msg}


def run_level(client, model: str, workers: int, n: int) -> dict:
    print(f"\n=== 并发 {workers} 路,发 {n} 个请求 ===")
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one_call, client, model, i) for i in range(n)]
        for f in as_completed(futs):
            results.append(f.result())
    wall = time.time() - t0

    oks = [r for r in results if r["ok"]]
    fails = [r for r in results if not r["ok"]]
    lats = [r["lat"] for r in oks]
    qps = len(oks) / wall if wall > 0 else 0

    summary = {
        "workers": workers,
        "n": n,
        "wall_s": round(wall, 2),
        "ok": len(oks),
        "fail": len(fails),
        "success_rate": round(len(oks) / n * 100, 1) if n else 0,
        "qps": round(qps, 2),
    }
    if lats:
        lats_sorted = sorted(lats)
        summary.update({
            "lat_avg": round(statistics.mean(lats), 2),
            "lat_p50": round(lats_sorted[len(lats_sorted) // 2], 2),
            "lat_p95": round(lats_sorted[int(len(lats_sorted) * 0.95)], 2),
            "lat_max": round(max(lats), 2),
        })
    if oks:
        summary["returned_model"] = oks[0].get("model")
        summary["sample_content"] = oks[0].get("content", "")[:30]
    if fails:
        # 错误类型分布
        from collections import Counter
        err_dist = Counter(r["err_type"] for r in fails)
        summary["err_dist"] = dict(err_dist)
        summary["err_sample"] = fails[0]["err_msg"]

    print(f"  耗时 {summary['wall_s']}s | 成功 {summary['ok']}/{n} "
          f"({summary['success_rate']}%) | QPS {summary['qps']}")
    if lats:
        print(f"  延迟 avg {summary['lat_avg']}s p50 {summary['lat_p50']}s "
              f"p95 {summary['lat_p95']}s max {summary['lat_max']}s")
    if oks:
        print(f"  返回模型名: {summary['returned_model']} | 样本输出: {summary['sample_content']!r}")
    if fails:
        print(f"  错误分布: {summary['err_dist']} | 样本: {summary['err_sample']}")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-5.4")
    ap.add_argument("--per-level", type=int, default=8,
                    help="每个并发档发多少请求")
    ap.add_argument("--levels", type=int, nargs="+",
                    default=CONCURRENCY_LEVELS,
                    help="并发档位,默认 1 2 4 8")
    args = ap.parse_args()

    client = make_client()
    print(f"[config] base_url={os.environ['OPENAI_BASE_URL']}")
    print(f"[config] model={args.model}")
    print(f"[config] key 前缀={os.environ['OPENAI_API_KEY'][:8]}...")

    # 先单个预热,确认连通 + 模型真实性
    print("\n--- 预热:单个请求 ---")
    warm = one_call(client, args.model, 0)
    if not warm["ok"]:
        print(f"[FAIL] 连不通: {warm['err_type']} {warm['err_msg']}")
        sys.exit(1)
    print(f"  OK | 延迟 {warm['lat']:.2f}s | 返回模型 {warm['model']} | 输出 {warm['content']!r}")

    all_summary = []
    for w in args.levels:
        s = run_level(client, args.model, w, args.per_level)
        all_summary.append(s)
        # 失败率过高就提前停,避免无意义压测
        if s["fail"] > args.per_level * 0.5:
            print(f"  [stop] 失败率>{50}%,停止更高并发测试")
            break
        time.sleep(2)  # 档位间冷静期

    print("\n=== 汇总 ===")
    print(f"{'workers':<8}{'ok/n':<10}{'rate%':<8}{'QPS':<8}{'p50':<8}{'p95':<8}")
    for s in all_summary:
        p50 = s.get("lat_p50", "-")
        p95 = s.get("lat_p95", "-")
        print(f"{s['workers']:<8}{s['ok']}/{s['n']:<8}{s['success_rate']:<8}"
              f"{s['qps']:<8}{p50}{'':<4}{p95}")

    # 给出推荐 workers
    ok_levels = [s for s in all_summary if s["success_rate"] == 100]
    if ok_levels:
        rec = max(ok_levels, key=lambda x: x["workers"])
        print(f"\n[推荐] 最高可用并发: workers={rec['workers']} "
              f"(QPS {rec['qps']}, 成功率 100%)")
    else:
        # 退而取成功率最高的
        best = max(all_summary, key=lambda x: (x["success_rate"], x["qps"]))
        print(f"\n[推荐] 无 100% 成功档,最佳: workers={best['workers']} "
              f"(成功率 {best['success_rate']}%)")


if __name__ == "__main__":
    main()
