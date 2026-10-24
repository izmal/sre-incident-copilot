"""Gemma 4 via the Gemini API. The only place in the code where the model is called."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = os.getenv("GEMMA_MODEL", "gemma-4-26b-a4b-it")
CACHE_DIR = Path(__file__).resolve().parent.parent / "evaluation" / "cache"


class LLMError(RuntimeError):
    pass


def _client():
    from google import genai

    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY is not set. Copy .env.example to .env and add a key from https://aistudio.google.com/apikey")
    return genai.Client(api_key=key)


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LLMError("Model did not return JSON.")
        data = json.loads(m.group(0))
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]  # Gemma sometimes wraps the object in a list
    if not isinstance(data, dict):
        raise LLMError("Model returned JSON that is not an object.")
    return data


def generate_json(system: str, user: str, model: str | None = None, temperature: float | None = None,
                  use_cache: bool = False, retries: int = 2) -> tuple[dict, dict]:
    """Returns (parsed_json, meta). meta has model, latency_s, raw_text, cached."""
    from google.genai import types

    model = model or DEFAULT_MODEL
    temperature = float(os.getenv("GEMMA_TEMPERATURE", "0.2")) if temperature is None else temperature
    key = hashlib.sha256(json.dumps([model, temperature, system, user]).encode()).hexdigest()[:24]
    cache_file = CACHE_DIR / f"{key}.json"
    if use_cache and cache_file.exists():
        cached = json.loads(cache_file.read_text())
        cache_file.touch()  # mark as used, so stale entries can be pruned
        return parse_json(cached["raw_text"]), {**cached["meta"], "cached": True, "raw_text": cached["raw_text"]}

    client = _client()
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=4096,
        response_mime_type="application/json",
        # Without this Gemma 4 spends the output budget on thinking and returns nothing.
        thinking_config=types.ThinkingConfig(thinking_level="minimal"),
    )
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            resp = client.models.generate_content(model=model, contents=user, config=config)
            raw = resp.text or ""
            data = parse_json(raw)
            meta = {"model": model, "latency_s": round(time.time() - t0, 2), "cached": False, "attempt": attempt + 1}
            if use_cache:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({"raw_text": raw, "meta": meta}))
            return data, {**meta, "raw_text": raw}
        except LLMError as e:
            last_err = e
        except Exception as e:  # network, quota, 5xx
            last_err = e
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                time.sleep(8 * (attempt + 1))
            else:
                time.sleep(1.5)
    raise LLMError(f"Gemma call failed after {retries + 1} attempts: {str(last_err)[:300]}")
