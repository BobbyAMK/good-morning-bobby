"""The brief's output contract.

Hard caps live in the SCHEMA, not in the prompt, so the model cannot pad its way past
them. `link_to_my_context` being REQUIRED is the anti-slop lever: a model cannot fill
that field with "this is important for the industry" without it looking obviously wrong.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Action(BaseModel):
    model_config = {"extra": "forbid"}

    what: str = Field(max_length=110, description="An action, not a topic. Start with a verb.")
    why_now: str = Field(
        max_length=120,
        description="Name the urgency concretely: who is blocked, what deadline, how long waiting.",
    )
    evidence_ref: str = Field(description="The message id or calendar event this comes from.")
    carried_from_yesterday: bool = False


class NeedsYou(BaseModel):
    model_config = {"extra": "forbid"}

    who: str = Field(max_length=60)
    what: str = Field(max_length=90)
    why_now: str = Field(max_length=60)
    msg_id: str


class NewsItem(BaseModel):
    model_config = {"extra": "forbid"}

    headline: str = Field(max_length=90)
    why_it_matters_to_me: str = Field(max_length=150)
    url: str
    link_to_my_context: str = Field(
        max_length=120,
        description=(
            "The specific meeting, thread, project or stated interest this connects to. "
            "If you cannot name one, OMIT the item entirely."
        ),
    )


class BriefBody(BaseModel):
    """What the REDUCE step returns.

    Sections 1, 3 and 6 are built by CODE, not by the model. Section 3 used to be asked
    of the model and it was unreliable: it would list two items and silently drop a real
    deadline, or repeat section 2 verbatim. The MAP step already produced who/timing per
    message -- that IS section 3, so deriving it in code makes it complete by
    construction and leaves the model only the judgment call (which three things matter).
    """

    model_config = {"extra": "forbid"}

    actions: list[Action] = Field(default_factory=list, max_length=3)
    tomorrow_landmine: str | None = Field(default=None, max_length=280)
    news: list[NewsItem] = Field(default_factory=list, max_length=3)
    notes: str = Field(default="", max_length=200)

    # Populated in code after the call, never by the model.
    needs_you: list[NeedsYou] = Field(default_factory=list, exclude=True)


Bucket = Literal["needs_reply", "needs_attention", "ignore"]

# A CLOSED SET, not free text. Measured finding: given example phrasings for a free-text
# urgency field, a 7B model copies the EXAMPLE as CONTENT -- it wrote "wants an answer
# before Friday's board meeting" about a message containing no board meeting. Fabricated
# urgency on a morning brief sends you chasing something that does not exist. An enum
# makes that failure structurally impossible: the model can pick wrong, but it cannot
# invent.
Timing = Literal[
    "hard_deadline",  # a date or cutoff is stated in the message
    "waiting_on_me",  # the sender has asked before, or is explicitly blocked
    "time_of_day",  # tied to something happening today
    "no_deadline",  # a real ask with no time pressure
    "none",  # nothing time-related
]


class MailVerdict(BaseModel):
    """What the MAP step returns, per message. Deliberately small: a 7B model loses
    track across wide schemas."""

    model_config = {"extra": "forbid"}

    msg_id: str
    bucket: Bucket
    timing: Timing
    # Must be a LITERAL substring of the message, so code can verify it. Anything the
    # model paraphrases or invents is dropped in agent.py rather than rendered.
    evidence_quote: str = Field(
        default="",
        max_length=100,
        description="Copy up to 100 characters VERBATIM from the message that justify "
        "the timing. Exact characters only. Empty string if none apply.",
    )
    who: str = Field(max_length=60, description="Person or organisation, as a human would say it.")


class MailBatch(BaseModel):
    model_config = {"extra": "forbid"}

    verdicts: list[MailVerdict] = Field(default_factory=list)
