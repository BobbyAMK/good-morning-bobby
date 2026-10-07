# Historical morning brief measurements

These measurements were recorded on an Apple M1 Pro with 16 GB unified memory.
They are historical observations, not results from the current checkout.

## Local inference, recorded 2026-09-12

Ollama 0.33.3, `num_ctx=8192`, models warmed before timing:

| Model | Prefill | Generation |
| --- | --- | --- |
| `qwen2.5:3b-instruct` | About 500-600 tokens/s | 46.6 tokens/s |
| `qwen2.5:7b-instruct-q4_K_M` | About 240-280 tokens/s | 22.9 tokens/s |

The configured default is the pinned 7B model. These throughput samples do not
measure the current tech brief's end-to-end runtime. Use `just bench` to measure
local inference on your own machine.

## Deployment and software search, recorded 2026-09-13

- Deploying unchanged package versions reused an older wheel until explicit package
  reinstallation was added. `just deploy` reinstalls workspace packages and compares
  source files with their deployed copies.
- A Python language filter excluded Python tools implemented in Rust. Search instead
  adds Python as a keyword where appropriate.
- npm's broad search could rank unrelated high-download packages first. npm therefore
  enriches repositories found by GitHub rather than defining the candidate list.
- Hacker News discussions must match every search keyword in their title.
- Observed search latency ranged from about 7 seconds for a JavaScript query to
  about 30 seconds for a Python query. These are samples, not latency guarantees.

## Current verification commands

```bash
just test
just lint
just doctor
uv run pa-core-admin smoke-llm
uv run python evals/run_search.py
```

The first two commands check local code. The remaining commands depend on local
services, credentials, or public endpoints. Report those results separately.
