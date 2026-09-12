-- 0001_platform_init.sql
--
-- IMPORTANT: `pragma user_version = N;` MUST be the LAST statement in this file.
-- sqlite3.Connection.executescript() issues an implicit COMMIT before running, so
-- wrapping it in your own BEGIN IMMEDIATE does nothing and you get a silently
-- non-atomic migration. Putting the version bump inside the script makes it part of
-- the same unit, and migrate.py's post-condition check turns a half-applied file
-- into a loud failure instead of a quiet one.

create table if not exists agent_run (
  run_id        text primary key,
  agent         text not null,
  -- The LOCAL date this run is FOR -- not wall-clock. A brief for 2026-09-12 is
  -- produced once, whether it runs at 06:05, at the 09:00 catch-up, or at 12:30
  -- because the Mac woke late. This single column is what makes scheduling the
  -- same job at three times cost nothing.
  logical_date  text not null,
  dry_run       integer not null default 0,
  status        text not null check (status in ('running','ok','failed','skipped')),
  started_at    text not null,
  finished_at   text,
  duration_ms   integer,
  usd           real not null default 0.0,
  git_sha       text,
  host          text,
  error_type    text,
  error_msg     text,
  metrics_json  text,
  -- THE idempotency guard. "Already ran today" is enforced by the database,
  -- not by an if-statement that someone will forget to write.
  unique (agent, logical_date, dry_run)
);
create index if not exists agent_run_recent on agent_run(agent, started_at desc);

create table if not exists llm_call (
  id            integer primary key,
  run_id        text references agent_run(run_id) on delete cascade,
  agent         text not null,
  task          text not null,
  backend       text not null,
  model         text not null,
  sensitive     integer not null,
  input_tokens  integer not null default 0,
  output_tokens integer not null default 0,
  cache_read_tokens  integer not null default 0,
  cache_write_tokens integer not null default 0,
  cost_usd      real not null default 0.0,
  latency_ms    integer not null default 0,
  ok            integer not null,
  error         text,
  created_at    text not null
);
create index if not exists llm_call_month on llm_call(created_at);
create index if not exists llm_call_run on llm_call(run_id);

create table if not exists http_cache (
  url             text primary key,
  final_url       text,
  status          integer,
  fetched_at      text not null,
  expires_at      text,
  content_type    text,
  body            blob,
  sha256          text,
  -- scraping-rot telemetry: a fetch returning 60 KB of HTML and 40 chars of text
  -- is rot. Recording the ratio is how you learn BEFORE the brief silently empties.
  raw_bytes       integer,
  extracted_chars integer
);

create table if not exists delivery (
  id           integer primary key,
  run_id       text references agent_run(run_id) on delete cascade,
  sink         text not null,
  external_id  text,
  delivered_at text not null,
  ok           integer not null,
  error        text
);
create index if not exists delivery_run on delivery(run_id);

create table if not exists persona_profile (
  slug          text not null,
  version       integer not null,
  created_at    text not null,
  source_hash   text not null,
  profile_json  text not null,
  style_card_md text not null,
  is_active     integer not null default 0,
  primary key (slug, version)
);

create table if not exists kv (
  k          text primary key,
  v          text not null,
  updated_at text not null
);

-- Per-message triage decisions, shared by the morning brief (project 1) and inbox
-- triage (project 3). Keeping this in SQLite rather than relying on Gmail labels is
-- what lets you ask "what did the agent think last Tuesday, and why".
create table if not exists mail_decision (
  gmail_id       text not null,
  run_id         text references agent_run(run_id) on delete cascade,
  thread_id      text,
  from_addr      text,
  from_domain    text,
  subject        text,
  received_at    text,
  rule_hit       text,          -- name of the deterministic rule that fired, or null
  bucket         text,          -- needs_attention | needs_reply | ignore | uncertain
  confidence     real,
  reason         text,
  model          text,
  prompt_version text,
  -- derived later from your own behaviour, for the backtest in project 3
  ground_truth   text,
  gt_source      text,
  primary key (gmail_id, run_id)
);
create index if not exists mail_decision_gmail on mail_decision(gmail_id);

-- Yesterday's actions, so the brief can say "still open, 2 days" instead of
-- re-deriving the same three suggestions every morning.
create table if not exists brief_action (
  id           integer primary key,
  logical_date text not null,
  idx          integer not null,
  what         text not null,
  why_now      text,
  evidence_ref text,
  thread_id    text,
  -- resolved by CODE on the next run, not self-reported by the model
  resolved_on  text,
  resolution   text,           -- done | still_open | dropped
  carry_count  integer not null default 0,
  unique (logical_date, idx)
);

pragma user_version = 1;
