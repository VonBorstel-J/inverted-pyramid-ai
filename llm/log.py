"""Routing log: one JSON line per model call (section 6)."""
import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def log_path():
    return Path(os.environ.get("IPA_ROUTING_LOG", REPO / "runs" / "routing.jsonl"))


def append(task, provider, model, ms, input_chars, output_chars, ok, error=None,
           tokens_in=None, tokens_out=None, quality=None):
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "task": task, "provider": provider, "model": model,
        "ms": round(ms, 1), "input_chars": input_chars, "output_chars": output_chars,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "ok": bool(ok), "error": error, "quality": quality,
    }
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return row


def read(path=None):
    p = Path(path) if path else log_path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows
