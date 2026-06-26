"""Shared S3 configuration and client factory.

Used by both the collector (upload, ``app.py``) and the downloader
(``download.py``) so bucket/region/credential handling is defined once.
"""

import os

import boto3

S3_BUCKET = os.environ.get("CTC_S3_BUCKET", "rr-agent-transcripts")
S3_REGION = os.environ.get("CTC_S3_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.environ.get("CTC_AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("CTC_AWS_SECRET_ACCESS_KEY", "")


def make_s3_client():
    """Build an S3 client.

    Use the explicit CTC_AWS_* credentials when both are provided; otherwise
    fall back to boto3's default credential chain (standard AWS env vars,
    shared config/credentials files, SSO, instance/container roles). Passing
    empty strings explicitly would override that chain, so we omit them.
    """
    kwargs = {"region_name": S3_REGION}
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
    return boto3.client("s3", **kwargs)
