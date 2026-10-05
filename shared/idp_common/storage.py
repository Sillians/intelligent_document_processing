import json
import hashlib
from typing import Any
import boto3
from botocore.exceptions import ClientError

from shared.idp_common.config import Settings


def _normalize_s3_endpoint(raw_endpoint: str, secure: bool) -> str:
    if raw_endpoint.startswith("http://") or raw_endpoint.startswith("https://"):
        return raw_endpoint
    scheme = "https" if secure else "http"
    return f"{scheme}://{raw_endpoint}"


def get_s3_client(settings: Settings):
    return boto3.client(
        "s3",
        endpoint_url=_normalize_s3_endpoint(settings.s3_endpoint, settings.s3_secure),
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        use_ssl=settings.s3_secure,
    )


def ensure_bucket(settings: Settings, bucket: str) -> None:
    s3 = get_s3_client(settings)
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except ClientError:
        pass

    try:
        s3.create_bucket(Bucket=bucket)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            return
        raise


def upload_bytes(settings: Settings, bucket: str, key: str, payload: bytes, content_type: str) -> str:
    s3 = get_s3_client(settings)
    s3.put_object(Bucket=bucket, Key=key, Body=payload, ContentType=content_type)
    return f"s3://{bucket}/{key}"


def download_bytes(settings: Settings, bucket: str, key: str) -> bytes:
    s3 = get_s3_client(settings)
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def object_exists(settings: Settings, bucket: str, key: str) -> bool:
    try:
        get_s3_client(settings).head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def upload_json(settings: Settings, bucket: str, key: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    return upload_bytes(settings, bucket, key, body, "application/json")


def download_json(settings: Settings, bucket: str, key: str) -> dict[str, Any]:
    raw = download_bytes(settings, bucket, key)
    return json.loads(raw.decode("utf-8"))


def verify_artifact(settings: Settings, bucket: str, key: str, sha256: str, size_bytes: int) -> None:
    response = get_s3_client(settings).get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        if response["ContentLength"] != size_bytes:
            raise ValueError("Raw artifact size mismatch")
        digest = hashlib.sha256()
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != sha256:
            raise ValueError("Raw artifact checksum mismatch")
    finally:
        body.close()


def download_verified_bytes(settings: Settings, bucket: str, key: str, sha256: str, size_bytes: int) -> bytes:
    if size_bytes > settings.ingestion_max_upload_size_mb * 1024 * 1024:
        raise ValueError("Source artifact exceeds the upload limit")
    response = get_s3_client(settings).get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        if response["ContentLength"] != size_bytes:
            raise ValueError("Source artifact size mismatch")
        data = body.read(size_bytes + 1)
        if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("Source artifact checksum mismatch")
        return data
    finally:
        body.close()
