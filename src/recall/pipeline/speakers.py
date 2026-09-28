"""Map transcript speaker labels (and owner names in recaps) to people.

Order: explicit override -> Entra ID (via Graph participants) -> email -> display name / alias -> fuzzy name
(ratio >= 0.9) -> unique first name among the meeting's team and participants. Anything else stays unmatched."""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

FUZZY_THRESHOLD = 0.9


@dataclass(frozen=True)
class PersonRef:
    id: uuid.UUID
    display_name: str
    email: str | None = None
    entra_id: str | None = None
    aliases: tuple[str, ...] = ()


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"\((?:guest|external|unverified)\)", "", value, flags=re.IGNORECASE)
    if "," in value:  # "Shah, Priya" -> "Priya Shah"
        last, _, first = value.partition(",")
        value = f"{first} {last}"
    value = re.sub(r"[^a-z0-9@. ]+", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip(" .")


@dataclass
class SpeakerMatcher:
    people: list[PersonRef]
    participants: list[dict[str, Any]] = field(default_factory=list)  # [{"entra_id", "name"}] from Graph
    local_ids: set[uuid.UUID] = field(default_factory=set)  # team members + meeting participants
    overrides: dict[str, str] = field(default_factory=dict)  # label -> person id

    def __post_init__(self) -> None:
        self._by_entra = {p.entra_id.lower(): p for p in self.people if p.entra_id}
        self._by_email = {p.email.lower(): p for p in self.people if p.email}
        self._by_name: dict[str, list[PersonRef]] = {}
        for p in self.people:
            for name in (p.display_name, *p.aliases):
                self._by_name.setdefault(normalize_name(name), []).append(p)

    def match(self, label: str) -> uuid.UUID | None:
        if label in self.overrides:
            return uuid.UUID(self.overrides[label])
        norm = normalize_name(label)
        if not norm or norm.startswith(("speaker ", "unknown")):
            return None
        # 1. Entra ID: Graph tells us which participant had this display name.
        for part in self.participants:
            if part.get("entra_id") and normalize_name(part.get("name") or "") == norm:
                person = self._by_entra.get(str(part["entra_id"]).lower())
                if person:
                    return person.id
        # 2. email
        if "@" in norm and norm in self._by_email:
            return self._by_email[norm].id
        # 3. exact display name / alias
        exact = self._unique(self._by_name.get(norm, []))
        if exact:
            return exact.id
        # 4. fuzzy
        fuzzy = self._fuzzy(norm)
        if fuzzy:
            return fuzzy.id
        # 5. unique first name among the people this meeting is about
        if " " not in norm:
            local = [p for p in self.people if p.id in self.local_ids]
            hits = [p for p in local if normalize_name(p.display_name).split(" ")[0] == norm]
            if len(hits) == 1:
                return hits[0].id
        return None

    def _unique(self, candidates: Iterable[PersonRef]) -> PersonRef | None:
        found = {p.id: p for p in candidates}
        if len(found) == 1:
            return next(iter(found.values()))
        local = [p for p in found.values() if p.id in self.local_ids]
        return local[0] if len(local) == 1 else None

    def _fuzzy(self, norm: str) -> PersonRef | None:
        scored: list[tuple[float, PersonRef]] = []
        for name, persons in self._by_name.items():
            ratio = SequenceMatcher(None, norm, name).ratio()
            if ratio >= FUZZY_THRESHOLD:
                scored.extend((ratio, p) for p in persons)
        if not scored:
            return None
        best = max(r for r, _ in scored)
        return self._unique(p for r, p in scored if r == best)
