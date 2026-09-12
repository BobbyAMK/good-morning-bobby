# pet-agents

Ten small agents that support daily work. Self-hosted, Python 3.14 + uv, scheduled by
launchd, **$0/month** (local Ollama, local embeddings, no paid API).

Plan: `~/.claude/plans/t-i-ang-c-d-sorted-perlis.md`
Measured facts about this machine: [docs/measured-facts.md](docs/measured-facts.md)
Eval sets and what they found: [evals/README.md](evals/README.md)

## Status

| | |
|---|---|
| `packages/core` | **built and tested** — config, secrets (Keychain), SQLite + migrations, run harness, LLM router, Google connectors, mail rule layer, Telegram/file/notify delivery |
| `packages/morning-brief` (project 1) | **built and tested end to end** against fake Google; needs your Google credentials to run for real |
| projects 2–10 | not started |

## Quick start

Three things must be done by hand before the brief can run for real.

### 1. Wake the Mac before the job (`just wake`)

Apple's docs: a missed `StartCalendarInterval` runs on **wake**, but if the Mac was
**off** it is skipped entirely. A closed-lid MacBook may not wake at all. Without this,
your 06:00 brief arrives whenever you next open the lid.

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 05:58:00
pmset -g sched
```

The plist also fires at 09:00 and 12:30 as a backstop; the DB constraint
`unique(agent, logical_date, dry_run)` makes the extra firings free.

### 2. Google OAuth — publish the app, or the token dies weekly

In Google Cloud Console: enable the Gmail and Calendar APIs, create an OAuth client of
type **Desktop app**, and set the app's publishing status to **"In production"**.

Leaving it in "Testing" issues a refresh token that **expires after 7 days**. You do not
need verification or a security assessment — Google lists personal use as an explicit
exception for restricted scopes. You will click past one "unverified app" warning.

```bash
just google-client file=~/Downloads/client_secret_xxx.json   # stored in the Keychain
just google-login                                            # gmail.readonly + calendar.readonly
```

### 3. Telegram

```bash
# @BotFather -> /newbot -> copy the token
just telegram-token      # keyring set pet-agents telegram_token
# send your new bot any message, then:
just telegram-bind       # pins YOUR chat id as the only accepted inbound chat
just telegram-test       # a message full of MarkdownV2 landmines, to prove HTML mode works
```

### Then

```bash
just doctor              # every silently-breaking thing, checked in one place
just bench               # real tok/s on this machine
just brief-dry           # compute and print; touches nothing
just brief               # for real
just install-p1          # launchd: morning-brief + watchdog
just kick morning-brief  # test the 6am job at 3pm
```

## Design notes worth knowing

- **`sensitive=True` is the default on every LLM call.** A call must explicitly opt out
  to be allowed off this machine. Mail, calendar, notes and money cannot leak by
  omission. Verified reason: Google's Gemini API terms say free-tier content is used to
  improve their products and that human reviewers may read it.
- **`num_ctx=8192`, not the model's nominal 256K.** At 16 GB the KV cache is the binding
  constraint. This is why the brief is map-reduce: a single 21K-token call does not fit,
  and measured here the 7B prefills at ~250 tok/s, so 8K tokens is ~30s before a token
  comes out.
- **Telegram uses HTML mode, not MarkdownV2.** MarkdownV2 needs 18 characters escaped
  including the period; LLM prose always has periods, and a parse error at 6am means
  silent non-delivery.
- **Everything that can be arithmetic is arithmetic.** Sections 1, 3 and 6 of the brief
  never touch an LLM. Section 3 used to be asked of the model and it silently dropped a
  real deadline, so it is now derived in code from the same data.
- **The rule layer retires 70–85% of the inbox at zero token cost**, deterministically.
  Section 6 of the brief always reports what it discarded: a filter that will not tell
  you what it dropped is a filter you stop believing within a week.
