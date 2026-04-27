# Frozen Eval Baselines

This directory holds the immutable per-phase eval baselines used by Phase 4
cutover and the C.* retrieval-quality phases.

## Policy

**Never rename. Never overwrite.** Each phase that changes embeddings,
collections, or chunking produces a new named file:

| File | Captured against | Used as comparison reference for |
|---|---|---|
| `omnigraph_code.json` | `omnigraph_code` (nomic-embed-text-v1.5; legacy collection name) | Phase 4 cutover gate when Hub still uses the legacy collection |
| `code_v1_nomic.json` | `code_v1_nomic` (nomic-embed-text-v1.5; canonical name) | Phase 4 cutover gate after rename migration |
| `code_v2_jina.json` | `code_v2_jina` (jina-embeddings-v2-base-code) | Phase C.1 gate |
| `code_v3_jina_symbol.json` | `code_v3_jina_symbol` (jina + symbol chunking) | Phase C.2 gate |
| `code_v4a_jina_symbol_ctx_go.json` | jina + Go ParentEntity context | Phase C.2 final gate |

`omnigraph_code` and `code_v1_nomic` capture the same nomic embeddings.
The split exists because `docker-compose.yml` defaulted to `omnigraph_code`
before the Plan v4 rename; deployments that never migrated continue to
write into `omnigraph_code`. Use whichever file matches the live Hub's
`/stats.qdrant.collection` value as the cutover comparison reference.

Once committed, a baseline is the immutable comparison target for the *next*
phase. Do not regenerate or update in place — produce a new file.

The working capture path `eval/baseline.json` (created by
`scripts/eval/run_baseline.py`) is a scratch capture, not a frozen reference.
Promote a working capture to this directory by `cp` once the gate passes; the
copy in `tests/eval/baselines/` is then immutable.

## Why immutable

The Phase 4 acceptance gate compares `code_v2_jina` against `code_v1_nomic`.
If we silently overwrite `code_v1_nomic.json` later, all future jina A/B
comparisons re-anchor to a moving target and the regression detector loses
its meaning. The same chain applies to C.1 → C.2.

## Adding a new baseline

1. Capture: `python scripts/eval/run_baseline.py --collection <name> --output eval/<name>.json`.
2. Reproducibility: `python scripts/eval/run_baseline.py --collection <name> --check` (must pass Jaccard ≥ 0.95).
3. Run the gate: `python scripts/eval/compare_baselines.py tests/eval/baselines/<prev>.json eval/<name>.json` (must exit 0).
4. Freeze: `cp eval/<name>.json tests/eval/baselines/<name>.json` and commit.

## Decommission

Replace the entire harness when a public labeled benchmark for code RAG
becomes accessible (CoIR, CodeSearchNet) — see `eval/README.md`.
