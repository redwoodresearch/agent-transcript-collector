"""Shared S3 configuration and client factory.

Used by the collector, watcher, and read-only S3 browser so bucket, region, and
credential handling is defined once.
"""

import os

import boto3

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


def make_s3_client():
    """Build an S3 client.

    Use AWS SSO through a local profile. Redwood's standard profile name is
    ``rw-eng``; callers can override it with ``CTC_AWS_PROFILE``, ``AWS_PROFILE``,
    or ``AWS_DEFAULT_PROFILE``.
    """
    profile = selected_profile()
    return boto3.Session(profile_name=profile, region_name=S3_REGION).client("s3")
