import copy
import math
from collections.abc import Sequence
from typing import Any, ClassVar

from src.agents.schemas.competence.assessment import CompetenceAssessment
from src.agents.schemas.competence.fragments import SourceView
from src.common.config.config import settings


def _evidence_spans(texts: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Partition real texts, preserving exact spans without constructing unused annotations."""
    chunk_chars = int(settings["agents.competence.evidence.chunk_chars"])
    min_split_chars = int(settings["agents.competence.evidence.min_split_chars"])
    groups: dict[str, list[dict[str, Any]]] = {}
    for source, text in texts.items():
        spans = []
        offset = 0
        for line in text.splitlines(keepends=True):
            rest = line
            while rest:
                length = min(len(rest), chunk_chars)
                if len(rest) > chunk_chars and (space := rest.rfind(" ", min_split_chars, chunk_chars)) > 0:
                    length = space + 1
                chunk, rest = rest[:length], rest[length:]
                if quote := chunk.strip():
                    start = offset + len(chunk) - len(chunk.lstrip())
                    spans.append({"quote": quote, "start": start, "end": start + len(quote)})
                offset += len(chunk)
        groups[source] = spans
    return groups


def block_pool(document: str, views: Sequence[SourceView]):
    texts = {"project": document, **{view.reading.source_id: view.text for view in views}}
    groups = _evidence_spans(texts)
    target_blocks = int(settings["agents.competence.evidence.target_blocks"])
    max_blocks = int(settings["agents.competence.evidence.max_blocks"])
    width = max(1, math.ceil(sum(map(len, groups.values())) / target_blocks))
    while sum(math.ceil(len(group) / width) for group in groups.values()) > max_blocks:
        width += 1
    blocks: dict[str, dict[str, Any]] = {}
    annotated: dict[str, str] = {}
    for source, group in groups.items():
        pieces: list[str] = []
        previous = 0
        for offset in range(0, len(group), width):
            chunk = group[offset : offset + width]
            start, end = chunk[0]["start"], chunk[-1]["end"]
            identity = f"B{len(blocks) + 1}"
            blocks[identity] = {
                "source": source,
                "quote": texts[source][start:end],
                "start": start,
                "end": end,
                "location": f"{source}: символы {start}–{end} ({identity})",
            }
            pieces.extend((texts[source][previous:start], f"\n⟦{identity}⟧\n", texts[source][start:end]))
            previous = end
        pieces.append(texts[source][previous:])
        annotated[source] = "".join(pieces)
    return blocks, annotated


def assessment_schema(blocks):
    """Ограничить ссылки модели идентификаторами переданных блоков текста."""

    class BoundAssessment(CompetenceAssessment):
        provider_strict: ClassVar[bool] = True

        @classmethod
        def model_json_schema(cls, *args, **kwargs):
            schema = copy.deepcopy(CompetenceAssessment.model_json_schema(*args, **kwargs))
            reference = {"$ref": "#/$defs/AllowedBlock"}
            schema["$defs"]["AllowedBlock"] = {"type": "string", "enum": list(blocks)}
            fields = schema["$defs"]["AuthorityAssessment"]["properties"]
            for field in ("project_evidence", "norm_evidence", "issuer_link_evidence", "counterevidence"):
                fields[field]["items"] = reference
            schema["$defs"]["FormAssessment"]["properties"]["evidence"]["items"] = reference
            return schema

    return BoundAssessment
