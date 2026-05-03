"""Generate the inventory CSV sidecar for a benchmark bucket.

The sidecar maps every S3 key to its source-mtime metadata. Scenario 4 needs
this because the AWS Console can't filter by user metadata - operators run the
search against this CSV instead. Native S3 Inventory is also enabled in
infra/inventory-config.json as a backup, but it's eventually-consistent and
takes ~24h for the first delivery, so we ship our own deterministic sidecar
that's available the moment seeding finishes.

Output is written to s3://<bucket>/<inventory.prefix><filename>, where the
prefix and filenames come from config.yaml.

Usage:
    python generate_inventory.py [--bucket flat|oss_mirror] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import io
import sys

import boto3
import botocore.exceptions

from common import Config, SeedingError, get_logger

log = get_logger("generate_inventory")


def head_with_retry(s3, bucket: str, key: str, attempts: int = 3) -> dict | None:
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            return s3.head_object(Bucket=bucket, Key=key)
        except botocore.exceptions.ClientError as exc:
            last_exc = exc
            code = exc.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
        except Exception as exc:
            last_exc = exc
    if last_exc:
        log.warning("head_object exhausted retries for %s: %s", key, last_exc)
    return None


def build_inventory(s3, bucket: str, skip_prefix: str) -> str:
    """Walk the bucket and return CSV text: key, size_bytes, etag, source_mtime."""
    paginator = s3.get_paginator("list_objects_v2")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["key", "size_bytes", "etag", "source_mtime"])
    counted = 0
    skipped = 0
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            if skip_prefix and key.startswith(skip_prefix):
                skipped += 1
                continue
            head = head_with_retry(s3, bucket, key)
            mtime = ""
            if head is not None:
                mtime = head.get("Metadata", {}).get("source-mtime", "")
            writer.writerow(
                [
                    key,
                    obj.get("Size", 0),
                    obj.get("ETag", "").strip('"'),
                    mtime,
                ]
            )
            counted += 1
            if counted % 5000 == 0:
                log.info("  inventory progress: %d rows", counted)
    log.info("inventory complete: %d rows (skipped %d under %s)", counted, skipped, skip_prefix)
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", choices=["flat", "oss_mirror"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = Config.load()
    bk = cfg.bucket(args.bucket)
    bucket = bk["name"]
    inv = cfg.raw["inventory"]
    prefix = inv["prefix"]
    fname = inv["flat_filename"] if args.bucket == "flat" else inv["oss_filename"]
    target_key = f"{prefix}{fname}"

    s3 = boto3.client("s3", region_name=cfg.region)
    log.info("building inventory for s3://%s, output=%s", bucket, target_key)
    csv_body = build_inventory(s3, bucket, skip_prefix=prefix)

    if args.dry_run:
        log.info("dry-run, not uploading. sample:")
        sample_lines = csv_body.splitlines()[:5]
        for line in sample_lines:
            log.info("  %s", line)
        return 0

    s3.put_object(
        Bucket=bucket,
        Key=target_key,
        Body=csv_body.encode("utf-8"),
        ContentType="text/csv",
        Metadata={"description": "csd-benchmark inventory sidecar"},
    )
    log.info("uploaded inventory: s3://%s/%s", bucket, target_key)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedingError as exc:
        log.error("inventory generation failed: %s", exc)
        sys.exit(1)
