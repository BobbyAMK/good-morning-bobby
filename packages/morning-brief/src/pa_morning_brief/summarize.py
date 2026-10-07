"""One-sentence English summaries, written by the local model, checked by code.

The model sees only the text the source itself published -- never the open web, never its
own memory -- and a summary that states a number or version the source does not contain is
thrown away for the source's own first sentence. A 7B model paraphrasing "v0.9.2" into
"version 1.0" is exactly the kind of error nobody notices on a phone at 6am.
"""

from __future__ import annotations

import re

from pa_core.harness.run import RunContext
from pa_core.llm.base import Task
from pydantic import BaseModel, Field

from .items import Item

BATCH = 5
MIN_TEXT = 40  # below this there is nothing to summarise; the title stands alone
# Source text this short is already a summary, so it is used VERBATIM. Measured: given
# "...and checks in much less frequently than with earlier models", the 7B wrote "system
# monitoring less frequently" -- a reversal of meaning no number check can catch. The model
# only condenses what is too long to show (release notes, full posts).
VERBATIM_MAX = 280

SYSTEM = """\
You write one-line summaries for a developer's morning tech brief.

For each item, write ONE plain English sentence of at most 25 words saying what is new or
what happened. Use ONLY the text given for that item. Do not add facts, numbers, versions,
names or opinions that are not in that text. No hype words ("revolutionary", "game-changer",
"exciting"). Do not start with "This article" or repeat the title word for word.

If the text does not say what is new, return an empty string for that item.
Use ASCII hyphens instead of typographic dashes.
Return every item id you were given, exactly once."""

USER = """\
Summarise each item.

{items}"""


class ItemSummary(BaseModel):
    model_config = {"extra": "forbid"}
    id: str
    summary: str = Field(max_length=220)


class SummaryBatch(BaseModel):
    model_config = {"extra": "forbid"}
    summaries: list[ItemSummary]


_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def grounded(summary: str, source: str) -> bool:
    """Every number/version in the summary must appear in the source text."""
    hay = source.replace(",", "")
    return all(n.replace(",", "") in hay for n in _NUM.findall(summary))


def first_sentence(text: str, limit: int = 180) -> str:
    s = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


def summarize(ctx: RunContext, items: list[Item]) -> dict[str, int]:
    """Fills item.summary in place. Returns counts for metrics."""
    stats = {"verbatim": 0, "summarized": 0, "fallback": 0}
    for it in items:
        if MIN_TEXT <= len(it.text) <= VERBATIM_MAX:
            it.summary = it.text
            stats["verbatim"] += 1
    todo = {f"i{n}": it for n, it in enumerate(items) if len(it.text) > VERBATIM_MAX}
    got: dict[str, str] = {}

    def run(ids: list[str]) -> None:
        block = "\n\n".join(
            f"--- id:{i}\nTitle: {todo[i].title}\nText: {todo[i].text[:900]}" for i in ids
        )
        res = ctx.llm.complete(
            task=Task.SUMMARIZE,
            system=SYSTEM,
            user=USER.format(items=block),
            agent=ctx.agent,
            run_id=ctx.run_id,
            # Public source text, summarized by the local Ollama backend.
            sensitive=False,
            schema=SummaryBatch,
            max_tokens=900,
        )
        for s in res.parsed.summaries:  # type: ignore[union-attr]
            if s.id in todo:
                got[s.id] = s.summary.strip()

    ids = list(todo)
    for n in range(0, len(ids), BATCH):
        run(ids[n : n + BATCH])
    # Completeness guard: a list schema with no minimum is
    # satisfied by one element, so a batch can come back short. Re-ask one at a time.
    for i in [i for i in ids if i not in got]:
        run([i])

    for i, it in todo.items():
        s = got.get(i, "")
        if s and grounded(s, f"{it.title} {it.text}"):
            it.summary = s
            stats["summarized"] += 1
        else:
            if s:
                ctx.log.warning("summary.ungrounded", title=it.title[:60], summary=s[:100])
            it.summary = first_sentence(it.text)
            stats["fallback"] += 1
    return stats
