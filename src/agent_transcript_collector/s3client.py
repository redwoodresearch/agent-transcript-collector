"""Shared S3 configuration and client factory.

Used by the collector, watcher, and read-only S3 browser so bucket, region, and
credential handling is defined once.

Defaults target Redwood's ``rr-agent-transcripts`` bucket on AWS via SSO. The
same code also drives any S3-compatible object store (Google Cloud Storage,
Cloudflare R2, MinIO, ...) by setting ``CTC_S3_ENDPOINT_URL`` plus HMAC-style
``CTC_S3_ACCESS_KEY_ID`` / ``CTC_S3_SECRET_ACCESS_KEY`` credentials.
"""

import os
from typing import Any

import boto3
from botocore.config import Config


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


S3_BUCKET = _env("CTC_S3_BUCKET", "rr-agent-transcripts")
S3_REGION = _env("CTC_S3_REGION", "us-east-1")
# Custom endpoint for non-AWS S3-compatible stores; unset means real AWS S3.
# For Google Cloud Storage use ``https://storage.googleapis.com``.
S3_ENDPOINT_URL = _env("CTC_S3_ENDPOINT_URL")
DEFAULT_AWS_PROFILE = "rw-eng"


def selected_profile() -> str:
    """Resolve the AWS profile using the collector's documented precedence."""
    return (
        os.environ.get("CTC_AWS_PROFILE")
        or os.environ.get("AWS_PROFILE")
        or os.environ.get("AWS_DEFAULT_PROFILE")
        or DEFAULT_AWS_PROFILE
    )


def _hmac_credentials() -> tuple[str, str] | None:
    """Return explicit access-key credentials when configured.

    S3-compatible stores like GCS authenticate with an HMAC key pair rather than
    an AWS SSO profile. When both env vars are present they take precedence over
    profile-based credentials.
    """
    key = _env("CTC_S3_ACCESS_KEY_ID")
    secret = _env("CTC_S3_SECRET_ACCESS_KEY")
    if key and secret:
        return key, secret
    return None


def _client_config() -> Config:
    kwargs: dict[str, Any] = dict(
        connect_timeout=4,
        read_timeout=8,
        max_pool_connections=20,
        retries={"max_attempts": 2, "mode": "standard"},
    )
    # boto3 >= 1.36 adds integrity checksums to uploads by default. Non-AWS
    # S3-compatible endpoints (GCS, R2, some MinIO builds) reject those headers,
    # so only send a checksum when the specific operation requires one.
    if S3_ENDPOINT_URL:
        kwargs["request_checksum_calculation"] = "when_required"
        kwargs["response_checksum_validation"] = "when_required"
    return Config(**kwargs)


def make_s3_client() -> Any:
    """Build an S3 client.

    Two credential modes:

    * **AWS SSO** (default) through a local profile. Redwood's standard profile
      name is ``rw-eng``; override with ``CTC_AWS_PROFILE``, ``AWS_PROFILE``, or
      ``AWS_DEFAULT_PROFILE``.
    * **HMAC keys** for an S3-compatible endpoint, via ``CTC_S3_ACCESS_KEY_ID``
      and ``CTC_S3_SECRET_ACCESS_KEY`` (typically paired with
      ``CTC_S3_ENDPOINT_URL``). When set, these bypass the SSO profile entirely.
    """
    config = _client_config()

    hmac = _hmac_credentials()
    if hmac is not None:
        access_key, secret_key = hmac
        return boto3.client(
            "s3",
            region_name=S3_REGION,
            endpoint_url=S3_ENDPOINT_URL,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=config,
        )

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
        endpoint_url=S3_ENDPOINT_URL,
        config=config,
    )
