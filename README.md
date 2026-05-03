# csd-benchmark

Reproducible benchmark comparing **CloudSee Drive** with **Fast Buckets** and **Tag Explorer** against the **AWS Console** and the **AWS CLI** on real-scale S3 buckets. Five scenarios, three tools, public seed scripts, public buckets, public results.

This repo holds the seeders that build the benchmark buckets, the infra config, and (in `results/`) the official run output once Phases 6 and 7 of the rollout plan complete. The harnesses live in a sibling repo.

## What gets built

Three S3 buckets in account `592920047652`, region `us-east-1`:

| Bucket | Visibility | Contents | Used by |
| :---- | :---- | :---- | :---- |
| `csd-benchmark-flat-150k` | Public-read | 150,000 synthetic flat objects with a known target at lexical index ~149,500 | Scenario 1 |
| `csd-benchmark-oss-mirror` | Public-read | Linux v6.10, Kubernetes v1.36.0, React v19.2.5, TensorFlow v2.20.0 at real paths, plus `_meta/inventory-*.csv` sidecars and `domain=audio` tags on a documented subset | Scenarios 2, 3, 4, 5 |
| `csd-benchmark-inventory-reports` | Private | Native S3 Inventory daily CSV reports for both source buckets, written by the S3 service. Used as a backup/audit trail for the `_meta/` sidecars that scenarios actually read from. | Internal verification |

Source-mtime is preserved as object metadata using each project's HEAD commit timestamp at the pinned tag, so Scenario 4 (date-range search) has real, defensible values.

## How to reproduce

You need:

- AWS credentials for an account you control with permission to create S3 buckets, IAM users, and CloudWatch alarms. The seeders default to account `592920047652`; change `aws.account_id` in `seed/config.yaml` if you're running against a different account.
- Python 3.10+
- `git`, `aws` CLI, `jq`, `bash`

Steps:

1. Stand up the AWS resources.

   ```bash
   AWS_PROFILE=your-profile bash scripts/bootstrap_aws.sh
   ```

   Creates both buckets, applies public-read policies, creates the read-only IAM user, and arms a 50 GB/hour CloudWatch alarm on each bucket's egress.

2. Install Python deps.

   ```bash
   cd seed
   python -m pip install -r requirements.txt
   ```

3. Seed the OSS mirror bucket. Takes ~30-60 minutes on a typical home connection. Idempotent.

   ```bash
   python seed_oss_mirror.py
   ```

   This runs four shallow clones into `.work/oss/`, walks each tree, uploads every file with `source-mtime` metadata, and asserts post-conditions (Scenario 2 target exists, Scenario 3 substring count >= 200, Scenario 4 date-range count >= 50).

4. Seed the flat 150k bucket.

   ```bash
   python seed_flat_150k.py
   ```

5. Apply Scenario 5 tags.

   ```bash
   python tag_audio_files.py
   ```

6. Generate the inventory CSV sidecars.

   ```bash
   python generate_inventory.py --bucket oss_mirror
   python generate_inventory.py --bucket flat
   ```

After step 6 the buckets are ready for the benchmark harnesses.

## Layout

```
csd-benchmark/
  README.md                 # this file
  RUN_CONDITIONS.md         # fairness rules, published verbatim alongside results
  seed/
    config.yaml             # SOLE source of truth for buckets, OSS refs, target counts, tag rules
    common.py               # shared helpers
    seed_oss_mirror.py      # mirrors four OSS projects into oss-mirror bucket
    seed_flat_150k.py       # synthesizes 150k flat objects
    tag_audio_files.py      # applies domain=audio per Scenario 5 allowlist
    generate_inventory.py   # writes _meta/inventory-{bucket}.csv sidecars
    requirements.txt
  infra/
    bucket-policy.json                  # public-read template (flat + oss buckets)
    inventory-dest-bucket-policy.json   # private inventory bucket policy, allows only S3 service writes
    iam-readonly-user.json              # benchmark IAM user policy
    inventory-config.json               # native S3 Inventory daily report (backup to our sidecar)
  scripts/
    bootstrap_aws.sh        # one-shot AWS setup
  results/                  # benchmark output, populated in P6
```

## Locked decisions

| Decision | Resolution |
| :---- | :---- |
| AWS account | `592920047652`, Webapper sandbox |
| Region | `us-east-1` |
| OSS source pins | Linux `v6.10`, Kubernetes `v1.36.0`, React `v19.2.5`, TensorFlow `v2.20.0` |
| Flat bucket size | Exactly 150,000 objects, target at index 149,500 |
| Scenario 4 methodology | `source-mtime` object metadata + `_meta/inventory-*.csv` sidecar, both reproducible from this repo |
| Scenario 5 audio subset | Path-glob allowlist in `seed/config.yaml`, applied via `tag_audio_files.py` |
| Public-read access | Yes, scripted in `bootstrap_aws.sh`. Bandwidth alarm armed at 50 GB/hour per bucket. |

## Scope of this repo (v1.0)

In: seeders, infra config, run conditions, results archive.
Out: Selenium harness, AWS Console runner, CLI runner. Those live in the harness repo and depend on the buckets this repo builds.

## Risks

- **Bandwidth from public-read.** The CloudWatch alarms in `bootstrap_aws.sh` are the first line of defense. Add a CloudFront cap if egress ever becomes material.
- **OSS pin drift.** Versions are baked into `config.yaml`. If a project deletes a tag from origin, seeding breaks loudly rather than silently.
- **Console pagination quirks.** Scenario 1 depends on the target landing at the configured lexical index. The flat seeder sorts deterministically and inserts the target at the configured position so the index is reproducible run-to-run.

## Reproduction is the point

Anyone can take this repo, point it at their own AWS account, and rebuild the same buckets in a few hours. That is the only credible way to publish a vendor-led benchmark and have anyone believe the numbers. If you find a bias or a bug in how the buckets get built, open an issue and we will fix it.
