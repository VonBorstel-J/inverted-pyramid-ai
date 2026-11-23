"""Provider back-ends. Each takes (prompt, model, json_mode, **opts) and returns
(text, meta) where meta may carry tokens_in / tokens_out / model."""
import json
import os
import subprocess
import tempfile

DEFAULT_LOCAL_URL = os.environ.get("IPA_LOCAL_URL", "http://127.0.0.1:8091/v1")
DEFAULT_MODELS = {
    "rules": "rules",
    "local": os.environ.get("IPA_LOCAL_MODEL", "qwen3-4b-instruct-2507-q4_0"),
    "claude": os.environ.get("IPA_CLAUDE_MODEL", "haiku"),
    "codex": os.environ.get("IPA_CODEX_MODEL", ""),
    "gemini": os.environ.get("IPA_GEMINI_MODEL", ""),
}
TIMEOUT = int(os.environ.get("IPA_LLM_TIMEOUT", "300"))


class ProviderError(RuntimeError):
    pass


def _run(cmd, stdin_text, timeout):
    try:
        p = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise ProviderError(f"{cmd[0]} not installed") from e
    except subprocess.TimeoutExpired as e:
        raise ProviderError(f"{cmd[0]} timed out after {timeout}s") from e
    if p.returncode != 0:
        raise ProviderError(f"{cmd[0]} exit {p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    return p.stdout


def local(prompt, model, json_mode=False, url=None, timeout=TIMEOUT, **_):
    import requests
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "user", "content": prompt}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        r = requests.post((url or DEFAULT_LOCAL_URL).rstrip("/") + "/chat/completions",
                          json=body, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise ProviderError(f"local: {e}") from e
    d = r.json()
    u = d.get("usage") or {}
    return d["choices"][0]["message"]["content"], {
        "tokens_in": u.get("prompt_tokens"), "tokens_out": u.get("completion_tokens"),
        "model": d.get("model") or model}


def claude(prompt, model, json_mode=False, timeout=TIMEOUT, **_):
    cmd = ["claude", "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    out = _run(cmd, prompt, timeout)
    try:
        d = json.loads(out)
    except json.JSONDecodeError as e:
        raise ProviderError(f"claude: non-JSON envelope: {out[:200]}") from e
    if d.get("is_error"):
        raise ProviderError(f"claude: {d.get('result')}")
    u = d.get("usage") or {}
    return d.get("result", ""), {"tokens_in": u.get("input_tokens"), "tokens_out": u.get("output_tokens")}


def codex(prompt, model, json_mode=False, timeout=TIMEOUT, **_):
    with tempfile.TemporaryDirectory() as td:
        last = os.path.join(td, "last.txt")
        cmd = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
               "--ephemeral", "-o", last]
        if model:
            cmd += ["-m", model]
        cmd.append("-")
        out = _run(cmd, prompt, timeout)
        if os.path.exists(last):
            with open(last, encoding="utf-8") as f:
                return f.read(), {}
        return out, {}


def gemini(prompt, model, json_mode=False, timeout=TIMEOUT, **_):
    cmd = ["gemini"]
    if model:
        cmd += ["-m", model]
    cmd += ["-p", prompt]
    return _run(cmd, None, timeout), {}


PROVIDERS = {"local": local, "claude": claude, "codex": codex, "gemini": gemini}
