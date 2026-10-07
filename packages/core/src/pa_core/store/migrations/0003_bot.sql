-- Two-way bot: numbered brief items, saved notes, and /search results you can save.

-- The brief's item number, so "reply 3 7" can find items 3 and 7 of that day's brief.
alter table sent_item add column n integer;
alter table sent_item add column summary text not null default '';

-- Every /search reply, numbered, keyed by the Telegram message that carried it.
create table search_result (
  message_id  integer not null,
  n           integer not null,
  title       text not null,
  url         text not null,
  line_md     text not null,     -- the exact line shown, reused verbatim in a note
  query       text not null,
  created_at  text not null,
  primary key (message_id, n)
);

-- One row per save action = one pinned note message.
create table saved_note (
  note_message_id    integer primary key,
  source_message_id  integer not null,
  created_at         text not null,
  items_json         text not null
);

-- Which items were already saved, so saving twice points at the existing note.
create table saved_item (
  url_key          text primary key,
  note_message_id  integer not null,
  saved_at         text not null
);

pragma user_version = 3;
