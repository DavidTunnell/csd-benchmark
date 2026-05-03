# csd-benchmark report

Generated 2026-05-03 20:47 UTC

Source files:
- `results\run-20260503T203237Z.csv`

Run conditions: see [csd-benchmark/RUN_CONDITIONS.md](https://bitbucket.org/cloudsee-drive/csd-benchmark/src/main/RUN_CONDITIONS.md).

## Headline metric

Scenario 3 median time-to-result, cold cache. Spec:
> Find files by partial filename across the whole bucket. Substring `test`. ~32,763 matches.

| Tool | N | Median (s) | p95 (s) |
| :---- | ----: | ----: | ----: |
| cli | 1 | 43.7 | 43.7 |

## Per-scenario detail

### Scenario 1

| Tool | Cache | N | Median t (s) | p95 t (s) | Median clicks | Median HTTP | Within cap | Non-tech ok | Correct |
| :---- | :---- | ----: | ----: | ----: | ----: | ----: | :---- | :---- | :---- |
| cli | cold | 1 | 0.91 | 0.91 | 0 | n/a | 1/1 (100%) | 0/1 (0%) | 1/1 (100%) |
| cli | warm | 1 | 0.88 | 0.88 | 0 | n/a | 1/1 (100%) | 0/1 (0%) | 1/1 (100%) |

### Scenario 2

| Tool | Cache | N | Median t (s) | p95 t (s) | Median clicks | Median HTTP | Within cap | Non-tech ok | Correct |
| :---- | :---- | ----: | ----: | ----: | ----: | ----: | :---- | :---- | :---- |
| cli | cold | 1 | 0.92 | 0.92 | 0 | n/a | 1/1 (100%) | 0/1 (0%) | 1/1 (100%) |
| cli | warm | 1 | 0.92 | 0.92 | 0 | n/a | 1/1 (100%) | 0/1 (0%) | 1/1 (100%) |

### Scenario 3

| Tool | Cache | N | Median t (s) | p95 t (s) | Median clicks | Median HTTP | Within cap | Non-tech ok | Correct |
| :---- | :---- | ----: | ----: | ----: | ----: | ----: | :---- | :---- | :---- |
| cli | cold | 1 | 43.7 | 43.7 | 0 | n/a | 1/1 (100%) | 0/1 (0%) | 1/1 (100%) |
| cli | warm | 1 | 44.0 | 44.0 | 0 | n/a | 1/1 (100%) | 0/1 (0%) | 1/1 (100%) |

### Scenario 4

| Tool | Cache | N | Median t (s) | p95 t (s) | Median clicks | Median HTTP | Within cap | Non-tech ok | Correct |
| :---- | :---- | ----: | ----: | ----: | ----: | ----: | :---- | :---- | :---- |
| cli | cold | 1 | 3.99 | 3.99 | 0 | n/a | 1/1 (100%) | 0/1 (0%) | 1/1 (100%) |
| cli | warm | 1 | 5.28 | 5.28 | 0 | n/a | 1/1 (100%) | 0/1 (0%) | 1/1 (100%) |

### Scenario 5

| Tool | Cache | N | Median t (s) | p95 t (s) | Median clicks | Median HTTP | Within cap | Non-tech ok | Correct |
| :---- | :---- | ----: | ----: | ----: | ----: | ----: | :---- | :---- | :---- |
| cli | cold | 1 | 379 | 379 | 0 | n/a | 0/1 (0%) | 0/1 (0%) | 1/1 (100%) |
| cli | warm | 1 | 372 | 372 | 0 | n/a | 0/1 (0%) | 0/1 (0%) | 1/1 (100%) |
