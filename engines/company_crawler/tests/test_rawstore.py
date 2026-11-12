import gzip
import hashlib
import io
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gtm_crawler.config import ConfigError, RawStoreSettings
from gtm_crawler.rawstore import RawStore, RawStoreError, S3RawStore, make_rawstore


def test_put_get_roundtrip_is_content_addressed(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    data = b"<html>raw evidence</html>"
    digest = store.put(data)
    assert digest == hashlib.sha256(data).hexdigest()
    path = tmp_path / digest[:2] / digest[2:4] / f"{digest}.gz"
    assert gzip.decompress(path.read_bytes()) == data
    assert store.get(digest) == data
    assert store.put(data) == digest  # idempotent
    assert list(tmp_path.rglob("*.tmp")) == []


def test_tampered_or_missing_documents_are_detected(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    digest = store.put(b"original")
    path = tmp_path / digest[:2] / digest[2:4] / f"{digest}.gz"
    path.write_bytes(gzip.compress(b"altered"))
    with pytest.raises(RawStoreError, match="integrity"):
        store.get(digest)
    with pytest.raises(RawStoreError, match="not found"):
        store.get("0" * 64)


@pytest.mark.parametrize("digest", ["../../etc/passwd", "ABC", "g" * 64, "a" * 63])
def test_invalid_digests_rejected(tmp_path: Path, digest: str) -> None:
    with pytest.raises(RawStoreError, match="invalid digest"):
        RawStore(tmp_path).get(digest)


def test_prune_keeps_referenced_and_recent(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    old_kept, old_unused, recent = store.put(b"a"), store.put(b"b"), store.put(b"c")
    long_ago = time.time() - 100 * 86_400
    for digest in (old_kept, old_unused):
        os.utime(tmp_path / digest[:2] / digest[2:4] / f"{digest}.gz", (long_ago, long_ago))

    assert store.prune(keep={old_kept}, older_than_days=90) == [old_unused]
    assert store.exists(old_kept) and store.exists(recent)
    assert not store.exists(old_unused)


def test_concurrent_puts_of_identical_content(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = RawStore(tmp_path)
    data = b"<html>same bytes from two URLs</html>" * 1000
    with ThreadPoolExecutor(8) as pool:
        digests = set(pool.map(lambda _: store.put(data), range(64)))
    assert digests == {hashlib.sha256(data).hexdigest()}
    assert store.get(digests.pop()) == data
    assert list(tmp_path.rglob("*.tmp")) == []


class FakeS3:
    """The slice of the S3 API the raw store uses, in memory."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}

    @staticmethod
    def _missing(key: str) -> Exception:
        exc = Exception(f"missing {key}")
        exc.response = {"Error": {"Code": "404"}}  # type: ignore[attr-defined]
        return exc

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_: Any) -> None:  # noqa: N803
        self.objects[Key] = (Body, datetime.now(UTC))

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise self._missing(Key)
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise self._missing(Key)
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def get_paginator(self, name: str) -> Any:
        store = self

        class Paginator:
            def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:  # noqa: N803
                return [{"Contents": [{"Key": k, "LastModified": t}
                                      for k, (_, t) in store.objects.items()
                                      if k.startswith(Prefix)]}]  # fmt: skip

        return Paginator()

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> None:  # noqa: N803
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)


def test_s3_store_layout_roundtrip_and_prune() -> None:
    s3 = FakeS3()
    store = S3RawStore("raw", "gtm/", client=s3)
    data = b"<html>raw evidence</html>"
    digest = store.put(data)
    assert list(s3.objects) == [f"gtm/sha256/{digest[:2]}/{digest[2:4]}/{digest}"]
    assert store.get(digest) == data and store.exists(digest)
    assert store.put(data) == digest and len(s3.objects) == 1

    with pytest.raises(RawStoreError, match="not found"):
        store.get("0" * 64)
    key = store.key(digest)
    s3.objects[key] = (gzip.compress(b"altered"), s3.objects[key][1])
    with pytest.raises(RawStoreError, match="integrity"):
        store.get(digest)

    old_kept, old_unused, recent = store.put(b"a"), store.put(b"b"), store.put(b"c")
    long_ago = datetime.now(UTC) - timedelta(days=100)
    for d in (old_kept, old_unused):
        s3.objects[store.key(d)] = (s3.objects[store.key(d)][0], long_ago)
    assert store.prune(keep={old_kept}, older_than_days=90) == [old_unused]
    assert store.exists(old_kept) and store.exists(recent) and not store.exists(old_unused)


def test_backend_is_chosen_by_configuration(tmp_path: Path) -> None:
    assert isinstance(make_rawstore(RawStoreSettings(root=str(tmp_path))), RawStore)
    with pytest.raises(ConfigError, match="s3_bucket"):
        RawStoreSettings(backend="s3")
    with pytest.raises(ConfigError, match="backend"):
        RawStoreSettings(backend="ftp")


@pytest.mark.skipif(not os.environ.get("GTM_TEST_S3_ENDPOINT"),
                    reason="set GTM_TEST_S3_ENDPOINT (e.g. the compose s3 service)")  # fmt: skip
def test_s3_store_against_a_real_endpoint() -> None:
    import boto3

    endpoint = os.environ["GTM_TEST_S3_ENDPOINT"]
    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    if "gtm-test" not in {b["Name"] for b in client.list_buckets().get("Buckets", [])}:
        client.create_bucket(Bucket="gtm-test")
    settings = RawStoreSettings(backend="s3", s3_bucket="gtm-test", s3_endpoint_url=endpoint,
                                s3_region="us-east-1")  # fmt: skip
    store = make_rawstore(settings)
    data = os.urandom(64)
    digest = store.put(data)
    assert store.get(digest) == data and store.exists(digest)
