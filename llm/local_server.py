"""Start / stop a llama.cpp `llama-server` for a GGUF, with a health check and
registration in the phone's janitor ledger (best effort).

    python -m llm.local_server start --model chat  [--port 8091]
    python -m llm.local_server start --model embed [--port 8092]
    python -m llm.local_server health --port 8091
    python -m llm.local_server stop --port 8091
    python -m llm.local_server smoke --model chat   # start, 1 request, stop
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BINARY = os.environ.get("IPA_LLAMA_SERVER", "llama-server")
MODELS = {
    "chat": os.environ.get("IPA_CHAT_GGUF", "models/Qwen3-4B-Instruct-2507-Q4_0.gguf"),
    "embed": os.environ.get("IPA_EMBED_GGUF", "models/embeddinggemma-300m-q8_0.gguf"),
}
JANITOR = os.environ.get("IPA_JANITOR")
THREADS, CTX = 4, 4096
STATE_DIR = REPO / "runs"


def _state(port):
    return STATE_DIR / f"local_server_{port}.json"


def build_cmd(model, port, embedding=False, binary=BINARY):
    path = MODELS.get(model, model)
    cmd = [binary, "-m", path, "--host", "127.0.0.1", "--port", str(port),
           "-t", str(THREADS), "-c", str(CTX)]
    if embedding:
        cmd.append("--embedding")
    return cmd


def health(port, timeout=2.0):
    import requests
    try:
        r = requests.get(f"http://127.0.0.1:{port}/health", timeout=timeout)
        return r.status_code == 200 and (r.json() or {}).get("status", "ok") == "ok"
    except Exception:
        return False


def _janitor(args):
    if not JANITOR or not os.path.isfile(JANITOR):
        return None
    try:
        p = subprocess.run([sys.executable, JANITOR] + args, capture_output=True, text=True, timeout=30)
        return p.stdout.strip().splitlines()[-1] if p.returncode == 0 and p.stdout.strip() else None
    except Exception:
        return None


def start(model="chat", port=8091, embedding=None, wait=180, lease="2h"):
    if embedding is None:
        embedding = model == "embed"
    cmd = build_cmd(model, port, embedding)
    if not os.path.exists(cmd[2]):
        raise FileNotFoundError(cmd[2])
    STATE_DIR.mkdir(exist_ok=True)
    logf = open(STATE_DIR / f"local_server_{port}.log", "ab")
    env = dict(os.environ)  # the release build ships its .so files next to the binary
    libdir = os.path.dirname(cmd[0])
    if libdir:
        env["LD_LIBRARY_PATH"] = libdir + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True, env=env)
    ledger = _janitor(["start", "--what", f"llama-server {Path(cmd[2]).name}", "--owner",
                       "inverted-pyramid", "--until", lease, "--pid", str(proc.pid)])
    handle = {"pid": proc.pid, "port": port, "model": cmd[2], "embedding": embedding,
              "ledger_id": ledger, "started": time.time()}
    _state(port).write_text(json.dumps(handle))
    t0 = time.time()
    while time.time() - t0 < wait:
        if proc.poll() is not None:
            stop(port)
            raise RuntimeError(f"llama-server exited early (code {proc.returncode}); see runs/local_server_{port}.log")
        if health(port):
            handle["ready_s"] = round(time.time() - t0, 1)
            _state(port).write_text(json.dumps(handle))
            return handle
        time.sleep(1)
    stop(port)
    raise TimeoutError(f"llama-server not healthy after {wait}s")


def stop(port=8091):
    sf = _state(port)
    if not sf.exists():
        return False
    h = json.loads(sf.read_text())
    try:
        os.killpg(h["pid"], signal.SIGTERM)
        for _ in range(20):
            try:  # reap if it is our child (smoke), so it does not linger as a zombie
                if os.waitpid(h["pid"], os.WNOHANG)[0]:
                    break
            except ChildProcessError:
                pass
            os.kill(h["pid"], 0)
            time.sleep(0.25)
        else:
            os.killpg(h["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass
    if h.get("ledger_id"):
        _janitor(["done", h["ledger_id"]])
    sf.unlink()
    return True


def smoke(model="chat", port=8091):
    """Start, send one request, stop. Returns a small result dict."""
    import requests
    h = start(model, port)
    try:
        t0 = time.perf_counter()
        if h["embedding"]:
            r = requests.post(f"http://127.0.0.1:{port}/v1/embeddings",
                              json={"input": "hydraulic leak grounded the aircraft"}, timeout=120)
            n = len(r.json()["data"][0]["embedding"])
            reply = f"dim={n}"
        else:
            from llm import call
            reply = call("smoke.local", "Reply with the single word: ready", provider="local",
                         url=f"http://127.0.0.1:{port}/v1")
        return {"ready_s": h.get("ready_s"), "request_ms": round((time.perf_counter() - t0) * 1000),
                "reply": str(reply)[:80], "ledger_id": h.get("ledger_id")}
    finally:
        stop(port)


def main(argv=None):
    ap = argparse.ArgumentParser(description="manage a local llama-server")
    ap.add_argument("cmd", choices=["start", "stop", "health", "smoke"])
    ap.add_argument("--model", default="chat", help="chat | embed | path to a .gguf")
    ap.add_argument("--port", type=int, default=8091)
    a = ap.parse_args(argv)
    if a.cmd == "start":
        print(json.dumps(start(a.model, a.port)))
    elif a.cmd == "stop":
        print("stopped" if stop(a.port) else "nothing to stop")
    elif a.cmd == "health":
        print("ok" if health(a.port) else "down")
    else:
        print(json.dumps(smoke(a.model, a.port)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
