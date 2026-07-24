"""External object storage via a boto3 client (no wreath equivalent — keep as-is)."""
import boto3

_s3 = boto3.client("s3")


def store_manifest(booking_id: str, payload: bytes) -> str:
    key = f"manifests/{booking_id}.json"
    _s3.put_object(Bucket="tumbleweed-manifests", Key=key, Body=payload)
    return key
