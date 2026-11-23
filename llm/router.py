"""The LLM router: `call(task, prompt, provider=..., schema=None)`.

- provider "rules" is a passthrough to a Python function (`rules=`), so a
  deterministic baseline and a model run go through one logged door;
- the PHI guard refuses raw identifiers to any provider but local and rules;
- with a pydantic `schema`, the reply must be JSON that validates; on failure
  the call retries once with the validation error in the prompt;
- every attempt appends exactly one line to runs/routing.jsonl (ok is false
  when the provider failed, the guard refused, or the reply failed the schema).
"""
import json
import re
import time

from pydantic import BaseModel, ValidationError

from llm import log, phi
from llm import providers as P

KNOWN = ("rules", "local", "claude", "codex", "gemini")


class RouterError(RuntimeError):
    pass


def extract_json(text):
    """Pull a JSON object out of a model reply (handles ``` fences and chatter)."""
    if isinstance(text, (dict, list)):
        return text
    if isinstance(text, BaseModel):
        return text.model_dump()
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        return json.loads(t[i:j + 1])
    raise ValueError("no JSON object in reply")


def _json_prompt(prompt, schema):
    return (prompt.rstrip() + "\n\nReply with one JSON object only, no prose, matching this JSON schema:\n"
            + json.dumps(schema.model_json_schema()))


def _attempt(task, prompt, provider, model, rules, schema, opts):
    """One provider attempt (plus validation when schema is set), logged once."""
    t0 = time.perf_counter()
    meta, out, err = {}, None, None
    try:
        phi.check(provider, prompt)
        if provider == "rules":
            if rules is None:
                raise RouterError(f"{task}: provider rules needs a rules= function")
            out = rules(prompt)
        else:
            out, meta = P.PROVIDERS[provider](prompt, model, json_mode=schema is not None, **opts)
        if schema is not None:
            return schema.model_validate(extract_json(out))
        return out
    except Exception as e:
        err = f"{type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}"
        raise
    finally:
        ms = (time.perf_counter() - t0) * 1000
        if out is None or isinstance(out, str):
            out_s = out or ""
        else:
            out_s = json.dumps(extract_json(out), default=str) if isinstance(out, (dict, list, BaseModel)) else str(out)
        log.append(task, provider, meta.get("model") or model, ms, len(prompt or ""), len(out_s),
                   ok=err is None, error=err, tokens_in=meta.get("tokens_in"), tokens_out=meta.get("tokens_out"))


def call(task, prompt, provider="rules", schema=None, model=None, rules=None, retries=1, **opts):
    """Route one task. Returns str (or the rules function's value), or a
    `schema` instance when schema is given."""
    if provider not in KNOWN:
        raise RouterError(f"unknown provider {provider!r}; known: {', '.join(KNOWN)}")
    model = model or P.DEFAULT_MODELS.get(provider)
    if schema is None:
        return _attempt(task, prompt, provider, model, rules, None, opts)
    return call_json(task, prompt, schema, provider=provider, model=model, rules=rules, retries=retries, **opts)


def call_json(task, prompt, schema, provider="rules", model=None, rules=None, retries=1, **opts):
    """JSON mode: validate against `schema`; on a bad reply retry `retries`
    times with the validation error appended to the prompt."""
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        raise RouterError("schema must be a pydantic BaseModel subclass")
    model = model or P.DEFAULT_MODELS.get(provider)
    p = prompt if provider == "rules" else _json_prompt(prompt, schema)
    last = None
    for _ in range(retries + 1):
        try:
            return _attempt(task, p, provider, model, rules, schema, opts)
        except (ValidationError, ValueError) as e:
            last = e
            if provider == "rules":
                break  # deterministic: a retry gives the same answer
            p = (_json_prompt(prompt, schema) + "\n\nYour previous reply was rejected:\n"
                 + str(e)[:800] + "\nReturn corrected JSON only.")
    raise RouterError(f"{task}: reply failed schema {schema.__name__}: {last}")
