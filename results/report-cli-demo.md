# csd-benchmark report

Generated 2026-05-03 20:14 UTC

Source files:
- `results\run-20260503T201316Z.csv`

Run conditions: see [csd-benchmark/RUN_CONDITIONS.md](https://bitbucket.org/cloudsee-drive/csd-benchmark/src/main/RUN_CONDITIONS.md).

## Headline metric

Scenario 3 median time-to-result, cold cache. Spec:
> Find files by partial filename across the whole bucket. Substring `test`. ~32,763 matches.

_No scenario-3 cold-cache runs in the supplied data._

## Per-scenario detail

### Scenario 1

| Tool | Cache | N | Median t (s) | p95 t (s) | Median clicks | Median HTTP | Within cap | Non-tech ok | Correct |
| :---- | :---- | ----: | ----: | ----: | ----: | ----: | :---- | :---- | :---- |
| cli | cold | 3 | 0.98 | 1.05 | 0 | n/a | 3/3 (100%) | 0/3 (0%) | 3/3 (100%) |
| cli | warm | 3 | 0.94 | 0.95 | 0 | n/a | 3/3 (100%) | 0/3 (0%) | 3/3 (100%) |

### Scenario 2

| Tool | Cache | N | Median t (s) | p95 t (s) | Median clicks | Median HTTP | Within cap | Non-tech ok | Correct |
| :---- | :---- | ----: | ----: | ----: | ----: | ----: | :---- | :---- | :---- |
| cli | cold | 3 | 0.94 | 1.01 | 0 | n/a | 3/3 (100%) | 0/3 (0%) | 3/3 (100%) |
| cli | warm | 3 | 0.93 | 0.96 | 0 | n/a | 3/3 (100%) | 0/3 (0%) | 3/3 (100%) |
