Blocking lint/type baselines for the `make ruff` / `make mypy` gates (run by `.github/workflows/ci.yml` and `make check`).

Baseline policy:
- Ruff keeps its total gate, holding R13's 2,117 below the R12 seed's 2,128. By owner decision on 12 Nov, mypy is gated per (file, code) only, seeded from R12 `0e1543e0`; missing pairs have a zero limit and reductions print a ratchet-down hint.
- The shared comparison is `scripts/reshape/count_baseline.py`. `import_cycles.json` is checked by `scripts/reshape/import_cycles.py`.
- The baselines can only shrink: commit the lower number in the same change that removed the findings.

Regeneration commands (after fixing or deleting code):

```bash
python -m scripts.reshape.compare_ruff_baseline --write-baseline
python -m scripts.reshape.compare_ruff_baseline --write-file-code-baseline
python -m scripts.reshape.compare_structure_baseline --write-baseline
```
