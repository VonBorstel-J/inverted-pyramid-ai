"""`python -m llm.report`: per task/provider summary of runs/routing.jsonl."""
import argparse
import statistics
from collections import defaultdict

from llm import log


def summarize(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r.get("task"), r.get("provider"))].append(r)
    out = []
    for (task, provider), rs in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        ms = [r["ms"] for r in rs if r.get("ms") is not None]
        q = [r["quality"] for r in rs if r.get("quality") is not None]
        out.append({
            "task": task, "provider": provider, "count": len(rs),
            "median_ms": round(statistics.median(ms), 1) if ms else None,
            "success_rate": round(sum(1 for r in rs if r.get("ok")) / len(rs), 3),
            "quality": round(statistics.mean(q), 3) if q else None,
        })
    return out


def format_table(summary):
    head = f"{'task':<32} {'provider':<8} {'count':>6} {'median_ms':>10} {'success':>8} {'quality':>8}"
    lines = [head, "-" * len(head)]
    for s in summary:
        lines.append(f"{str(s['task']):<32} {str(s['provider']):<8} {s['count']:>6} "
                     f"{'' if s['median_ms'] is None else s['median_ms']:>10} "
                     f"{s['success_rate']:>8} {'' if s['quality'] is None else s['quality']:>8}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=None, help="routing log path (default runs/routing.jsonl)")
    a = ap.parse_args(argv)
    rows = log.read(a.log)
    if not rows:
        print("no routing log rows yet")
        return 0
    print(format_table(summarize(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
