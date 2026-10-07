from pa_core.store import db, migrate


def test_fresh_database_contains_only_brief_state(tmp_path):
    with db.connect(tmp_path / "platform.db") as con:
        migrate.migrate(con, db.MIGRATIONS)
        tables = {
            row[0] for row in con.execute("select name from sqlite_master where type='table'")
        }
        assert tables == {
            "agent_run",
            "llm_call",
            "delivery",
            "kv",
            "sent_item",
            "search_result",
            "saved_note",
            "saved_item",
        }
        assert con.execute("pragma user_version").fetchone()[0] == 4
        assert migrate.migrate(con, db.MIGRATIONS) == []


def test_upgrade_preserves_briefs_notes_and_telegram_binding(tmp_path):
    with db.connect(tmp_path / "platform.db") as con:
        migrate.migrate(con, db.MIGRATIONS)
        con.executescript("""
            create table http_cache (url text);
            create table persona_profile (slug text);
            create table mail_decision (gmail_id text);
            create table brief_action (what text);
            insert into kv values ('telegram:chat_id', '123', '2026-10-07');
            insert into agent_run
                (run_id, agent, logical_date, status, started_at)
                values ('r1', 'morning-brief', '2026-10-07', 'ok', '2026-10-07');
            insert into delivery (run_id, sink, external_id, delivered_at, ok)
                values ('r1', 'telegram', '42', '2026-10-07', 1);
            insert into sent_item (url_key, title, section, url, sent_on, n, summary)
                values ('https://example.com', 'Release', 'ai', 'https://example.com',
                        '2026-10-07', 1, 'New release');
            insert into saved_note values (43, 42, '2026-10-07', '[]');
            insert into saved_item values ('https://example.com', 43, '2026-10-07');
            insert into search_result values
                (44, 1, 'Repo', 'https://github.com/a/b', 'Repo', 'search', '2026-10-07');
            pragma user_version = 3;
        """)
        retained = (
            "kv",
            "agent_run",
            "delivery",
            "sent_item",
            "saved_note",
            "saved_item",
            "search_result",
        )
        before = {table: list(con.execute(f"select * from {table}")) for table in retained}
        assert migrate.migrate(con, db.MIGRATIONS) == ["0004_brief_only.sql"]
        assert {table: list(con.execute(f"select * from {table}")) for table in retained} == before
        tables = {
            row[0] for row in con.execute("select name from sqlite_master where type='table'")
        }
        assert not tables & {"http_cache", "persona_profile", "mail_decision", "brief_action"}
