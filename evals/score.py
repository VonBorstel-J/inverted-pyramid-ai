"""Write eval scores back into runs/routing.jsonl `quality` (ARCHITECTURE.md
section 6). An eval notes the log's line count before and after each model
call (`mark()`), so it knows exactly which lines that call appended; after
scoring it fills `quality` on those lines (task must match, as a guard)."""
import json
import os
import tempfile

from llm import log


def mark():
    """Current number of lines in the routing log."""
    p = log.log_path()
    if not p.exists():
        return 0
    with open(p, "rb") as f:
        return sum(1 for _ in f)


class Span:
    """with Span() as s: <calls>  ->  s.lines is the range of appended lines."""
    def __enter__(self):
        self.start = mark()
        return self

    def __exit__(self, *a):
        self.lines = range(self.start, mark())
        return False


def apply(assignments, path=None):
    """assignments: [(line_index, task_or_prefix, quality)]. Sets `quality` on
    each line whose task starts with the given task. Returns lines updated."""
    p = path or log.log_path()
    if not assignments or not os.path.exists(p):
        return 0
    with open(p, encoding="utf-8") as f:
        lines = f.read().splitlines()
    n = 0
    for i, task, q in assignments:
        if q is None or not (0 <= i < len(lines)):
            continue
        try:
            row = json.loads(lines[i])
        except json.JSONDecodeError:
            continue
        if not str(row.get("task", "")).startswith(task):
            continue
        row["quality"] = round(float(q), 4)
        lines[i] = json.dumps(row)
        n += 1
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(str(p)), prefix=".routing.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, p)
    return n
