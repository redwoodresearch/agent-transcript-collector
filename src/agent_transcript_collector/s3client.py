"""Shared S3 configuration and client factory.

Used by the collector, watcher, and read-only S3 browser so bucket, region, and
credential handling is defined once.
"""

import os
from typing import Any

import boto3
from botocore.config import Config

S3_BUCKET = "rr-agent-transcripts"
S3_REGION = "us-east-1"
DEFAULT_AWS_PROFILE = "rw-eng"


def selected_profile() -> str:
    """Resolve the AWS profile using the collector's documented precedence."""
    return (
        os.environ.get("CTC_AWS_PROFILE")
        or os.environ.get("AWS_PROFILE")
        or os.environ.get("AWS_DEFAULT_PROFILE")
        or DEFAULT_AWS_PROFILE
    )


def make_s3_client() -> Any:
    """Build an S3 client.

    Use AWS SSO through a local profile. Redwood's standard profile name is
    ``rw-eng``; callers can override it with ``CTC_AWS_PROFILE``, ``AWS_PROFILE``,
    or ``AWS_DEFAULT_PROFILE``.
    """
    profile = selected_profile()
    session = boto3.Session(profile_name=profile, region_name=S3_REGION)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError(
            f"AWS credentials not found. Run: aws sso login --profile {profile}"
        )
    # Resolve refreshable credentials once before fan-out. Otherwise an expired
    # SSO token can make every parallel HEAD request retry credential refresh
    # independently, leaving the UI apparently stuck for minutes.
    credentials.get_frozen_credentials()
    return session.client(
        "s3",
        config=Config(
            connect_timeout=4,
            read_timeout=8,
            max_pool_connections=20,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )
