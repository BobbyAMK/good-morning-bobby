# Measured on this machine

Anything in the plan marked "estimate" should be replaced by a number here.
Machine: Apple M1 Pro, 8 cores (6P/2E), 16 GB unified memory, macOS 26.5.2.

## LLM throughput (Ollama 0.33.3, num_ctx=8192, measured 2026-09-12)

Models warmed before timing, so these are inference rates and not model-load time.

| Model | Prefill | Generation |
|---|---|---|
| `qwen2.5:3b-instruct` | ~500-600 tok/s | **46.6 tok/s** |
| `qwen2.5:7b-instruct-q4_K_M` | ~240-280 tok/s | **22.9 tok/s** |

Raw points: 3B 575 in -> 1.6s, 3353 in -> 6.6s; 7B 575 in -> 3.1s, 3353 in -> 14.2s.
Generation: 3B 844 out in 18.1s; 7B 900 out in 39.3s.

### What this implies
- An 8000-token prompt costs **~30s of pure prefill** on the 7B. Prefill dominates.
- A single 21K-token brief call would be ~80s of prefill AND would not fit in
  num_ctx=8192 at all. **Map-reduce is not an optimization here, it is the only shape
  that runs.**
- Realistic morning-brief budget: MAP 2 batches on the 3B (~12s) + REDUCE on the 7B
  (~3000 tok prefill = 12s, ~1200 tok output = 52s) = **~80 seconds total**.
  Better than the plan's 2-4 minute estimate.

## Verified platform facts

- SQLite 3.53.4, FTS5 present. `tokenize='unicode61 remove_diacritics 2'` makes
  `tai chinh` match `tài chính` -- tested, works. Vietnamese full-text search is free.
- `httpx2` 2.12.0 installs as the module **`httpx2`**, not `httpx`. (The design sketch
  had this wrong.)
- keyring 25.7.0 resolves to the macOS Keychain backend.
- uv 0.11.17 (Homebrew, so `uv self update` refuses -- use `brew upgrade uv`) builds the
  workspace fine with `uv_build>=0.11,<0.13`.
- Ollama models present: `qwen2.5:{1.5b,3b,7b}-instruct`, `qwen2.5:7b-instruct-q4_K_M`,
  `nomic-embed-text:latest` (768 dims, 2K context -- the 2K ceiling is why project 6
  should use `bge-m3` instead).

## Still unmeasured
- `qwen3.5:*` tags: not confirmed to exist. Run `ollama pull` before pinning them.
- Real end-to-end morning brief wall time (needs the Gmail/Calendar connectors).
