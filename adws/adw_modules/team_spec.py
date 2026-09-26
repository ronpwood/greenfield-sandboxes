"""The team chain's living spec: `<context_handoff_dir>/plan.md`, parsed.

The planner writes it in a fixed eight-section form; every later agent may
annotate it. Three sections are FROZEN after `commit_plan` — what we are
solving for, the requirements, the expected values — and change only through
an amendment the reviewer rules on. The effective spec is the frozen sections
plus the accepted amendments.

This module is the one place the section names live. The gates and the prompts
use these exact strings; a heading that drifts from them is a missing section.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FROZEN = ("What we're solving for", "Requirements", "Expected values")
SECTIONS = FROZEN + ("Approach", "Left to the builder", "Traps", "Team notes", "Amendments")
RULINGS = ("proposed", "accepted", "rejected")

_REQ_ID = re.compile(r"^\s*[-*]\s*\**(R\d+)\b", re.M)
_VAL_ID = re.compile(r"^\s*\|\s*\**(V\d+)\**\s*\|", re.M)
_AMEND_HEAD = re.compile(r"^###\s+(A\d+)\b.*$", re.M)
_FIELD = r"^\*\*{}:\*\*\s*(.*)$"


@dataclass
class Amendment:
    id: str
    targets: str = ""
    change: str = ""
    ruling: str = ""                # the whole ruling line, e.g. "accepted by reviewer (review_1): …"

    @property
    def status(self) -> str:
        """`proposed | accepted | rejected`, or "" when the ruling names none of them."""
        word = self.ruling.strip().split(" ", 1)[0].rstrip(":").lower() if self.ruling.strip() else ""
        return word if word in RULINGS else ""

    @property
    def reason(self) -> str:
        """What follows the first colon of the ruling: the why of an accept or reject."""
        return self.ruling.split(":", 1)[1].strip() if ":" in self.ruling else ""

    @property
    def adds_value_ids(self) -> list[str]:
        """V ids this amendment introduces — named in Targets but defined only in Change."""
        return _VAL_ID.findall(self.change) or re.findall(r"\bV\d+\b", self.targets)


@dataclass
class TeamSpec:
    sections: dict[str, str] = field(default_factory=dict)
    requirement_ids: list[str] = field(default_factory=list)
    value_ids: list[str] = field(default_factory=list)
    value_rows: list[list[str]] = field(default_factory=list)   # cells of each V row, id first
    amendments: list[Amendment] = field(default_factory=list)
    traps: list[str] = field(default_factory=list)              # top-level list items in ## Traps

    def trap_value_refs(self, trap: str) -> list[str]:
        """The V ids a trap names, e.g. "(V12, V40)". A trap with none is prose nobody sweeps."""
        return re.findall(r"\bV\d+\b", trap)


def _list_items(body: str) -> list[str]:
    """Top-level `- ` / `* ` items, each with its continuation lines folded in."""
    items: list[str] = []
    for line in body.splitlines():
        if re.match(r"^[-*]\s+", line):
            items.append(line.strip())
        elif items and line.startswith((" ", "\t")) and line.strip():
            items[-1] += " " + line.strip()
    return items


def _split_sections(text: str) -> dict[str, str]:
    """`## ` headings to their bodies. A heading inside a fenced code block is text."""
    sections: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(body).strip("\n")
            current, body = line[3:].strip(), []
            continue
        if current is not None:
            body.append(line)
    if current is not None:
        sections[current] = "\n".join(body).strip("\n")
    return sections


def _amendments(body: str) -> list[Amendment]:
    heads = list(_AMEND_HEAD.finditer(body))
    found = []
    for i, head in enumerate(heads):
        block = body[head.end(): heads[i + 1].start() if i + 1 < len(heads) else len(body)]

        def get(name: str) -> str:
            m = re.search(_FIELD.format(re.escape(name)), block, re.M)
            return m.group(1).strip() if m else ""

        found.append(Amendment(id=head.group(1), targets=get("Targets"),
                               change=block, ruling=get("Ruling")))
    return found


def parse(text: str) -> TeamSpec:
    sections = _split_sections(text)
    values = sections.get("Expected values", "")
    rows = []
    for line in values.splitlines():
        m = _VAL_ID.match(line)
        if m:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(cells)
    return TeamSpec(
        sections=sections,
        requirement_ids=_REQ_ID.findall(sections.get("Requirements", "")),
        value_ids=[r[0].strip("*") for r in rows],
        value_rows=rows,
        amendments=_amendments(sections.get("Amendments", "")),
        traps=_list_items(sections.get("Traps", "")),
    )


def effective_value_ids(spec: TeamSpec) -> list[str]:
    """The frozen V ids, plus any V id an ACCEPTED amendment adds. Document order."""
    ids = list(spec.value_ids)
    for a in spec.amendments:
        if a.status == "accepted":
            ids.extend(v for v in a.adds_value_ids if v not in ids)
    return ids
