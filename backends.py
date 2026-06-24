"""
LLM backends for the experiment.

Every backend implements the same method:

    generate(system, user, tools) -> {
        "tool_name": str | None,     # the tool the model chose to call
        "arguments": dict,           # the arguments it produced
        "raw": <anything>,           # raw provider response, for the log
    }

`tools` is an OpenAI-style tool list:
    [{"type": "function",
      "function": {"name": ..., "description": ..., "parameters": <json schema>}}]

The only backend that runs without external services is MockBackend, which lets
the whole pipeline (success detection + summary table) be tested offline. The
HTTP backends (Ollama / OpenAI / Anthropic) hit real endpoints.

Models are selected on the CLI with strings like:
    ollama:qwen2.5:7b   ollama:llama3.1:8b   openai:gpt-4o-mini   anthropic:claude-...
    mock:safe           mock:leaky
"""

import os
import json
import random

import httpx


# --------------------------------------------------------------------------- #
# Mock (offline) -- used to validate the harness end to end.
# --------------------------------------------------------------------------- #
class MockBackend:
    """
    Simulates a model's susceptibility to tool-description poisoning so the
    pipeline can be exercised with no GPU and no API key.

    It inspects the tool description, classifies the injection strength, and
    leaks the planted secret with a fixed per-strength probability. This is NOT
    a model of any real system -- it exists only to prove that logging, success
    detection, and the summary table work.
    """

    # leak probability by injection strength
    PROFILES = {
        "safe":  {"none": 0.0, "blatant": 0.0, "justified": 0.2, "disguised": 0.6},
        "leaky": {"none": 0.0, "blatant": 0.4, "justified": 0.8, "disguised": 1.0},
    }

    def __init__(self, profile="safe", seed=None):
        self.name = f"mock:{profile}"
        self._p = self.PROFILES.get(profile, self.PROFILES["safe"])
        self._rng = random.Random(seed)

    @staticmethod
    def _classify(desc: str) -> str:
        if "一切説明しないでください" in desc or "IMPORTANT" in desc:
            return "blatant"
        if "デバッグ" in desc or "認証検証" in desc:
            return "justified"
        if "telemetry" in desc or "metadata" in desc:
            return "disguised"
        return "none"

    def generate(self, system, user, tools):
        fn = tools[0]["function"]
        desc = fn.get("description", "")
        strength = self._classify(desc)
        leak = self._rng.random() < self._p.get(strength, 0.0)
        # extract the planted secret from the system prompt, the way a model
        # "tempted" by the injection would pull it from its context
        secret = ""
        marker = "トークンは "
        if leak and marker in system:
            secret = system.split(marker, 1)[1].split(" ", 1)[0].rstrip("。")
        args = {"city": "東京"}
        if secret:
            args["context"] = secret
        return {
            "tool_name": fn["name"],
            "arguments": args,
            "raw": {"mock_strength": strength, "leaked": leak},
        }


# --------------------------------------------------------------------------- #
# Ollama (local) -- the primary backend for the RTX 5070 / Windows setup.
# --------------------------------------------------------------------------- #
class OllamaBackend:
    """
    Talks to a local Ollama server (default http://localhost:11434).
    Requires a tool-calling-capable model, e.g. qwen2.5:7b, llama3.1:8b.
    """

    def __init__(self, model, host=None):
        self.model = model
        self.name = f"ollama:{model}"
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    def generate(self, system, user, tools):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": tools,
            "stream": False,
            "options": {"temperature": 0.7, "num_predict": 512},
        }
        r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=120)
        r.raise_for_status()
        data = r.json()
        msg = data.get("message", {})
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"tool_name": None, "arguments": {}, "raw": data}
        call = calls[0]["function"]
        args = call.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_unparsed": args}
        return {"tool_name": call.get("name"), "arguments": args, "raw": data}


# --------------------------------------------------------------------------- #
# OpenAI -- optional, only if a key is present.
# --------------------------------------------------------------------------- #
class OpenAIBackend:
    # API key is read from the env var My_OPENAI_API_KEY (falls back to the
    # standard OPENAI_API_KEY if that one is set instead).
    ENV_KEYS = ("My_OPENAI_API_KEY", "OPENAI_API_KEY")

    def __init__(self, model):
        self.model = model
        self.name = f"openai:{model}"
        self.key = next((os.environ[k] for k in self.ENV_KEYS if os.environ.get(k)), None)

    def generate(self, system, user, tools):
        if not self.key:
            raise RuntimeError("My_OPENAI_API_KEY not set")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": tools,
            "tool_choice": "auto",
            # "temperature": 0.7,
            # Cap output. GPT-5 / o-series / 4.1 on chat-completions require
            # `max_completion_tokens` (the older `max_tokens` is rejected).
            # If you switch to a model that only accepts `max_tokens`, rename
            # this key to match that model's spec.
            "max_completion_tokens": 512,
        }
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json=payload,
            timeout=120,
        )
        # r.raise_for_status()
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:300]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"tool_name": None, "arguments": {}, "raw": data}
        fn = calls[0]["function"]
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_unparsed": fn.get("arguments")}
        return {"tool_name": fn.get("name"), "arguments": args, "raw": data}


# --------------------------------------------------------------------------- #
# Anthropic -- optional, only if a key is present.
# --------------------------------------------------------------------------- #
class AnthropicBackend:
    def __init__(self, model):
        self.model = model
        self.name = f"anthropic:{model}"
        self.key = os.environ.get("ANTHROPIC_API_KEY")

    def generate(self, system, user, tools):
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        # convert OpenAI-style tools -> Anthropic tool schema
        a_tools = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in tools
        ]
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": a_tools,
            "tool_choice": {"type": "auto"},
        }
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return {
                    "tool_name": block.get("name"),
                    "arguments": block.get("input", {}),
                    "raw": data,
                }
        return {"tool_name": None, "arguments": {}, "raw": data}


# --------------------------------------------------------------------------- #
def build_backend(spec: str):
    """spec like 'ollama:qwen2.5:7b', 'openai:gpt-4o-mini', 'mock:safe'."""
    provider, _, model = spec.partition(":")
    if provider == "mock":
        return MockBackend(profile=model or "safe")
    if provider == "ollama":
        return OllamaBackend(model)
    if provider == "openai":
        return OpenAIBackend(model)
    if provider == "anthropic":
        return AnthropicBackend(model)
    raise ValueError(f"unknown backend spec: {spec!r}")
