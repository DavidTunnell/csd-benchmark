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

CSD_BASE_URL = "https://drive.cloudsee.cloud"
AWS_CONSOLE_BASE_URL = "https://console.aws.amazon.com/s3/"

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
        ak = os.environ["CSD_BENCHMARK_AWS_ACCESS_KEY_ID"]
        sk = os.environ["CSD_BENCHMARK_AWS_SECRET_ACCESS_KEY"]
        return cls(ak, sk)


@dataclass(frozen=True)
class CsdCreds:
    """Trial-tier CSD account creds. Production-side login at drive.cloudsee.cloud."""

    email: str
    password: str

    @classmethod
    def from_env(cls) -> "CsdCreds":
        return cls(
            email=os.environ["CSD_BENCHMARK_CSD_EMAIL"],
            password=os.environ["CSD_BENCHMARK_CSD_PASSWORD"],
        )
