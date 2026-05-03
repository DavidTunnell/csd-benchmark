"""Apply Scenario 5 tag (default: domain=audio) to objects in csd-benchmark-oss-mirror.

Reads config.yaml/scenario_5/path_patterns and applies the configured tag to
every key matching any pattern. Patterns use fnmatch-style globs against the
full S3 key. Existing tags on the object are preserved by reading first,
merging the new tag in, and writing the merged tag set back. This is what S3's
PutObjectTagging API requires - it replaces the entire tag set on the object.

Idempotent: re-running on already-tagged objects is a no-op.

Usage:
    python tag_audio_files.py [--dry-run]
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import botocore.exceptions

from common import Config, SeedingError, get_logger

log = get_logger("tag_audio_files")

WORKERS = 16


def matches_any(key: str, patterns: list[str]) -> bool:
    """fnmatch supports * and ?. We use it on the full key (no leading slash)."""
    return any(fnmatch.fnmatch(key, p) for p in patterns)


def list_all_keys(s3, bucket: str):
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []) or []:
            yield obj["Key"]


def get_existing_tags(s3, bucket: str, key: str) -> dict[str, str]:
    try:
        resp = s3.get_object_tagging(Bucket=bucket, Key=key)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NoSuchTagSet"):
            return {}
        raise
    return {t["Key"]: t["Value"] for t in resp.get("TagSet", []) or []}


def apply_tag(s3, bucket: str, key: str, tag_key: str, tag_value: str) -> str:
    existing = get_existing_tags(s3, bucket, key)
    if existing.get(tag_key) == tag_value:
        return "skipped"
    existing[tag_key] = tag_value
    s3.put_object_tagging(
        Bucket=bucket,
        Key=key,
        Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in existing.items()]},
    )
    return "tagged"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = Config.load()
    bucket = cfg.bucket("oss_mirror")["name"]
    sc5 = cfg.raw["scenario_5"]
    tag_key = sc5["tag_key"]
    tag_value = sc5["tag_value"]
    patterns = list(sc5["path_patterns"])
    min_matches = int(sc5.get("min_matches", 0))

    log.info("scanning s3://%s for audio-tag candidates", bucket)
    if args.dry_run:
        log.info("dry-run mode, no PutObjectTagging calls")
        s3 = boto3.client("s3", region_name=cfg.region)
        sample = []
        scanned = 0
        for key in list_all_keys(s3, bucket):
            scanned += 1
            if matches_any(key, patterns):
                sample.append(key)
        log.info("scanned %d keys, %d would be tagged", scanned, len(sample))
        for k in sample[:20]:
            log.info("  match: %s", k)
        if len(sample) > 20:
            log.info("  ... %d more", len(sample) - 20)
        if len(sample) < min_matches:
            raise SeedingError(
                f"only {len(sample)} matches, expected at least {min_matches}"
            )
        return 0

    s3 = boto3.client("s3", region_name=cfg.region)

    keys_to_tag: list[str] = []
    for key in list_all_keys(s3, bucket):
        if matches_any(key, patterns):
            keys_to_tag.append(key)

    log.info("found %d keys to tag with %s=%s", len(keys_to_tag), tag_key, tag_value)
    if len(keys_to_tag) < min_matches:
        raise SeedingError(
            f"only {len(keys_to_tag)} matches, expected at least {min_matches}. "
            "Check Scenario 5 patterns in config.yaml."
        )

    tagged = skipped = failed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(apply_tag, s3, bucket, k, tag_key, tag_value): k
            for k in keys_to_tag
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            k = futures[fut]
            try:
                status = fut.result()
            except Exception as exc:
                failed += 1
                log.error("tag failed for %s: %s", k, exc)
                continue
            if status == "tagged":
                tagged += 1
            else:
                skipped += 1
            if i % 500 == 0:
                log.info(
                    "  progress: %d/%d (tagged=%d skipped=%d failed=%d)",
                    i,
                    len(keys_to_tag),
                    tagged,
                    skipped,
                    failed,
                )

    log.info("done: tagged=%d skipped=%d failed=%d", tagged, skipped, failed)
    return 2 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedingError as exc:
        log.error("tagging failed: %s", exc)
        sys.exit(1)
