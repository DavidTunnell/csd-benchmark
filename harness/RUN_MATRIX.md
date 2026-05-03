# Run matrix

Per spec: each scenario runs N=10 per tool, cold and warm cache captured separately, all under human supervision.

```
       │ scenario 1 │ scenario 2 │ scenario 3 │ scenario 4 │ scenario 5 │
───────┼────────────┼────────────┼────────────┼────────────┼────────────┤
 csd   │ 10c + 10w  │ 10c + 10w  │ 10c + 10w  │ 10c + 10w  │ 10c + 10w  │
console│ 10c + 10w  │ 10c + 10w  │ 10c + 10w  │ 10c + 10w  │  N/A * 1   │
 cli   │ 10c + 10w  │ 10c + 10w  │ 10c + 10w  │ 10c + 10w  │ 10c + 10w  │
```

\* Console cannot query S3 object tags. The runner records this as `completed_within_cap=false, result_correct=false, notes='Console has no tag query UI'` rather than running.

Total runs in a full group: 14 cells × 20 runs = 280 minus 20 (Console scenario 5) = **260 runs**.

## Per-scenario notes

### Scenario 1: deep flat-bucket target

- Bucket: `csd-benchmark-flat-150k`
- Target: `needle-quarterly-report-2024-Q4.pdf` at lexical index ~149,500
- CSD path: Fast Buckets search by full filename
- Console path: paginate next/next/next OR substring filter at bucket root (will only match the current page; expected pain)
- CLI path: `aws s3api head-object`. Will be near-instant; the point is to anchor the comparison

### Scenario 2: 6-folder-deep target with full path

- Bucket: `csd-benchmark-oss-mirror`
- Target: `linux/v6.10/arch/arm64/boot/dts/freescale/imx8mq-evk.dts`
- CSD path: paste path into search, or click through folders
- Console path: click through folders one at a time
- CLI path: `aws s3api head-object`

### Scenario 3: substring across whole bucket

- Bucket: `csd-benchmark-oss-mirror`
- Substring: `test`. Real count: 32,763 across all four projects
- CSD path: Fast Buckets substring search at bucket root
- Console path: not natively possible across prefixes; only filters within current "folder"
- CLI path: `aws s3 ls --recursive | findstr test | measure -line`

This is the headline metric per spec.

### Scenario 4: source-mtime in 2020

- Bucket: `csd-benchmark-oss-mirror`
- Range: `2020-01-01` to `2020-12-31`, evaluated against `source-mtime` user metadata
- CSD path: Tag Explorer / metadata filter on source-mtime
- Console path: not natively possible; operator opens `_meta/inventory-oss-mirror.csv` in Excel as workaround
- CLI path: `aws s3 cp _meta/inventory-oss-mirror.csv -` then awk filter

The fact that the Console path is "open the sidecar in Excel" *is* the result for Scenario 4.

### Scenario 5: tag query

- Bucket: `csd-benchmark-oss-mirror`
- Tag: `domain=audio`. 3,060 keys tagged
- CSD path: Tag Explorer
- Console path: structurally impossible, recorded as N/A
- CLI path: boto3 script (the canonical "you have to write code" workaround)

This is where Tag Explorer earns its name.

## Cache state

- **Cold:** browser profile cleared (cookies, storage, cache), page reloaded.
- **Warm:** no reset between runs.

CLI has no meaningful client cache; cold and warm should produce identical numbers and we record both for symmetry.

## Run grouping

Per spec, "runs grouped within the same hour to control for bandwidth variance." The orchestrator runs the full matrix in one process per group. If a run group is interrupted, restart with a fresh `run_id` rather than splicing.

## Discard policy

Per spec: any run where the harness misbehaves is discarded and re-run, not patched in post. The CSV stores all rows; aggregation drops `notes ~= 'discarded'` rows.

## Output schema

See `harness/results.py`. 16 columns, one row per run, append-only.
