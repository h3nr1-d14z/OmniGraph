# Phase 0.5 — Baseline Eval Harness

This directory captures retrieval baselines so Phase 4 (embedder swap) and
Phase 4.5 (RRF) can be measured against a frozen reference instead of vibes.

## Self-labeling acknowledgment

The 12 queries in `queries.json` are hand-curated by the developer for the
fixture corpus at `tests/fixtures/`. This is **self-labeled relevance** — it
reflects the developer's expectation of what each query should match in the
current (buggy) corpus. Confirmation bias is mitigated by:

- Quotas: ≥4 queries each in 3 categories (code-symbolic, nl-semantic,
  dependency-graph). NL-semantic queries deliberately avoid naming the
  target entity.
- Frozen reference: once `baseline.json` is captured, Phase 4/4.5
  acceptance compares **deltas** (improvement vs regression) rather than
  absolute correctness. A self-labeled baseline still surfaces *changes*
  in retrieval behavior.
- Transparent expected_paths: the `expected_paths_substring` field is
  advisory and auditable.

When a public labeled benchmark for code RAG becomes accessible (CoIR,
CodeSearchNet), retire this self-labeled set in favor of it.

## Files

- `queries.json` — 12 query specs (≥4 per category × 3 categories).
- `baseline.json` — top-10 captured against `code_v1_nomic` corpus.
  **Frozen** during Phase 4 / Phase 4.5.
- `jina_v2.json` — top-10 captured against `code_v2_jina` corpus
  (Phase 4 acceptance).
- `rrf.json` — top-10 after RRF replacement (Phase 4.5 acceptance).

## Running the harness

```bash
.venv/bin/python scripts/eval/run_baseline.py --output eval/baseline.json
```

Defaults to `--exact` for deterministic ANN ordering. Re-run to verify
top-10 Jaccard ≥0.95 reproducibility before freezing.

## Acceptance gates (downstream phases)

- Phase 0.5: two runs on unchanged corpus produce top-10 Jaccard ≥0.95.
- Phase 4: ≥80% of queries show ≥1 improvement OR identical top-3 set vs
  `baseline.json` AND no query loses >2 known-good results from baseline
  top-10.
- Phase 4.5: ≥80% of queries show ≥1 improvement OR identical top-3 set
  vs `jina_v2.json`.
