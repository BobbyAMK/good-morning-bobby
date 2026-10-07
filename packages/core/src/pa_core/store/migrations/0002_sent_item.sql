-- Every item a tech brief has carried, so tomorrow's brief never repeats it.
-- Keyed by (url_key, sent_on) rather than url_key alone: a same-day re-run (`--force`)
-- must reproduce today's items, so dedup only looks at EARLIER logical dates.
create table sent_item (
  url_key  text not null,
  title    text not null,
  section  text not null,
  url      text not null,
  sent_on  text not null,   -- logical_date of the brief that carried it
  primary key (url_key, sent_on)
);
create index sent_item_sent_on on sent_item (sent_on);

pragma user_version = 2;
