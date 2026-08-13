"""
Compatibility shim exposing a minimal `google.generativeai`-style API on top of
the newer `google.genai` client, so existing code that calls
`genai.configure(...)` and `GenerativeModel(...).generate_content(...)` keeps
working unchanged.

It also adds resilience the old SDK never had: the free Gemini tier limits each
model to a small number of requests per day, but the quota is *per model and per
key*. So when a call comes back 429 / RESOURCE_EXHAUSTED, this rotates through
the other API keys and then the fallback models configured in
config/api_keys.json before giving up. A genuine error (bad request, etc.) is
raised at once rather than pointlessly retried everywhere.

The returned object mirrors the old shape: a `.text` attribute plus a
`.candidates` list of `.content.parts`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

_API_KEY: str | None = None
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"

# Sensible defaults if the config names no fallbacks — all known to exist on the
# free tier, cheapest / highest-quota last.
_DEFAULT_FALLBACKS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-31b-it",
]


def configure(api_key: str) -> None:
    global _API_KEY
    _API_KEY = api_key


def _load_cfg() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _keys() -> List[str]:
    """Every Gemini key we may use, primary first, de-duplicated."""
    cfg = _load_cfg()
    out: List[str] = []
    for k in [_API_KEY, cfg.get("gemini_api_key"), *(cfg.get("gemini_api_keys") or [])]:
        if k and k not in out:
            out.append(k)
    return out or ([_API_KEY] if _API_KEY else [])


def _models(primary: str) -> List[str]:
    """The model to try first, then the configured (or default) fallbacks."""
    cfg = _load_cfg()
    out: List[str] = []
    fallbacks = cfg.get("fallback_models") or _DEFAULT_FALLBACKS
    for m in [primary, cfg.get("model_name"), *fallbacks]:
        if m and m not in out:
            out.append(m)
    return out


def _is_quota(err: Exception) -> bool:
    s = str(err)
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "quota" in s.lower()


def _is_absent(err: Exception) -> bool:
    s = str(err)
    return "404" in s or "NOT_FOUND" in s or "not found" in s.lower()


@dataclass
class _Part:
    text: str = ""


@dataclass
class _Content:
    parts: List[_Part]


@dataclass
class _Candidate:
    content: _Content


@dataclass
class _Response:
    text: str
    candidates: List[_Candidate]


def _wrap(resp: Any) -> _Response:
    text = ""
    candidates: List[_Candidate] = []
    try:
        for cand in getattr(resp, "candidates", []) or []:
            parts = []
            for p in getattr(cand.content, "parts", []) or []:
                part_text = getattr(p, "text", "") or ""
                text += part_text
                parts.append(_Part(text=part_text))
            candidates.append(_Candidate(content=_Content(parts=parts)))
    except Exception:
        text = getattr(resp, "text", "") or str(resp)
    if not text:
        text = getattr(resp, "text", "") or text
    return _Response(text=text, candidates=candidates)


class GenerativeModel:
    def __init__(self, model_name: str):
        self.model_name = model_name

    def generate_content(self, prompt: str | list | dict, **kwargs: Any) -> _Response:
        try:
            from google import genai as real_genai
        except Exception as e:
            raise RuntimeError("google.genai is required for genai_compat shim") from e

        keys = _keys()
        if not keys:
            raise RuntimeError("No Gemini API key configured. Call configure() first.")

        contents = prompt
        if isinstance(prompt, dict) and "input" in prompt:
            contents = prompt["input"]

        models   = _models(self.model_name)
        last_err: Exception | None = None

        # (model, key) grid: exhaust every key for a model before moving on, so
        # a healthy second key rescues the preferred model before we downgrade.
        for model in models:
            model_absent_everywhere = True
            for key in keys:
                try:
                    client = real_genai.Client(api_key=key)
                    resp = client.models.generate_content(
                        model=model, contents=contents, **kwargs
                    )
                    if model != self.model_name:
                        print(f"[genai] {self.model_name} exhausted — used {model}")
                    return _wrap(resp)
                except Exception as e:                       # noqa: BLE001
                    last_err = e
                    if _is_quota(e):
                        model_absent_everywhere = False
                        continue                              # next key, same model
                    if _is_absent(e):
                        continue                              # this key lacks it; try next key
                    # A real error (bad prompt, safety, network): don't paper
                    # over it by rotating — surface it immediately.
                    raise
            # Fell through every key for this model. If it was quota, keep going
            # to the next model; if it simply does not exist, also move on.
            _ = model_absent_everywhere

        raise last_err or RuntimeError("All Gemini models and keys are exhausted.")
