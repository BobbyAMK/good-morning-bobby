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

create table if not exists kv (
  k          text primary key,
  v          text not null,
  updated_at text not null
);

pragma user_version = 1;
