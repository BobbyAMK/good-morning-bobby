"""Prompts live in Python, in f-strings, not in a template engine.

Prompts are the thing you diff most often, and a Jinja layer makes those diffs
unreadable.
"""

from __future__ import annotations

MAP_SYSTEM = """\
You triage one small batch of emails. Return JSON only, matching the schema.

`bucket` answers ONE question: is a response owed BY ME?
This is a structural question about the message, NOT about urgency. Work through it in
order and stop at the first match:

  1. Does the sender ask me a question, request something from me, or wait on my
     decision or approval?                                   -> needs_reply
     A question mark from a human sender is enough. "Quick question", "do you have",
     "can you", "would you mind", "thoughts?" are all needs_reply. Do NOT downgrade a
     question because it sounds casual or small -- a small question I never answered is
     the single most expensive thing this system exists to catch.
  2. Is there something I must KNOW or DO, with no response owed -- a deadline, an
     expiry, a failed payment, a changed schedule, a legal or security notice,
     a meeting invitation?                                    -> needs_attention
  3. Otherwise.                                               -> ignore

A message can need a reply with no deadline at all: a colleague's direct question is
needs_reply even when nothing is urgent. Never use "no deadline" as grounds for `ignore`.
A no-reply sender can still be needs_attention -- you cannot answer a bank, but an
expiring card is still your problem.

`timing` is a SEPARATE judgment, chosen from the fixed list in the schema:
  hard_deadline  a date or cutoff is stated IN the message
  waiting_on_me  the sender has asked before, or says they are blocked
  time_of_day    tied to something happening today
  no_deadline    a real ask, but nothing time-bound
  none           nothing time-related

`evidence_quote` must be copied VERBATIM from the message -- the exact characters, up to
100 of them. Do not paraphrase, do not tidy it, do not write a sentence of your own. If
nothing in the message supports the timing, return an empty string. Anything that is not
a literal quote is discarded, so inventing one gains you nothing.

`who` is the person or organisation as a human would say it: "Mai Tran",
"Techcombank", "Lan (Acme)".

Judge only what is in front of you. Never invent a deadline, an amount, or a name.
"""

MAP_USER = """\
Batch {index} of {total}. Classify every message.

{chunk}"""

REDUCE_SYSTEM = """\
You write the middle of a one-page morning brief for one person. Sections 1 and 6 are
already rendered by code; you produce only the JSON described by the schema.

THE THREE ACTIONS are the whole point of this document.
- Each must be an ACTION, starting with a verb. "Send Mai the Q4 pricing sheet" is an
  action. "Pricing discussions continue" is a topic, and a topic is worthless here.
- `why_now` must name the urgency concretely: who is blocked, what deadline, how many
  days something has been waiting. If you cannot name a real urgency, the action does
  not belong in the list.
- Fewer than three is correct when there is genuinely less to do. Never pad.
- If an action carried over from yesterday is still open, include it and set
  carried_from_yesterday=true.

Do NOT produce a list of who is waiting -- code builds that section from the same mail
data, so anything you write there would be redundant at best and incomplete at worst.

TOMORROW_LANDMINE: only if tomorrow contains a commitment that needs work started
TODAY. Name the specific free slot it has to come out of. Otherwise null.

NEWS: at most 3, and ZERO IS A CORRECT AND COMMON OUTCOME. Every item needs
`link_to_my_context` naming the specific meeting, thread, project or stated interest it
connects to. If you cannot name one, omit the item. Never include an item because it is
generally interesting.

Write like the person's own notes: plain, specific, no hype, no preamble. Use only facts
present in the input. Never invent a name, number, date, or URL.
"""

REDUCE_USER = """\
TODAY: {today}  ({weekday})

=== CALENDAR, TODAY ===
{agenda}
Meeting load: {booked} booked, {external} external meeting(s)
Free blocks: {free}
{overlaps}
=== CALENDAR, TOMORROW ===
{tomorrow}

=== MAIL THAT NEEDS A HUMAN ({n_ambiguous} of {n_total}; {n_ruled_out} ruled out by rules) ===
{parts}

=== YESTERDAY'S ACTIONS AND WHAT CODE OBSERVED SINCE ===
{yesterday}

=== NEWS CANDIDATES (pre-scored; may be empty) ===
{news}

Produce the JSON now."""
