"""Resolve a continuous quotation to the actual source, without trusting a model's address."""

from __future__ import annotations

import re

from src.agents.schemas.parse.structure import DocumentStructureNode
from src.agents.steps_tools.document.structure import build_document_structure_tree


class QuoteIndex:
    def __init__(self, text: str) -> None:
        self.text = text
        self._compact: tuple[str, list[int]] | None = None
        words = list(re.finditer(r"\S+", text))
        self.normalized = " ".join(word.group() for word in words)
        self.offsets: list[int] = []
        for index, word in enumerate(words):
            if index:
                self.offsets.append(word.start() - 1)
            self.offsets.extend(range(word.start(), word.end()))
        self._tree = build_document_structure_tree(text)

    def restore_letter_spacing(self, quote: str) -> str:
        """Вернуть исходную цитату при однозначном расхождении только пробелов между буквами.

        Не восстанавливать буквы, знаки, числа, пропуски текста или неоднозначные
        варианты. Возвращённый фрагмент проходит обычную строгую проверку цитат.
        """
        if not quote.strip() or self.spans(quote):
            return quote
        if self._compact is None:
            positions = [i for i, char in enumerate(self.text) if not char.isspace()]
            self._compact = ("".join(self.text[i] for i in positions), positions)
        compact, positions = self._compact
        letters = list(re.finditer(r"\S", quote))
        needle = "".join(match.group() for match in letters)
        candidates: dict[str, str] = {}
        at = compact.find(needle)
        while at != -1:
            spacing_safe = all(
                (letters[i + 1].start() > letters[i].end()) == (positions[at + i + 1] > positions[at + i] + 1)
                or (needle[i].isalpha() and needle[i + 1].isalpha())
                for i in range(len(needle) - 1)
            )
            if spacing_safe:
                original = self.text[positions[at] : positions[at + len(needle) - 1] + 1]
                candidates.setdefault(" ".join(original.split()), original)
            at = compact.find(needle, at + 1)
        return next(iter(candidates.values())) if len(candidates) == 1 else quote

    def spans(self, quote: str) -> list[tuple[int, int]]:
        """Zero-based half-open original coordinates, including whitespace provenance."""
        needle = " ".join(quote.split())
        if not needle:
            return []
        found = []
        at = self.normalized.find(needle)
        while at != -1:
            found.append((self.offsets[at], self.offsets[at + len(needle) - 1] + 1))
            at = self.normalized.find(needle, at + 1)
        return found

    def location(self, span: tuple[int, int]) -> str:
        """Use proven structural labels; always include exact one-based source coordinates."""
        start, end = span

        def labels(node: DocumentStructureNode, ancestors: tuple[str, ...] = ()) -> tuple[str, ...]:
            path = ancestors
            if node.address_resolved and node.kind not in {"document", "preamble", "paragraph", "signature"}:
                path = (*path, node.label)
            best = path if node.address_resolved and node.char_start <= start and end <= node.char_end else ()
            for child in node.children:
                candidate = labels(child, path)
                if len(candidate) > len(best):
                    best = candidate
            return best

        path = labels(self._tree.root)
        coordinates = f"символы {start + 1}–{end}"
        return f"{', '.join(path)} ({coordinates})" if path else coordinates
