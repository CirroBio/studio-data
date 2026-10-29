"""Where a collection is published: the S3 bucket, or a local folder in the same layout that an app's
dev data URL can serve."""
import base64
import hashlib
import itertools
import mimetypes
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import boto3
from botocore.exceptions import ClientError

from .items import PART_SIZE, FileEntry, describe_file

NO_CACHE = "no-cache"
IMMUTABLE = "public, max-age=31536000, immutable"
MISSING = ("404", "NoSuchKey", "NotFound")
REFUSED = ("PreconditionFailed", "ConditionalRequestConflict")


class Conflict(Exception):
    """A conditional write was refused: the key exists, or changed since it was read."""


@dataclass(frozen=True)
class Stat:
    size: int
    sha256: str | None
    crc32: str | None


def matches(stat: Stat | None, f: FileEntry) -> bool:
    if stat is None or stat.size != f.size:
        return False
    return stat.crc32 == f.crc32 if f.crc32 else stat.sha256 == f.sha256


def content_type(key: str) -> str:
    kind, encoding = mimetypes.guess_type(key)
    # A .gz is served as the bytes stored, as application/gzip, never with Content-Encoding.
    if encoding:
        return "application/gzip" if encoding == "gzip" else "application/octet-stream"
    return kind or "application/octet-stream"


class Storage(Protocol):
    def url(self, key: str) -> str: ...
    def read(self, key: str) -> tuple[bytes, str] | None: ...  # body and ETag
    def write(self, key: str, body: bytes, *, cache_control: str = IMMUTABLE, if_match: str | None = None, create_only: bool = False) -> None: ...
    def upload(self, key: str, path: Path, f: FileEntry) -> None: ...  # never overwrites
    def stat(self, key: str) -> Stat | None: ...
    def keys(self, prefix: str) -> Iterator[tuple[str, datetime]]: ...
    def delete(self, keys: list[str]) -> None: ...


def bucket_url(bucket: str, region: str, public_url: str = "") -> str:
    """The address apps read the bucket at: the configured one (a CDN), or the bucket's own."""
    return public_url.rstrip("/") or f"https://{bucket}.s3.{region}.amazonaws.com"


class S3Storage:
    def __init__(self, bucket: str, region: str, public_url: str = "", client=None):
        self.bucket = bucket
        self.s3 = client or boto3.client("s3", region_name=region)
        self.public_url = bucket_url(bucket, region, public_url)

    def url(self, key: str) -> str:
        return f"{self.public_url}/{key}"

    def _conditional(self, call, **args):
        try:
            return call(Bucket=self.bucket, **args)
        except ClientError as e:
            if e.response["Error"]["Code"] in REFUSED:
                raise Conflict(args["Key"]) from None
            raise

    def read(self, key):
        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in MISSING:
                return None
            raise
        return r["Body"].read(), r["ETag"]

    def write(self, key, body, *, cache_control=IMMUTABLE, if_match=None, create_only=False):
        args = {"Key": key, "Body": body, "ContentType": content_type(key), "CacheControl": cache_control,
                "ChecksumAlgorithm": "SHA256", "ChecksumSHA256": base64.b64encode(hashlib.sha256(body).digest()).decode()}
        if if_match:
            args["IfMatch"] = if_match
        if create_only:
            args["IfNoneMatch"] = "*"
        self._conditional(self.s3.put_object, **args)

    def upload(self, key, path, f):
        common = {"Key": key, "ContentType": content_type(key), "CacheControl": IMMUTABLE}
        if f.crc32 is None:
            with path.open("rb") as body:
                self._conditional(self.s3.put_object, Body=body, ChecksumAlgorithm="SHA256", ChecksumSHA256=f.sha256, IfNoneMatch="*", **common)
            return
        upload_id = self.s3.create_multipart_upload(Bucket=self.bucket, ChecksumAlgorithm="CRC32", ChecksumType="FULL_OBJECT", **common)["UploadId"]
        try:
            parts = []
            with path.open("rb") as body:
                for number in itertools.count(1):
                    if not (chunk := body.read(PART_SIZE)):
                        break
                    r = self.s3.upload_part(Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=chunk, ChecksumAlgorithm="CRC32")
                    parts.append({"PartNumber": number, "ETag": r["ETag"], "ChecksumCRC32": r["ChecksumCRC32"]})
            self._conditional(self.s3.complete_multipart_upload, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts},
                              ChecksumCRC32=f.crc32, ChecksumType="FULL_OBJECT", IfNoneMatch="*")
        except BaseException:
            self.s3.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
            raise

    def stat(self, key):
        try:
            r = self.s3.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as e:
            if e.response["Error"]["Code"] in MISSING:
                return None
            raise
        return Stat(r["ContentLength"], r.get("ChecksumSHA256"), r.get("ChecksumCRC32"))

    def keys(self, prefix):
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                yield o["Key"], o["LastModified"]

    def delete(self, keys):
        for start in range(0, len(keys), 1000):
            r = self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys[start:start + 1000]], "Quiet": True})
            if r.get("Errors"):
                raise RuntimeError(f"S3 refused to delete {len(r['Errors'])} objects, such as {r['Errors'][0]}")


class LocalStorage:
    """The bucket's layout in a folder. Its conditional writes check, then write, so two publishers
    running at once on the same folder are not kept apart as they are on S3."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def url(self, key):
        return (self.root / key).resolve().as_uri()

    def read(self, key):
        p = self.root / key
        if not p.is_file():
            return None
        body = p.read_bytes()
        return body, hashlib.sha256(body).hexdigest()

    def write(self, key, body, *, cache_control=IMMUTABLE, if_match=None, create_only=False):
        current = self.read(key)
        if (create_only and current is not None) or (if_match is not None and (current is None or current[1] != if_match)):
            raise Conflict(key)
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        partial = p.with_name(p.name + ".partial")
        partial.write_bytes(body)
        os.replace(partial, p)

    def upload(self, key, path, f):
        p = self.root / key
        if p.exists():
            raise Conflict(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, p)

    def stat(self, key):
        p = self.root / key
        if not p.is_file():
            return None
        d = describe_file(p, key)
        return Stat(d.size, d.sha256, d.crc32)

    def keys(self, prefix):
        base = self.root / prefix
        if not base.is_dir():
            return
        for p in sorted(base.rglob("*")):
            if p.is_file():
                yield p.relative_to(self.root).as_posix(), datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)

    def delete(self, keys):
        for k in keys:
            (self.root / k).unlink()
