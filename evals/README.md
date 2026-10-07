# Morning brief evaluations

Offline tests cover source parsing, freshness, duplicate filtering, summary number
checks, Telegram formatting, software-search behavior, saved notes, and watchdogs:

```bash
just test
```

## Live software search

```bash
uv run python evals/run_search.py
```

This manual evaluation uses the configured local Ollama model and real GitHub, npm,
PyPI, and Hacker News services. It needs network access but does not send Telegram
messages; replies are captured locally and state uses an in-memory SQLite database.

Five software queries check whether enough expected repositories appear in the top
five results. Two unrelated queries check that software search declines requests
outside its scope. The script prints per-case outcomes and returns a nonzero exit
code if any case fails.

The cases are a small regression set, not a general search-quality benchmark. Source
availability, upstream rankings, and the configured model can affect live results.
