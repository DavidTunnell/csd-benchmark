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
4. **Same AWS identity.** AWS Console and AWS CLI runs both authenticate as the `csd-benchmark-readonly` IAM user defined in `infra/iam-readonly-user.json`. No MFA. CSD runs use the internal Webapper-owned demo account `demoadmin@cloudsee.cloud` on **production** (`drive.cloudsee.cloud`). The original spec called for a dedicated trial account on production CSD; we elected to use the internal demo account because it had bucket connection and indexing workflows already tested. UAT (`drive-uat.cloudsee.cloud`) was used during harness bring-up but the marketing numbers run against prod because UAT may have weaker hardware that would understate CSD's real-world performance. An external reproducer can run the benchmark against their own trial account on production with the documented expectation that account state (cache, prior queries, indexing prewarm) is reset per the cache-state rules below.

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
| 3 | Find files by partial filename across the whole bucket | Substring `test` in `csd-benchmark-oss-mirror`. CLI grep on full key matches 32,763; CSD's filename-only match yields 11,457. Both are valid for their tools and reported alongside the time. |
| 4 | Find a file modified in a specific date range | CLI filters the inventory CSV's `source_mtime` column by `2024-01-01..2024-12-31` (Linux v6.10 commit dates: ~85k matches). CSD filters S3 `LastModified` via Advanced Search > Filters by `2025-01-01..2026-12-31` (the seeding window: all ~155k objects). The two tools answer "find files in a date range" against their respective data models; documented per-tool in the runner notes. |
| 5 | Find files by tag (`domain=audio`) | Tagged subset of audio files under `linux/v6.10/sound/` and TensorFlow audio paths. Seeded count: 3,060. CSD uses Advanced Search > Tag Explorer; CLI requires inline boto3 with parallel `get_object_tagging` calls (canonical "you have to write code" workaround per spec). |

## Headline metric

Scenario 3 median time-to-result. The Console literally cannot do this search natively. Backup numbers come from Scenarios 1 and 5.

## Per-scenario data-source notes

A few scenarios surface a difference between what each tool can natively measure. We don't paper over these; we document them and report each tool's answer in its own data model.

- **Scenario 3 ("test" substring).** CSD's basic search matches the filename portion of each S3 key only, returning 11,457. The CLI's `aws s3 ls --recursive | grep test` matches anywhere in the full key including directory names, returning 32,763. Both numbers are correct for what the user asked their tool to do.

- **Scenario 4 (date range).** CSD's "Date Updated" filter reads each object's S3 `LastModified` timestamp. The CLI runner reads the `source_mtime` column from the inventory CSV (which preserves the original file's mtime from the OSS project being mirrored). Our seeded objects were uploaded to S3 in 2026, so a 2024 range hits zero in CSD even though the inventory CSV has 2024 source mtimes. We use a 2025-2026 range for CSD (the seeding window) and the spec's 2024 range for CLI. The headline UX claim — *can a non-technical user filter by date in a few seconds?* — does not depend on the specific range chosen.

- **Scenario 5 (tag search).** CSD's Tag Explorer is a native UI that lists every tag key in the bucket and lets a non-technical user pick a value. The CLI has no native equivalent: `aws s3api get-object-tagging` is per-object, so listing all 155k objects' tags would take 30+ minutes serially. The CLI runner uses inline boto3 with a 64-thread `ThreadPoolExecutor` doing parallel `get_object_tagging` calls. The spec calls this the canonical "you have to write code" workaround.

## Receipts

For every official run group we archive: results CSV, run logs, HAR files, screenshots. They land in `results/` in this repo.
