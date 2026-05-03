"""Seed the csd-benchmark-flat-150k bucket with 150,000 synthetic flat objects.

The point of this bucket is Scenario 1: a non-technical user has to find a
specific file at lexical index ~149,500 in a flat namespace. The Console can't
sort more than 999 objects without prefix narrowing and has no jump-to-page,
so it forces the 'next, next, next' pain.

Generation rules:
  - Every key sits at the bucket root, no prefixes.
  - Filenames look plausible (reports, backups, logs, exports, invoices...).
  - Each object body is small (~1KB) random-but-deterministic padding so
    re-runs produce the same bytes and idempotency works.
  - The Scenario 1 target file (config: target_filename) is placed at
    config: target_index. We sort all generated keys lexically, then insert
    the target at the configured index so its position is reproducible.
  - source-mtime metadata is set to a fixed seed date so Scenario 4 can
    optionally rely on this bucket too if we ever extend the spec.

Usage:
    python seed_flat_150k.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import botocore.exceptions

from common import Config, SeedingError, get_logger

log = get_logger("seed_flat_150k")

UPLOAD_WORKERS = 32

# Fixed seed for reproducibility. Anyone re-running gets the exact same key set.
GENERATION_SEED = 42

# Pools of name fragments. Cartesian product is more than enough for 150k uniques.
CATEGORIES = [
    "report",
    "backup",
    "log",
    "export",
    "invoice",
    "snapshot",
    "audit",
    "metrics",
    "transcript",
    "ledger",
    "manifest",
    "archive",
    "extract",
    "summary",
    "rollup",
]

PROJECTS = [
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
]

EXTENSIONS = [
    "pdf",
    "csv",
    "json",
    "txt",
    "xlsx",
    "tar.gz",
    "zip",
    "log",
]

# Fixed source-mtime for all flat objects. Mid-2024 keeps it adjacent to the
# OSS-mirror bucket era without overlapping the Scenario 4 range.
FLAT_MTIME = "2024-06-15T12:00:00Z"


def deterministic_padding(key: str, size_bytes: int) -> bytes:
    """Return size_bytes of bytes derived from key. Same key, same bytes, every time."""
    out = bytearray()
    counter = 0
    while len(out) < size_bytes:
        chunk = hashlib.sha256(f"{key}:{counter}".encode("utf-8")).digest()
        out.extend(chunk)
        counter += 1
    return bytes(out[:size_bytes])


def generate_keys(target_count: int, target_filename: str, target_index: int) -> list[str]:
    """Return a list of exactly target_count keys, with target_filename at target_index.

    Generation strategy:
      - Generate target_count - 1 deterministic synthetic keys.
      - Sort lexically.
      - Insert target_filename at target_index.
    """
    if target_index >= target_count:
        raise SeedingError(
            f"target_index {target_index} >= target_count {target_count}"
        )

    keys: list[str] = []
    # We deterministically pick (category, project, extension, n) tuples by stepping
    # through a 4D grid in a fixed order. Combined with a small numeric suffix this
    # gives us > 150k unique names without needing randomness.
    n_per_combo = (
        (target_count // (len(CATEGORIES) * len(PROJECTS) * len(EXTENSIONS))) + 2
    )
    for cat in CATEGORIES:
        for proj in PROJECTS:
            for ext in EXTENSIONS:
                for n in range(n_per_combo):
                    fname = f"{cat}-{proj}-{n:05d}.{ext}"
                    keys.append(fname)
                    if len(keys) >= target_count - 1:
                        break
                if len(keys) >= target_count - 1:
                    break
            if len(keys) >= target_count - 1:
                break
        if len(keys) >= target_count - 1:
            break

    keys = sorted(set(keys))
    if len(keys) < target_count - 1:
        raise SeedingError(
            f"key generation produced only {len(keys)} uniques, need {target_count - 1}"
        )
    keys = keys[: target_count - 1]
    keys.insert(target_index, target_filename)
    return keys


def existing_etag(s3, bucket: str, key: str) -> str | None:
    try:
        resp = s3.head_object(Bucket=bucket, Key=key)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return resp.get("ETag", "").strip('"') or None


def upload_one(s3, bucket: str, key: str, size_bytes: int) -> str:
    body = deterministic_padding(key, size_bytes)
    local_md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
    if existing_etag(s3, bucket, key) == local_md5:
        return "skipped"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=io.BytesIO(body),
        Metadata={"source-mtime": FLAT_MTIME},
    )
    return "uploaded"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of objects uploaded (for smoke tests).",
    )
    args = parser.parse_args()

    cfg = Config.load()
    bk = cfg.bucket("flat")
    bucket = bk["name"]
    target_count = int(bk["object_count"])
    target_index = int(bk["target_index"])
    target_name = bk["target_filename"]
    size_bytes = int(bk["object_size_bytes"])

    log.info(
        "generating %d keys with target '%s' at index %d",
        target_count,
        target_name,
        target_index,
    )
    keys = generate_keys(target_count, target_name, target_index)
    if keys[target_index] != target_name:
        raise SeedingError(
            f"key generation drift: expected '{target_name}' at index {target_index}, "
            f"got '{keys[target_index]}'"
        )
    log.info("first key: %s", keys[0])
    log.info("last key:  %s", keys[-1])
    log.info("target lands at index %d: %s", target_index, keys[target_index])

    if args.limit:
        keys = keys[: args.limit]
        log.info("--limit applied, will upload %d keys", len(keys))

    if args.dry_run:
        log.info("dry-run, skipping S3 calls")
        return 0

    s3 = boto3.client("s3", region_name=cfg.region)
    log.info("uploading %d objects to s3://%s", len(keys), bucket)

    uploaded = skipped = failed = 0
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
        futures = {
            pool.submit(upload_one, s3, bucket, key, size_bytes): key for key in keys
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            key = futures[fut]
            try:
                status = fut.result()
            except Exception as exc:
                failed += 1
                log.error("upload failed for %s: %s", key, exc)
                continue
            if status == "uploaded":
                uploaded += 1
            else:
                skipped += 1
            if i % 5000 == 0:
                log.info(
                    "  progress: %d/%d (uploaded=%d skipped=%d failed=%d)",
                    i,
                    len(keys),
                    uploaded,
                    skipped,
                    failed,
                )

    log.info(
        "flat seed done: uploaded=%d skipped=%d failed=%d", uploaded, skipped, failed
    )
    if failed:
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedingError as exc:
        log.error("seeding failed: %s", exc)
        sys.exit(1)
