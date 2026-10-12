"""Explicit, idempotent publication of local templates to Langfuse.

    python -m src.infrastructure.prompts.publish           # inspect only
    python -m src.infrastructure.prompts.publish --publish # create missing templates

Existing templates with different contents are never overwritten.
"""

from __future__ import annotations

import argparse

from langfuse.api import NotFoundError
from langfuse.model import ChatMessageDict, ChatMessageWithPlaceholdersDict

from src.common.config.config import settings
from src.common.utils.logger import configure_logging, get_logger
from src.infrastructure.prompts.prompt_kit import PROMPTS_DIR, _client, _load_local_prompt

log = get_logger("src.infrastructure.prompts.publish")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Create missing templates with the configured label")
    args = parser.parse_args()
    configure_logging()
    client = _client()
    if client is None:
        parser.error("Set LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL")
    label = str(settings.get("LANGFUSE_PROMPT_LABEL") or "production")
    missing = []
    conflicts = []
    try:
        # Inspect all names before writing, so a collision cannot leave a partial migration.
        for path in sorted(PROMPTS_DIR.glob("*.yaml")):
            local_messages, contract = _load_local_prompt(path.stem)
            config = {"contract": contract} if contract else {}
            local: list[ChatMessageDict | ChatMessageWithPlaceholdersDict] = [
                ChatMessageDict(role=role, content=text) for role, text in local_messages
            ]
            try:
                remote = client.get_prompt(
                    path.stem,
                    type="chat",
                    label=label,
                    cache_ttl_seconds=0,
                    fetch_timeout_seconds=5,
                    max_retries=0,
                )
            except NotFoundError:
                missing.append((path.stem, local, config))
                log.info("prompt_missing", prompt_name=path.stem, label=label)
                continue
            expected = [{"type": "message", **message} for message in local]
            if remote.prompt != expected or (contract and remote.config.get("contract") != contract):
                conflicts.append(path.stem)
                log.error("prompt_content_conflict", prompt_name=path.stem, version=remote.version)
            else:
                log.info("prompt_already_published", prompt_name=path.stem, version=remote.version)
        if conflicts:
            raise SystemExit("Remote templates differ from local YAML; resolve conflicts before publication")
        if args.publish:
            for name, messages, config in missing:
                published = client.create_prompt(
                    name=name,
                    type="chat",
                    prompt=messages,
                    config=config,
                    labels=[label],
                    commit_message="Publish Antikorr repository template and its contract",
                )
                log.info("prompt_published", prompt_name=name, version=published.version, label=label)
        log.info("prompt_publication_complete", missing=len(missing), published=len(missing) if args.publish else 0)
    finally:
        client.shutdown()
        _client.cache_clear()


if __name__ == "__main__":
    main()
