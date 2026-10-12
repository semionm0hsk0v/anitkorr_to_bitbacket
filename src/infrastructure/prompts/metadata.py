"""Safe identities of templates; no compiled document content is retained here."""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from langfuse.model import ChatPromptClient


def contract_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PromptIdentity:
    name: str
    version: int
    is_fallback: bool
    sha256: str


def describe_prompt(template: ChatPromptClient) -> PromptIdentity:
    return PromptIdentity(
        name=template.name,
        version=template.version,
        is_fallback=template.is_fallback,
        sha256=contract_hash(template.prompt),
    )
