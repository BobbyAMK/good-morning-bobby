# Eval sets

Not vibes. Each file is a small set of labelled examples with a threshold, so "can the
small model do this, or do I need the large one / the cloud?" is a measurement rather
than a guess. On a zero-cost path where you WILL swap local models, this is the
difference between engineering and superstition.

Run: `uv run python evals/run_mail_triage.py`

## mail_triage.jsonl

10 labelled messages. Threshold: **bucket accuracy >= 0.85**, and `needs_reply` recall
must be **1.0** -- the asymmetry is deliberate, because missing a reply costs a client
and over-flagging a newsletter costs two seconds.

Measured 2026-09-12 on the 4-message subset that became m1-m4:

| model | bucket accuracy | notes |
|---|---|---|
| `qwen2.5:3b-instruct` | 3/4 | every `timing` wrong -- called a stated card-expiry date "none" |
| `qwen2.5:7b-instruct-q4_K_M`, one batched call | **1/4** | returned ONE verdict and stopped; not truncated, just done |
| `qwen2.5:7b-instruct-q4_K_M` + completeness guard | **4/4** | correct timings, real verbatim quotes |

Three findings that changed the design, in order of importance:

1. **A `list[...]` schema with no minimum is satisfied by one element.** Constrained
   decoding guarantees shape, never completeness. Batching 4 messages made the 7B answer
   one of them. The fix is code, not prompting: batch for speed, then re-ask the missing
   ids one at a time. That alone took 1/4 -> 4/4.
2. **Example phrasings in a prompt get copied as CONTENT.** Given `good: "wants an answer
   before Friday's board meeting"` as a style example, the 7B wrote exactly that about a
   message with no board meeting in it. Fabricated urgency is worse than an ugly verbatim
   quote. Hence `timing` is a closed enum and `evidence_quote` must be a literal
   substring that code verifies before rendering.
3. **The 3B is not good enough for this**, despite the plan assuming "classify -> small
   model". Measurement overruled the plan.

Still open: the REDUCE step attributes one sender's request to another (it wrote
"Lan Nguyen's quick question on the runbook" when the runbook question came from Tuan),
even with `from:` repeated on every input line. No fix found at 7B. This is the clearest
place where the paid path buys something real.

## Result on the full 10 cases (2026-09-12, `qwen2.5:7b-instruct-q4_K_M`)

    bucket accuracy    9/10 = 0.90   PASS (threshold 0.85)
    needs_reply recall 4/5  = 0.80   FAIL (threshold 1.00)
    timing accuracy    6/9  = 0.67
    29.5s, 6 calls (4 backfilled by the completeness guard)

**The recall threshold fails, and it is not being tuned away.** m10 ("Action required:
countersign by Friday") is both a stated deadline and a response owed; step 1 of the
rubric says needs_reply wins, and the model chose needs_attention. Tuning the prompt
until 10 examples go green would be overfitting to 10 examples, not an improvement.

How much this actually costs, for the morning brief specifically: **not much.** Sections
2 and 3 draw on `needs_reply` AND `needs_attention` together, so m10 still appears on the
page — only its label is wrong. The distinction becomes expensive in project 3, which
decides whether to draft a reply. So the honest position is:

- For project 1: ship it. 0.90 bucket accuracy with both buckets surfacing is fine.
- Before project 3 drafts anything, grow this set to ~40 real messages from your own
  archive (project 3's behavioural backtest generates them for free) and re-measure.
  Do not give a 0.80-recall classifier the ability to write replies.

`timing` accuracy of 0.67 is the weakest number here. It only affects one phrase of
rendered text, and the `evidence_quote` grounding guard means a wrong timing cannot
invent a fact -- it can only pick the wrong label for a real quote.
