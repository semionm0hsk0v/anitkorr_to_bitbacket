from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import ValidationError

from src.agents.schemas.legal_base.currentness import (
    ReferenceCurrentnessOutcome,
    ReferenceCurrentnessReceipt,
    validate_reference_currentness_receipt,
)

_trusted_currentness_receipts: ContextVar[set[tuple[str, str, str]] | None] = ContextVar(
    "trusted_reference_currentness_receipts",
    default=None,
)


def _trust_key(receipt: ReferenceCurrentnessReceipt) -> tuple[str, str, str] | None:
    observed = receipt.observed
    if receipt.outcome is not ReferenceCurrentnessOutcome.CURRENT or observed is None:
        return None
    return receipt.receipt_id, observed.version_fingerprint, observed.content_fingerprint


def register_trusted_currentness_receipt(receipt: ReferenceCurrentnessReceipt) -> None:
    "Register a receipt created by the live trusted retrieval/fixture boundary."
    canonical = validate_reference_currentness_receipt(receipt.model_dump(mode="json"))
    key = _trust_key(canonical)
    if key is not None:
        registry = _trusted_currentness_receipts.get()
        if registry is None:
            registry = set()
            _trusted_currentness_receipts.set(registry)
        registry.add(key)


@contextmanager
def trusted_currentness_scope() -> Iterator[None]:
    "Isolate mutable trust capabilities to one graph request context."
    token = _trusted_currentness_receipts.set(set())
    try:
        yield
    finally:
        _trusted_currentness_receipts.reset(token)


def currentness_receipt_is_trusted(receipt: ReferenceCurrentnessReceipt | None) -> bool:
    "Whether this exact receipt was produced by a live boundary in *this* request."
    if receipt is None:
        return False
    try:
        canonical = validate_reference_currentness_receipt(receipt.model_dump(mode="json"))
    except (ValidationError, TypeError, ValueError):
        return False
    key = _trust_key(canonical)
    registry = _trusted_currentness_receipts.get()
    return key is not None and registry is not None and key in registry
