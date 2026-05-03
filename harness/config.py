"""Configuration for the harness.

Anything that varies per environment lives here. Per-run settings (which
scenario, which tool, cold vs warm, N) are passed via run_matrix.py CLI flags.

Bucket names, region, and target keys are sourced from a copy of the csd-benchmark
config at fetch time; we do not import the seeder repo as a dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# Buckets and target files come from the csd-benchmark seed config.
# Hard-coded here so the harness can run without csd-benchmark on PYTHONPATH.
# Keep these in sync if the seeder changes them.
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

BUCKET_FLAT = "csd-benchmark-flat-150k"
BUCKET_OSS_MIRROR = "csd-benchmark-oss-mirror"

# Production CSD. UAT (drive-uat.cloudsee.cloud) was used during harness
# bring-up but the marketing numbers run against prod because UAT may
# have weaker hardware that would understate CSD's real-world performance.
# The harness is environment-agnostic; flip this URL to re-target.
CSD_BASE_URL = "https://drive.cloudsee.cloud"
AWS_CONSOLE_BASE_URL = "https://console.aws.amazon.com/s3/"

# CSD identifies S3 buckets by a "Drive" display name in the left sidebar.
# The drive name is whatever the operator picks when connecting the bucket
# inside CSD - it does not have to equal the S3 bucket name. This map lets
# the harness translate "Scenario X bucket name" -> "drive name to click in
# the sidebar". Update these strings to match what's actually visible in
# the demo account's drive list.
CSD_DRIVE_NAMES: dict[str, str] = {
    BUCKET_FLAT: "csd-benchmark-flat-150k",
    BUCKET_OSS_MIRROR: "csd-benchmark-oss-mirror",
}

# When set, the CSD runner attaches Selenium to an already-running Chrome
# at this debugger address instead of launching a new browser. The operator
# starts Chrome with --remote-debugging-port=9222 and a custom user-data-dir,
# signs in once, and leaves the window open. The harness then drives that
# session - same Chrome environment as a real user, no Selenium-managed
# Chrome quirks that block CSD's debounced search from firing.
CSD_CHROME_DEBUGGER_ADDRESS = os.environ.get(
    "CSD_BENCHMARK_CHROME_DEBUGGER_ADDRESS", "localhost:9222"
)

# 5-minute soft cap on time-to-result, per spec.
TIMEOUT_SOFT_CAP_SEC = 300

# Target inventory CSV sidecar paths (used by Scenario 4).
INVENTORY_OSS_KEY = "_meta/inventory-oss-mirror.csv"
INVENTORY_FLAT_KEY = "_meta/inventory-flat-150k.csv"


@dataclass(frozen=True)
class IamCreds:
    """Read-only IAM creds used by the AWS Console and CLI runners.

    Per RUN_CONDITIONS.md: dedicated IAM user csd-benchmark-readonly,
    no MFA. Env vars expected for non-interactive runs.
    """

    access_key_id: str
    secret_access_key: str

    @classmethod
    def from_env(cls) -> "IamCreds":
        # Empty strings signal "use ambient AWS config" - the boto3/aws-cli
        # default credential provider chain (env vars, AWS_PROFILE, ~/.aws/credentials,
        # instance metadata) takes over. CliRunner.setup() honors this contract.
        ak = os.environ.get("CSD_BENCHMARK_AWS_ACCESS_KEY_ID", "")
        sk = os.environ.get("CSD_BENCHMARK_AWS_SECRET_ACCESS_KEY", "")
        return cls(ak, sk)


@dataclass(frozen=True)
class CsdCreds:
    """Trial-tier CSD account creds. Production-side login at drive.cloudsee.cloud."""

    email: str
    password: str

    @classmethod
    def from_env(cls) -> "CsdCreds":
        # Email is non-secret (shown in operator prompts as a hint).
        # Password is typed manually by the operator in the live browser
        # window per the locked auth decision; the harness never types it.
        # Empty defaults so the runner can launch without CI env wiring.
        return cls(
            email=os.environ.get("CSD_BENCHMARK_CSD_EMAIL", "demoadmin@cloudsee.cloud"),
            password=os.environ.get("CSD_BENCHMARK_CSD_PASSWORD", ""),
        )
