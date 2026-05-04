# csd-benchmark report

Generated 2026-05-04 00:12 UTC

Source files:
- `results\run-20260503T235918Z.csv`

Run conditions: see [csd-benchmark/RUN_CONDITIONS.md](https://bitbucket.org/cloudsee-drive/csd-benchmark/src/main/RUN_CONDITIONS.md).

## Headline metric

Scenario 3 median time-to-result, cold cache. Spec:
> Find files by partial filename across the whole bucket. Substring `test`. ~32,763 matches.

| Tool | N | Median (s) | p95 (s) |
| :---- | ----: | ----: | ----: |
| csd | 10 | 5.74 | 6.01 |

## Per-scenario detail

### Scenario 3

| Tool | Cache | N | Median t (s) | p95 t (s) | Median clicks | Median HTTP | Within cap | Non-tech ok | Correct |
| :---- | :---- | ----: | ----: | ----: | ----: | ----: | :---- | :---- | :---- |
| csd | cold | 10 | 5.74 | 6.01 | 1.00 | n/a | 10/10 (100%) | 10/10 (100%) | 10/10 (100%) |
| csd | warm | 10 | 5.71 | 5.95 | 1.00 | n/a | 10/10 (100%) | 10/10 (100%) | 10/10 (100%) |
