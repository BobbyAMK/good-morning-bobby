from datetime import UTC, datetime

from pa_core.delivery.markdown_file import MarkdownFileSink
from pa_core.delivery.telegram import markdown_to_telegram_html
from pa_core.text import plain_dashes
from pa_morning_brief.tech_render import _safe


def test_source_and_model_prose_uses_plain_dashes():
    text = "Release \u2014 faster inference in versions 2\u20133"
    expected = "Release - faster inference in versions 2-3"
    assert plain_dashes(text) == expected
    assert _safe(text) == expected
    assert markdown_to_telegram_html(f"**{text}**") == f"<b>{expected}</b>"


def test_required_url_characters_are_preserved():
    url = "https://example.com/release\u2013notes"
    text = f"[Release \u2014 notes]({url})"
    assert plain_dashes(text) == f"[Release - notes]({url})"
    assert markdown_to_telegram_html(text) == f'<a href="{url}">Release - notes</a>'


def test_archive_normalizes_both_title_and_body(tmp_path):
    sink = MarkdownFileSink(tmp_path, agent="morning-brief", now=datetime(2026, 10, 7, tzinfo=UTC))
    result = sink.deliver(
        title="Tech Brief \u2014 today",
        body_markdown="Version 2\u20133 is available.",
        run_id="test",
    )
    assert result.ok
    text = next(tmp_path.rglob("*.md")).read_text()
    assert "title: Tech Brief - today" in text
    assert "Version 2-3 is available." in text
    assert "\u2013" not in text and "\u2014" not in text
