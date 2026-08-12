"""
Compatibility shim to provide a minimal `google.generativeai`-like API
backed by the newer `google.genai` client. This allows older code that
uses `genai.configure()` and `genai.GenerativeModel(...).generate_content(...)`
to keep working with minimal edits.

The shim implements:
- configure(api_key)
- class GenerativeModel(model_name) with method `generate_content(prompt)`

The returned object has a `.text` attribute and a `.candidates` list
with `.content.parts` mirroring the older SDK shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

_API_KEY: str | None = None


def configure(api_key: str) -> None:
    global _API_KEY
    _API_KEY = api_key


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


class GenerativeModel:
    def __init__(self, model_name: str):
        self.model_name = model_name

    def generate_content(self, prompt: str | list | dict, **kwargs: Any) -> _Response:
        try:
            from google import genai as real_genai
        except Exception as e:
            raise RuntimeError("google.genai is required for genai_compat shim") from e

        if _API_KEY is None:
            raise RuntimeError("API key not configured. Call configure(api_key) first.")

        client = real_genai.Client(api_key=_API_KEY)

        # Map prompt to contents param
        contents = prompt
        # If prompt is dict with 'input' or similar, try to extract
        if isinstance(prompt, dict) and "input" in prompt:
            contents = prompt["input"]

        # Call the modern API
        resp = client.models.generate_content(model=self.model_name, contents=contents, **kwargs)

        # Build text by concatenating candidate parts when available
        text = ""
        candidates = []
        try:
            for cand in getattr(resp, "candidates", []) or []:
                parts = []
                for p in getattr(cand.content, "parts", []) or []:
                    part_text = getattr(p, "text", "") or ""
                    text += part_text
                    parts.append(_Part(text=part_text))
                candidates.append(_Candidate(content=_Content(parts=parts)))
        except Exception:
            # Fallback: try to get plain text
            text = getattr(resp, "text", "") or str(resp)

        return _Response(text=text, candidates=candidates)
