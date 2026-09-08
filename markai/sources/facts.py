"""Owner-curated facts: ordinances with effective dates, and Chicagoland cost ranges.

Everything else Mark knows is prose that someone wrote on a date nobody recorded. An
ordinance is different: it has a number, a jurisdiction, and a day it started applying, and
last year's version being quoted as current is the one mistake that actually hurts a
landlord. Same for a cost range, which is only useful with the month it was true.

So these live in their own file, ``sources/facts.yaml``, outside the crawl:

- Nothing enters without a ``citation``. The loader refuses an ordinance that does not name
  where it comes from, because an unsourced legal claim in an authoritative block is worse
  than no block at all.
- ``effective_from`` and ``effective_to`` decide what is in force on the day of the
  question. A superseded rule stays in the file and stops being selected.
- The file is optional. With no file there is no block, no extra tokens, and Mark answers
  from the sources exactly as before.

Selection is deliberately plain term overlap rather than the retriever: a rule set is
small, the owner wrote every word of it, and a deterministic match is one they can predict.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

MAX_SELECTED = 5

# Words that carry no matching signal, in either language the questions come in.
_NOISE = frozenset(
    """
    a an the and or but if of to in on at for is are was were be do does did have has had
    my me i you your it its that this what which who how when where why can could will
    would should may might about into than then there here not no so all any some
    el la los las un una unos unas y o de del al en por para con sin que qué como cómo
    cuando cuándo donde dónde quien quién es son era eran ser estar mi mis tu tus su sus
    lo le les se me te nos hay muy más mas pero si sí no ya
    """.split()
)
_WORD = re.compile(r"[a-z0-9áéíóúñü]+")


def _terms(text: str) -> set[str]:
    return {
        word for word in _WORD.findall((text or "").lower()) if len(word) > 2 and word not in _NOISE
    }


class Ordinance(BaseModel):
    """One rule, as the owner states it, with the dates it applies between."""

    id: str
    jurisdiction: str
    topic: str
    rule: str
    citation: str
    effective_from: date | None = None
    effective_to: date | None = None
    url: str | None = None
    notes: str | None = None
    keywords: list[str] = Field(default_factory=list)

    @field_validator("citation")
    @classmethod
    def _must_name_a_source(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "every ordinance needs a citation (the section number, or the page it came "
                "from). An unsourced rule is not a fact."
            )
        return value.strip()

    @model_validator(mode="after")
    def _dates_must_make_sense(self) -> Ordinance:
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError(
                f"{self.id}: effective_to ({self.effective_to}) is before effective_from "
                f"({self.effective_from})."
            )
        return self

    def in_force(self, as_of: date) -> bool:
        if self.effective_from and as_of < self.effective_from:
            return False
        return not (self.effective_to and as_of > self.effective_to)

    def terms(self) -> set[str]:
        return _terms(" ".join([self.topic, self.jurisdiction, self.rule, " ".join(self.keywords)]))


class CostRange(BaseModel):
    """What a job actually costs around here, and the month that was true."""

    id: str
    item: str
    low: float = Field(ge=0)
    high: float = Field(ge=0)
    unit: str = "per job"
    as_of: str | None = None
    market: str = "Chicagoland"
    source: str | None = None
    notes: str | None = None
    keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _low_below_high(self) -> CostRange:
        if self.high < self.low:
            raise ValueError(f"{self.id}: high ({self.high}) is below low ({self.low}).")
        return self

    def terms(self) -> set[str]:
        return _terms(" ".join([self.item, self.market, self.unit, " ".join(self.keywords)]))

    def money(self) -> str:
        if self.low == self.high:
            return f"${self.low:,.0f}"
        return f"${self.low:,.0f} to ${self.high:,.0f}"


class FactBook(BaseModel):
    """The whole file. Both sections are optional."""

    ordinances: list[Ordinance] = Field(default_factory=list)
    costs: list[CostRange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids_must_be_unique(self) -> FactBook:
        for label, items in (("ordinance", self.ordinances), ("cost", self.costs)):
            seen: set[str] = set()
            for item in items:
                if item.id in seen:
                    raise ValueError(f"duplicate {label} id {item.id!r}.")
                seen.add(item.id)
        return self

    def is_empty(self) -> bool:
        return not self.ordinances and not self.costs

    def in_force(self, as_of: date) -> list[Ordinance]:
        return [o for o in self.ordinances if o.in_force(as_of)]

    def superseded(self, as_of: date) -> list[Ordinance]:
        return [o for o in self.ordinances if not o.in_force(as_of)]


def facts_path(sources_file: Path | str) -> Path:
    """``sources/facts.yaml``, next to the manifest. ``facts.local.yaml`` wins if present."""
    folder = Path(sources_file).parent
    local = folder / "facts.local.yaml"
    return local if local.exists() else folder / "facts.yaml"


def load_facts(path: Path | str) -> FactBook:
    """Read the fact book. A missing file is normal and yields an empty one."""
    path = Path(path)
    if not path.exists():
        return FactBook()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level.")
    return FactBook.model_validate(raw)


def select(
    book: FactBook,
    question: str,
    as_of: date | None = None,
    limit: int = MAX_SELECTED,
) -> tuple[list[Ordinance], list[CostRange]]:
    """The facts worth putting in front of one question, best match first.

    A rule that shares no word with the question is left out: the point of the block is that
    everything in it is relevant and current, not that it is complete.
    """
    if book.is_empty():
        return [], []
    asked = _terms(question)
    if not asked:
        return [], []
    today = as_of or date.today()

    def ranked(items: list[Any]) -> list[Any]:
        scored = [(len(asked & item.terms()), item) for item in items]
        hits = [(score, item) for score, item in scored if score]
        hits.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [item for _, item in hits[: max(limit, 0)]]

    return ranked(book.in_force(today)), ranked(book.costs)
