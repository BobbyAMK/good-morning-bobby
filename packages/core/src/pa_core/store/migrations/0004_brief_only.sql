-- Retire unused tables in installations created before the brief-only scope.
-- Active brief history, Telegram bindings, search results, and saved notes stay intact.
drop table if exists http_cache;
drop table if exists persona_profile;
drop table if exists mail_decision;
drop table if exists brief_action;

pragma user_version = 4;
