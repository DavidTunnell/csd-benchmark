"""Seed the csd-benchmark-oss-mirror bucket from four OSS projects.

Streaming, working-tree-free design so seeding works on Windows. The Linux
kernel contains a file at drivers/gpu/drm/nouveau/nvkm/subdev/i2c/aux.c -
"AUX" is a Windows reserved name and cannot be created on NTFS. We avoid
the issue by never writing source files to disk.

For each project in config.yaml/oss_sources:
  1. Clone the repo at the pinned tag with --no-checkout (working tree empty).
  2. Run `git archive HEAD --format=tar` and stream the result through
     Python's tarfile module.
  3. For each file member, upload to S3 under the project's prefix with metadata:
       source-mtime: <ISO 8601 timestamp from the commit at git_ref>
  4. Idempotent: if an object already exists with a matching ETag, skip upload.

After upload, we assert the post-conditions from config.yaml:
  - Scenario 2 target file is present.
  - Scenario 3 substring 'bluetooth' returns at least min_matches keys.
  - Scenario 4 date range yields at least min_matches files (by source-mtime).

Usage:
    python seed_oss_mirror.py [--dry-run] [--only PROJECT] [--limit N] [--skip-checks]

This script is human-supervised. It does not run AWS calls until you give it
real credentials via environment variables. Use --dry-run to validate the
clone + archive walk without touching AWS.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

import boto3
import botocore.exceptions

from common import Config, SeedingError, get_logger, run_cmd, workdir

log = get_logger("seed_oss_mirror")

UPLOAD_WORKERS = 16


def shallow_clone(git_url: str, git_ref: str, dest: Path) -> None:
    """Ensure dest contains a --no-checkout clone of git_url at git_ref.

    The working tree stays empty so we never run into NTFS reserved-name
    issues (CON, PRN, AUX, NUL, COMx, LPTx). All source content is later
    streamed via `git archive`. Idempotent: if dest already has a .git/,
    we re-fetch the ref instead of re-cloning.
    """
    if (dest / ".git").is_dir():
        log.info("repo exists at %s, fetching %s", dest, git_ref)
        run_cmd(["git", "fetch", "--tags", "--depth=1", "origin", git_ref], cwd=dest)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("cloning %s @ %s into %s (no-checkout)", git_url, git_ref, dest)
    run_cmd(
        [
            "git",
            "clone",
            "--depth=1",
            "--branch",
            git_ref,
            "--single-branch",
            "--no-checkout",
            git_url,
            str(dest),
        ]
    )


def commit_iso_timestamp(repo_dir: Path, git_ref: str) -> str:
    """ISO 8601 timestamp of the commit at git_ref, used as source-mtime.

    We resolve git_ref explicitly rather than HEAD because in a fresh
    --no-checkout clone HEAD may point at the resolved commit but FETCH_HEAD
    or the tag ref is the safer reference.
    """
    iso = run_cmd(
        ["git", "log", "-1", "--format=%cI", git_ref],
        cwd=repo_dir,
    )
    if not iso:
        # Fallback: try HEAD
        iso = run_cmd(["git", "log", "-1", "--format=%cI"], cwd=repo_dir)
    if not iso:
        raise SeedingError(f"could not read commit timestamp at {git_ref} in {repo_dir}")
    return iso


def iter_archive_entries(repo_dir: Path, git_ref: str) -> Iterable[tuple[str, bytes]]:
    """Yield (relative_path, file_bytes) for every regular file at git_ref.

    Streams `git archive --format=tar <ref>` and walks the resulting tar via
    Python's tarfile module. Symlinks, directories, and non-regular entries
    are skipped. Path separators in tar entries are always forward slashes,
    which is what we want for S3 keys.
    """
    proc = subprocess.Popen(
        ["git", "archive", "--format=tar", git_ref],
        cwd=str(repo_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|*") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    continue
                yield member.name, fh.read()
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()
        if proc.returncode != 0:
            err_bytes = proc.stderr.read() if proc.stderr else b""
            err_text = err_bytes.decode("utf-8", errors="ignore").strip()
            raise SeedingError(
                f"git archive {git_ref} failed (exit {proc.returncode}): {err_text}"
            )


def existing_etag(s3, bucket: str, key: str) -> str | None:
    try:
        resp = s3.head_object(Bucket=bucket, Key=key)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return resp.get("ETag", "").strip('"') or None


def upload_bytes(
    s3,
    bucket: str,
    key: str,
    body: bytes,
    source_mtime: str,
) -> str:
    """Upload body bytes to s3://bucket/key with source-mtime metadata.

    Returns 'uploaded' or 'skipped' (matching ETag already present).
    """
    local_md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
    if existing_etag(s3, bucket, key) == local_md5:
        return "skipped"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=io.BytesIO(body),
        Metadata={"source-mtime": source_mtime},
    )
    return "uploaded"


def upload_project(
    s3,
    bucket: str,
    prefix: str,
    repo_dir: Path,
    git_ref: str,
    source_mtime: str,
    dry_run: bool,
    limit: int | None,
) -> tuple[int, int, int]:
    """Stream every file at git_ref from repo_dir and upload to s3://bucket/{prefix}{relpath}.

    We materialize all archive entries up front into a list because tarfile's
    streaming reader has to be consumed sequentially in a single thread - we
    can't share a streaming TarFile across our upload workers. Memory cost
    is bounded by total source size (a few hundred MB at most for these
    projects), which is fine on any modern machine.

    Returns (uploaded, skipped, failed).
    """
    log.info("project %s: streaming archive at %s", prefix.rstrip("/"), git_ref)
    entries: list[tuple[str, bytes]] = []
    for relpath, body in iter_archive_entries(repo_dir, git_ref):
        entries.append((relpath, body))
        if limit and len(entries) >= limit:
            break
    log.info("project %s: %d files in archive", prefix.rstrip("/"), len(entries))

    if dry_run:
        log.info("dry-run, sample keys:")
        for relpath, _ in entries[:5]:
            log.info("  %s%s", prefix, relpath)
        return (0, 0, 0)

    uploaded = skipped = failed = 0

    def _task(relpath: str, body: bytes) -> str:
        key = f"{prefix}{relpath}"
        return upload_bytes(s3, bucket, key, body, source_mtime)

    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
        futures = {pool.submit(_task, rel, body): rel for rel, body in entries}
        for i, fut in enumerate(as_completed(futures), start=1):
            rel = futures[fut]
            try:
                status = fut.result()
            except Exception as exc:
                failed += 1
                log.error("upload failed for %s: %s", rel, exc)
                continue
            if status == "uploaded":
                uploaded += 1
            elif status == "skipped":
                skipped += 1
            if i % 1000 == 0:
                log.info(
                    "  progress: %d/%d (uploaded=%d skipped=%d failed=%d)",
                    i,
                    len(entries),
                    uploaded,
                    skipped,
                    failed,
                )

    log.info(
        "project %s done: uploaded=%d skipped=%d failed=%d",
        prefix.rstrip("/"),
        uploaded,
        skipped,
        failed,
    )
    return (uploaded, skipped, failed)


# ----- Post-condition checks -----

def assert_scenario_2(s3, bucket: str, target_key: str) -> None:
    log.info("checking Scenario 2 target file: %s", target_key)
    try:
        s3.head_object(Bucket=bucket, Key=target_key)
    except botocore.exceptions.ClientError as exc:
        raise SeedingError(
            f"Scenario 2 target file missing: s3://{bucket}/{target_key}"
        ) from exc


def assert_scenario_3(s3, bucket: str, substring: str, min_matches: int) -> None:
    log.info("checking Scenario 3: substring '%s' across all keys", substring)
    paginator = s3.get_paginator("list_objects_v2")
    matches = 0
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []) or []:
            if substring in obj["Key"].lower():
                matches += 1
                if matches >= min_matches:
                    log.info("  Scenario 3 OK (>= %d matches)", min_matches)
                    return
    raise SeedingError(
        f"Scenario 3 only found {matches} keys containing '{substring}', "
        f"expected at least {min_matches}"
    )


def assert_scenario_4(
    s3, bucket: str, start_iso: str, end_iso: str, min_matches: int
) -> None:
    log.info(
        "checking Scenario 4: source-mtime in [%s, %s]", start_iso, end_iso
    )
    start = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end = dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    paginator = s3.get_paginator("list_objects_v2")
    matches = 0
    sampled = 0
    sample_cap = max(min_matches * 100, 5000)
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []) or []:
            if sampled >= sample_cap and matches < min_matches:
                pass
            sampled += 1
            try:
                head = s3.head_object(Bucket=bucket, Key=obj["Key"])
            except botocore.exceptions.ClientError:
                continue
            mtime_str = head.get("Metadata", {}).get("source-mtime")
            if not mtime_str:
                continue
            try:
                mtime = dt.datetime.fromisoformat(mtime_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start <= mtime <= end:
                matches += 1
                if matches >= min_matches:
                    log.info("  Scenario 4 OK (>= %d matches)", min_matches)
                    return
    raise SeedingError(
        f"Scenario 4 only found {matches} files with source-mtime in range, "
        f"expected at least {min_matches}"
    )


def filter_projects(sources: list[dict], only: str | None) -> list[dict]:
    if not only:
        return sources
    keep = [s for s in sources if s["name"] == only]
    if not keep:
        raise SeedingError(
            f"--only {only} matched no projects in config (have: "
            f"{[s['name'] for s in sources]})"
        )
    return keep


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Clone and walk archive but do not upload to S3.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Run only the named project (e.g. linux). For incremental work.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of files uploaded per project (for smoke tests).",
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip post-seed assertion checks (use only when seeding partial data).",
    )
    args = parser.parse_args()

    cfg = Config.load()
    bucket = cfg.bucket("oss_mirror")["name"]
    sources = filter_projects(cfg.oss_sources(), args.only)

    log.info(
        "seeding %d project(s) into s3://%s (region=%s, dry_run=%s)",
        len(sources),
        bucket,
        cfg.region,
        args.dry_run,
    )

    s3 = boto3.client("s3", region_name=cfg.region) if not args.dry_run else None

    totals = [0, 0, 0]
    for src in sources:
        repo_dir = workdir("oss", src["name"])
        shallow_clone(src["git_url"], src["git_ref"], repo_dir)
        source_mtime = commit_iso_timestamp(repo_dir, src["git_ref"])
        log.info(
            "project %s pinned at %s, source-mtime=%s",
            src["name"],
            src["git_ref"],
            source_mtime,
        )
        u, s, f = upload_project(
            s3,
            bucket,
            src["prefix"],
            repo_dir,
            src["git_ref"],
            source_mtime,
            dry_run=args.dry_run,
            limit=args.limit,
        )
        totals[0] += u
        totals[1] += s
        totals[2] += f

    log.info(
        "seeding totals: uploaded=%d skipped=%d failed=%d", *totals
    )

    if args.dry_run:
        log.info("dry-run complete, no AWS calls made")
        return 0

    if totals[2] > 0:
        log.error("non-zero failures, refusing to run post-seed checks")
        return 2

    if args.skip_checks:
        log.warning("--skip-checks set, not validating scenarios")
        return 0

    if args.only or args.limit:
        log.info("partial run, skipping scenario assertions")
        return 0

    assert s3 is not None
    target = next(
        (s for s in cfg.oss_sources() if s["name"] == "linux"),
        None,
    )
    if target and "scenario_2_target" in target:
        assert_scenario_2(s3, bucket, target["scenario_2_target"])

    sc3 = cfg.raw.get("scenario_3", {})
    assert_scenario_3(
        s3, bucket, sc3.get("substring", "bluetooth"), int(sc3.get("min_matches", 200))
    )

    sc4 = cfg.raw.get("scenario_4", {})
    assert_scenario_4(
        s3,
        bucket,
        sc4.get("date_range_start"),
        sc4.get("date_range_end"),
        int(sc4.get("min_matches", 50)),
    )

    log.info("all scenario post-conditions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedingError as exc:
        log.error("seeding failed: %s", exc)
        sys.exit(1)
