"""Langfuse chat templates; local YAML supplies the cold-start fallback."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from time import monotonic

import yaml
from langfuse import Langfuse
from langfuse.api import Prompt_Chat
from langfuse.model import ChatMessageDict, ChatPromptClient

from src.common.config.config import settings
from src.common.utils.logger import get_logger

_REPO_ROOT = Path(__file__).resolve().parents[3]
_prompts_dir = Path(settings.get("PROMPTS_DIR") or "src/agents/prompts")
PROMPTS_DIR = (_prompts_dir if _prompts_dir.is_absolute() else _REPO_ROOT / _prompts_dir).resolve()
log = get_logger(__name__)
_fallback_cache: dict[str, tuple[float, ChatPromptClient]] = {}

_TEMPLATE_VARIABLE = re.compile(r"{{\s*([^{}]+?)\s*}}")


def validate_instruction_variables(messages: list) -> None:
    """Only the application-assigned date may be interpolated into system messages.

    Validate the template before compilation: document text may legitimately contain
    template-like strings and must never be interpreted as template configuration.
    """
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Unsupported prompt message")
        role, content = message.get("role"), message.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise ValueError("Unsupported prompt message")
        if role == "system" and set(_TEMPLATE_VARIABLE.findall(content)) - {"analysis_date"}:
            raise ValueError("Untrusted variables in system instructions")


@lru_cache
def _load_local_prompt(name: str) -> tuple[tuple[tuple[str, str], ...], str]:
    """Load immutable local messages for fallback and explicit publication."""
    if Path(name).name != name or not name:
        raise ValueError(f"Invalid prompt name: {name!r}")
    path = PROMPTS_DIR / f"{name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("name") != name:
        raise ValueError(f"Invalid prompt metadata: {path.name}")
    contract = data.get("contract", "")
    if not isinstance(contract, str):
        raise ValueError(f"Invalid prompt contract: {path.name}")
    content = data.get("prompt")
    if not isinstance(content, list) or not content:
        raise ValueError(f"Expected a nonempty chat template: {path.name}")
    validate_instruction_variables(content)
    messages = []
    for item in content:
        if not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant"}:
            raise ValueError(f"Invalid message role: {path.name}")
        if not isinstance(item.get("content"), str):
            raise ValueError(f"Expected message text: {path.name}")
        messages.append((item["role"], item["content"]))
    return tuple(messages), contract


@lru_cache(maxsize=1)
def _client() -> Langfuse | None:
    public_key = settings.get("LANGFUSE_PUBLIC_KEY")
    secret_key = settings.get("LANGFUSE_SECRET_KEY")
    if not public_key and not secret_key:
        return None
    if not public_key or not secret_key or not settings.get("LANGFUSE_BASE_URL"):
        raise ValueError("Langfuse requires LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL")
    return Langfuse(
        public_key=str(public_key),
        secret_key=str(secret_key),
        base_url=str(settings.LANGFUSE_BASE_URL),
        tracing_enabled=False,
    )


def get_prompt(name: str) -> ChatPromptClient:
    """Get a native SDK template; compilation stays explicit at the call site.

    SDK caches remote templates and refreshes stale entries in the background.
    The application lifespan warms cold entries outside the event loop.
    No credentials means local-only SDK compilation, without a network client.
    """
    local, contract = _load_local_prompt(name)
    fallback = [ChatMessageDict(role=role, content=text) for role, text in local]
    config = {"contract": contract} if contract else {}
    client = _client()
    if client is None:
        return ChatPromptClient(
            Prompt_Chat.parse_obj({"name": name, "version": 0, "prompt": fallback, "config": config, "labels": [], "tags": []}),
            is_fallback=True,
        )
    ttl = int(settings.get("LANGFUSE_PROMPT_CACHE_TTL_SECONDS", 300))
    timeout = int(settings.get("LANGFUSE_PROMPT_FETCH_TIMEOUT_SECONDS", 2))
    if ttl <= 0 or timeout <= 0:
        raise ValueError("Langfuse prompt cache TTL and fetch timeout must be positive")
    cached = _fallback_cache.get(name)
    if cached is not None and cached[0] > monotonic():
        return cached[1]
    template = client.get_prompt(
        name,
        type="chat",
        label=str(settings.get("LANGFUSE_PROMPT_LABEL") or "production"),
        cache_ttl_seconds=ttl,
        fetch_timeout_seconds=timeout,
        max_retries=0,
        fallback=fallback,
    )
    try:
        validate_instruction_variables(template.prompt)
        if contract and not template.is_fallback and template.config.get("contract") != contract:
            raise ValueError("Remote prompt has an incompatible input/output contract")
    except ValueError:
        log.warning("prompt_incompatible_remote_template", prompt_name=name, version=template.version)
        template = ChatPromptClient(
            Prompt_Chat.parse_obj({"name": name, "version": 0, "prompt": fallback, "config": config, "labels": [], "tags": []}),
            is_fallback=True,
        )
    if template.is_fallback:
        log.warning("prompt_local_fallback", prompt_name=name)
        # SDK caches successful fetches, but does not cache cold-start fallbacks.
        # Back off here so an outage cannot add a network timeout to every LLM call.
        _fallback_cache[name] = (monotonic() + ttl, template)
    else:
        _fallback_cache.pop(name, None)
    return template


@asynccontextmanager
async def prompt_lifespan():
    """Warm the SDK cache without blocking async request handling; close its workers."""
    import asyncio  # noqa: PLC0415

    client = _client()
    try:
        if client is not None:
            await asyncio.gather(*(asyncio.to_thread(get_prompt, path.stem) for path in PROMPTS_DIR.glob("*.yaml")))
        yield
    finally:
        if client is not None:
            await asyncio.to_thread(client.shutdown)
        _client.cache_clear()
        _fallback_cache.clear()
