"""Content-addressed store for raw response bodies.

Raw HTML is kept exactly as received so that any evidence quote can be checked
against the original document. Documents are gzip-compressed and named by the
SHA-256 of the *uncompressed* bytes, so identical responses are stored once
and a document's name doubles as its integrity check.

Two backends (``rawstore.backend``):

* ``filesystem``: ``<root>/<aa>/<bb>/<sha256>.gz``, written to a temporary
  file, fsynced and renamed, so a stored name always holds a complete body.
* ``s3`` (any S3-compatible service: AWS S3, MinIO, R2; approved 2026-09-29):
  ``<prefix>sha256/<aa>/<bb>/<sha256>`` in ``s3_bucket``. Credentials come from
  the standard AWS chain (environment, profile, instance role), never from
  this configuration.

``put`` returns only once the object is durable, and callers record the hash
in the database after that (production LLD sections 19 and 28), so the
database never references a missing object. The database's record of each
document is the ``artifact`` table (spec section 22).

``ARTIFACT_VERSION`` names the stored format: version 1 is the response body
exactly as delivered after HTTP content-coding is removed, gzip-compressed,
keyed by the SHA-256 of the uncompressed bytes.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import tempfile
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .config import RawStoreSettings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

_SHA256 = re.compile(r"[0-9a-f]{64}")
ARTIFACT_VERSION = "1"


class RawStoreError(Exception):
    pass


class RawStoreBackend(Protocol):
    def put(self, data: bytes) -> str: ...
    def get(self, digest: str) -> bytes: ...
    def exists(self, digest: str) -> bool: ...
    def prune(self, keep: Iterable[str], older_than_days: int) -> list[str]: ...


def make_rawstore(settings: RawStoreSettings) -> RawStoreBackend:
    if settings.backend == "s3":
        return S3RawStore(settings.s3_bucket, settings.s3_prefix,
                          endpoint_url=settings.s3_endpoint_url or None,
                          region=settings.s3_region or None)  # fmt: skip
    return RawStore(settings.root)


def _check(digest: str) -> str:
    if not _SHA256.fullmatch(digest):
        raise RawStoreError(f"invalid digest: {digest!r}")
    return digest


def _verified(data: bytes, digest: str) -> bytes:
    if hashlib.sha256(data).hexdigest() != digest:
        raise RawStoreError(f"integrity check failed: {digest}")
    return data


class RawStore:
    """Filesystem backend."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, data: bytes) -> str:
        """Store ``data`` and return its SHA-256 hex digest. Idempotent."""
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file and rename, so a crash never leaves a
        # truncated file under a valid content address.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(gzip.compress(data, mtime=0))
                fh.flush()
                os.fsync(fh.fileno())  # durable before the database refers to it
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            # A concurrent put of the same bytes won the rename (Windows refuses
            # to replace a file another writer just created). Same name means
            # same content, so the stored copy is already correct.
            if path.exists():
                return digest
            raise
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return digest

    def get(self, digest: str) -> bytes:
        """Return the stored bytes, verifying they still match ``digest``."""
        path = self._path(digest)
        try:
            data = gzip.decompress(path.read_bytes())
        except FileNotFoundError as exc:
            raise RawStoreError(f"not found: {digest}") from exc
        return _verified(data, digest)

    def exists(self, digest: str) -> bool:
        return self._path(digest).exists()

    def prune(self, keep: Iterable[str], older_than_days: int) -> list[str]:
        """Delete documents not in ``keep`` whose files are older than the cut-off;
        return the deleted digests.

        ``keep`` is the set of digests something still refers to (e.g. pages
        with evidence); those are never deleted regardless of age.
        """
        keep_set = set(keep)
        cutoff = time.time() - older_than_days * 86_400
        removed: list[str] = []
        for path in self.root.glob("*/*/*.gz"):
            digest = path.name.removesuffix(".gz")
            if digest in keep_set or path.stat().st_mtime >= cutoff:
                continue
            path.unlink(missing_ok=True)
            removed.append(digest)
        return removed

    def _path(self, digest: str) -> Path:
        _check(digest)
        return self.root / digest[:2] / digest[2:4] / f"{digest}.gz"


class S3RawStore:
    """S3-compatible backend. ``client`` is injectable for tests."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: S3Client | Any | None = None,
        endpoint_url: str | None = None,
        region: str | None = None,
    ) -> None:
        if not bucket:
            raise RawStoreError("rawstore.s3_bucket is required for the s3 backend")
        if client is None:
            import boto3

            client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def key(self, digest: str) -> str:
        _check(digest)
        return f"{self.prefix}sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        if self.exists(digest):
            return digest
        # One PUT: S3 stores the whole object or nothing, and acknowledges it
        # only once it is durable.
        self.client.put_object(
            Bucket=self.bucket, Key=self.key(digest), Body=gzip.compress(data, mtime=0),
            ContentType="application/gzip", Metadata={"sha256": digest},
        )  # fmt: skip
        return digest

    def get(self, digest: str) -> bytes:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=self.key(digest))["Body"].read()
        except Exception as exc:
            if _not_found(exc):
                raise RawStoreError(f"not found: {digest}") from exc
            raise
        return _verified(gzip.decompress(body), digest)

    def exists(self, digest: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key(digest))
        except Exception as exc:
            if _not_found(exc):
                return False
            raise
        return True

    def prune(self, keep: Iterable[str], older_than_days: int) -> list[str]:
        keep_set = set(keep)
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        doomed: list[str] = []
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=f"{self.prefix}sha256/"
        )
        for page in pages:
            for obj in page.get("Contents", []):
                digest = obj["Key"].rsplit("/", 1)[-1]
                if (
                    _SHA256.fullmatch(digest)
                    and digest not in keep_set
                    and (obj["LastModified"] < cutoff)
                ):
                    doomed.append(obj["Key"])
        for start in range(0, len(doomed), 1000):  # the API's batch limit
            batch: list[Any] = [{"Key": k} for k in doomed[start : start + 1000]]
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})
        return [key.rsplit("/", 1)[-1] for key in doomed]


def _not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code in ("404", "NoSuchKey", "NotFound")
