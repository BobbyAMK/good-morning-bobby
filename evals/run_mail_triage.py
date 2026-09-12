"""Measure mail-triage accuracy on this machine, for any configured model.

uv run python evals/run_mail_triage.py
uv run python evals/run_mail_triage.py qwen2.5:3b-instruct
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "packages/core/src"), str(ROOT / "packages/morning-brief/src")]

from pa_core.connectors.base import MailMessage  # noqa: E402
from pa_core.llm.ollama import OllamaBackend  # noqa: E402
from pa_core.mailrules import RuleConfig, apply  # noqa: E402
from pa_morning_brief.agent import MAP_BATCH, _fmt_mail  # noqa: E402
from pa_morning_brief.prompts import MAP_SYSTEM, MAP_USER  # noqa: E402
from pa_morning_brief.schema import MailBatch  # noqa: E402

CASES = [
    json.loads(ln)
    for ln in (Path(__file__).parent / "mail_triage.jsonl").read_text().splitlines()
    if ln.strip()
]
RULES = RuleConfig(internal_domains=frozenset({"mycorp.com"}), my_email="me@mycorp.com")


def as_msg(c: dict) -> MailMessage:
    return MailMessage(
        id=c["id"],
        thread_id="t" + c["id"],
        from_addr=c["from"],
        from_name="",
        to=("me@mycorp.com",),
        cc=(),
        subject=c["subject"],
        snippet=c["body"][:120],
        received_at=datetime.now(UTC),
        is_unread=True,
        labels=("INBOX", "UNREAD"),
        headers={},
        body_text=c["body"],
    )


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5:7b-instruct-q4_K_M"
    be = OllamaBackend(num_ctx=8192)
    be.complete(
        system="ok",
        user="hi",
        model=model,
        max_tokens=5,
        temperature=0.0,
        schema=None,
        cacheable_prefix=None,
        timeout_s=120,
    )

    # Anything the deterministic rules decide never reaches the model -- score it as-is.
    to_llm, by_rule = [], {}
    for c in CASES:
        m = as_msg(c)
        d = apply(m, RULES)
        if d is None:
            to_llm.append((c, m))
        else:
            by_rule[c["id"]] = str(d.bucket)

    got: dict[str, object] = {}
    t0, calls = time.monotonic(), 0

    def ask(batch: list[MailMessage]) -> None:
        nonlocal calls
        calls += 1
        r = be.complete(
            system=MAP_SYSTEM,
            user=MAP_USER.format(chunk="\n".join(_fmt_mail(m) for m in batch), index=1, total=1),
            model=model,
            max_tokens=1400,
            temperature=0.0,
            schema=MailBatch,
            cacheable_prefix=None,
            timeout_s=300,
        )
        for v in r.parsed.verdicts:  # type: ignore[union-attr]
            got[v.msg_id] = v

    msgs = [m for _, m in to_llm]
    for i in range(0, len(msgs), MAP_BATCH):
        ask(msgs[i : i + MAP_BATCH])
    missing = [m for m in msgs if m.id not in got]
    for m in missing:  # the completeness guard
        ask([m])
    wall = time.monotonic() - t0

    right = tq = 0
    nr_total = nr_found = 0
    print(f"\nmodel={model}  {wall:.1f}s  {calls} calls  backfilled={len(missing)}\n")
    for c in CASES:
        want, mid = c["bucket"], c["id"]
        if mid in by_rule:
            gb, gt, src = by_rule[mid], "-", "rule"
        else:
            v = got.get(mid)
            gb = getattr(v, "bucket", "MISSING")
            gt = getattr(v, "timing", "-")
            src = "llm"
            if v is not None and gt == c["timing"]:
                tq += 1
        ok = gb == want
        right += ok
        if want == "needs_reply":
            nr_total += 1
            nr_found += ok
        print(
            f"  {'ok   ' if ok else 'WRONG'} {mid:4s} {src:4s} got={gb:17s} want={want:17s} "
            f"timing={gt:14s} | {c['note'][:52]}"
        )

    acc = right / len(CASES)
    rec = nr_found / nr_total if nr_total else 1.0
    n_llm = len(to_llm)
    print(f"\n  bucket accuracy   {right}/{len(CASES)} = {acc:.2f}   (threshold 0.85)")
    print(f"  needs_reply recall {nr_found}/{nr_total} = {rec:.2f}   (threshold 1.00)")
    print(f"  timing accuracy   {tq}/{n_llm} = {tq / n_llm:.2f}   (of those the LLM saw)")
    print(f"  rules handled     {len(by_rule)}/{len(CASES)} at zero token cost")
    return 0 if (acc >= 0.85 and rec >= 1.0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
