# Run conditions

These are the rules the official benchmark runs follow. They live in the same repo as the seeders so anyone reproducing the benchmark uses the same conditions we do.

## Tools compared

| Tool | How it runs | Operator skill assumption |
| :---- | :---- | :---- |
| AWS Console | Headed Chrome session, human-supervised, scripted clicks where bot detection allows, manual fallback otherwise | Non-technical |
| AWS CLI | `aws s3 ls` and friends, timed with `time`, captured to logs | Technical |
| CloudSee Drive | Headed Chrome session driven by Selenium against `https://drive.cloudsee.cloud` | Non-technical |

## Fairness rules

1. **Same data.** All three tools run against the exact buckets built by this repo, in account `592920047652`, region `us-east-1`, against the same target files.
2. **Same client.** Same machine, same network, runs grouped within the same hour to control for bandwidth variance.
3. **Same browser baseline.** Latest stable Chrome, default settings, no extensions, fresh profile per run.
4. **Same AWS identity.** AWS Console and AWS CLI runs both authenticate as the `csd-benchmark-readonly` IAM user defined in `infra/iam-readonly-user.json`. No MFA. CSD runs use the internal Webapper-owned demo account `demoadmin@cloudsee.cloud` on `drive-uat.cloudsee.cloud` (UAT). The original spec called for a dedicated trial account on production CSD; we elected to run on UAT with the internal demo account because it had bucket connection and indexing workflows already tested. UAT and production share the same codebase and search backend, so the marketing claim ports cleanly to production once we re-run there. An external reproducer can run the benchmark against their own trial account on production with the documented expectation that account state (cache, prior queries, indexing prewarm) is reset per the cache-state rules below.

5. **Indexing prewarm.** CSD's Fast Buckets and Tag Explorer features require an index over each connected drive. Indexing is a one-time setup cost that varies by bucket size and is *not* part of the search-time measurements. The benchmark measures time-to-result *after* indexing is complete. This matches how a real customer experiences the tool: they connect a bucket, wait for indexing, then search. Indexing duration is documented separately in run logs but not aggregated into time-to-result.
6. **Cache state declared.** Cold-cache and warm-cache variants are captured separately. Cache reset procedure is documented per scenario in the harness repo.
7. **Human-supervised throughout.** Any run where the harness misbehaves is discarded and re-run, not patched in post.
8. **N=10 per scenario per tool.** Report median and p95.
9. **Binary completion flag.** Each run records whether a non-technical user could complete the task without help. This is the qualitative signal we publish alongside the numbers.

## Metrics captured per run

- Time-to-result, from search initiated to file visible in viewport
- Click count or keystroke count
- Number of HTTP requests fired
- Network bytes transferred
- Did the run complete inside a 5-minute soft cap, yes or no
- Cold-cache vs warm-cache variant

## Scenarios

| # | Scenario | Target |
| :---- | :---- | :---- |
| 1 | Find a file on page ~500 of a flat 150k-object bucket | Pre-known filename at object index ~149,500 in `csd-benchmark-flat-150k` |
| 2 | Find a file 6+ folders deep, full path known | `linux/v6.10/arch/arm64/boot/dts/freescale/imx8mq-evk.dts` in `csd-benchmark-oss-mirror` |
| 3 | Find files by partial filename across the whole bucket | Substring `bluetooth`, expecting 200+ matches across the four OSS projects |
| 4 | Find a file modified in a specific date range | Files with `source-mtime` between `2020-01-01` and `2020-12-31` across all four projects |
| 5 | Find files by tag (`domain=audio`) | Tagged subset under `linux/v6.10/sound/` and TensorFlow audio paths |

## Headline metric

Scenario 3 median time-to-result. The Console literally cannot do this search natively. Backup numbers come from Scenarios 1 and 5.

## Receipts

For every official run group we archive: results CSV, run logs, HAR files, screenshots. They land in `results/` in this repo.
