"""Pure-function tests -- no network, no Ollama, no mocks.

These cover roughly the bugs you will actually have: markup escaping, tag-aware
chunking, and local usage accounting. They need zero infrastructure, which
is why they run on every commit.
"""

from __future__ import annotations

import re

import pytest
from pa_core.delivery.telegram import (
    TG_LIMIT,
    chunk_html,
    escape_markdown_v2,
    markdown_to_telegram_html,
)
from pa_core.llm.base import Usage
from pa_core.llm.usage import price_usd


# ----------------------------------------------------------------- telegram
def _balanced(s: str) -> bool:
    stack: list[str] = []
    for closing, tag in re.findall(r"<(/?)(b|i|u|s|code|pre|a|blockquote)\b[^>]*>", s):
        if closing:
            if not stack or stack[-1] != tag:
                return False
            stack.pop()
        else:
            stack.append(tag)
    return not stack


def test_html_escapes_only_the_three_characters_it_must():
    # The whole reason we use HTML mode: MarkdownV2 would need 18 characters escaped,
    # including the period, and LLM prose always has periods.
    risky = "Load: 4h15m (3 external) - protect tonight. Cost: $0.09."
    assert escape_markdown_v2(risky) != risky
    assert "\\" not in markdown_to_telegram_html(risky)


@pytest.mark.parametrize(
    "src,want",
    [
        ("**bold**", "<b>bold</b>"),
        ("*em*", "<i>em</i>"),
        ("`code()`", "<code>code()</code>"),
        ("# Heading", "<b>Heading</b>"),
        ("- item", "• item"),
        ("a & b", "a &amp; b"),
        ("<tag>", "&lt;tag&gt;"),
        ("[x](https://e.com?a=1&b=2)", '<a href="https://e.com?a=1&amp;b=2">x</a>'),
    ],
)
def test_markdown_subset_converts(src, want):
    assert want in markdown_to_telegram_html(src)


def test_unclosed_fence_never_ships_an_unclosed_tag():
    out = markdown_to_telegram_html("text\n```\nnever closed")
    assert out.count("<pre>") == out.count("</pre>") == 1


def test_chunks_are_individually_valid_html():
    big = "\n\n".join(f"**Section {i}**\n" + "x" * 400 for i in range(30))
    parts = chunk_html(markdown_to_telegram_html(big))
    assert len(parts) > 1
    assert all(len(p) <= TG_LIMIT for p in parts)
    assert all(_balanced(p) for p in parts)
    assert all("<" not in p.split(">")[-1] for p in parts)


def test_never_splits_an_html_entity():
    parts = chunk_html(markdown_to_telegram_html("x & y " * 1200))
    assert all(not re.search(r"&[a-z]{0,8}$", p) for p in parts)


# ----------------------------------------------------------------- pricing
def test_local_inference_has_no_api_cost():
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert price_usd("qwen2.5:7b-instruct-q4_K_M", u) == 0.0
