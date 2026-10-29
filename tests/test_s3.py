import json

import boto3
import pytest
from moto import mock_aws
from moto.server import ThreadedMotoServer

from conftest import quiet
from studio_data import bucket, items as items_module, storage as storage_module
from studio_data.publish import publish
from studio_data.settings import Collection
from studio_data.storage import Conflict, S3Storage

BUCKET = "studio-data-test"
REGION = "us-east-1"
EXPRESSION = Collection("expression")


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


def test_publish_stores_checksums_types_and_cache_headers(s3, items):
    a = items("GSE1", {"data/counts.tsv.gz": b"\x1f\x8bcounts", "data/samplesheet.tsv": b"sheet"})
    rid = publish(S3Storage(BUCKET, REGION, client=s3), EXPRESSION, [a], [], "first", log=quiet)
    key = f"collections/expression/items/GSE1/{a.hash}/data/counts.tsv.gz"
    head = s3.head_object(Bucket=BUCKET, Key=key, ChecksumMode="ENABLED")
    assert head["ChecksumSHA256"] == a.files[0].sha256
    assert (head["ContentType"], head.get("ContentEncoding"), head["CacheControl"]) == ("application/gzip", None, storage_module.IMMUTABLE)
    pointer = s3.get_object(Bucket=BUCKET, Key="collections/expression/catalog.json")
    assert pointer["CacheControl"] == "no-cache"
    assert json.loads(pointer["Body"].read())["release"] == rid


def test_written_content_is_never_overwritten(s3, items):
    store = S3Storage(BUCKET, REGION, client=s3)
    a = items("GSE1", {"x": b"1"})
    store.upload("collections/expression/k", a.folder / "x", a.files[0])
    with pytest.raises(Conflict):
        store.upload("collections/expression/k", a.folder / "x", a.files[0])
    with pytest.raises(Conflict):
        store.write("collections/expression/k", b"other", create_only=True)


def test_a_large_file_goes_up_in_parts_and_is_checked_whole(s3, items, monkeypatch):
    part = 5 * 1024 * 1024  # S3's smallest part
    monkeypatch.setattr(items_module, "PART_SIZE", part)
    monkeypatch.setattr(storage_module, "PART_SIZE", part)
    big = items("big", {"store.zip": bytes(range(256)) * (part * 2 // 256 + 7)}, entry="store.zip")
    assert big.files[0].crc32
    store = S3Storage(BUCKET, REGION, client=s3)
    publish(store, EXPRESSION, [big], [], "big", log=quiet)
    stat = store.stat(f"collections/expression/items/big/{big.hash}/store.zip")
    assert storage_module.matches(stat, big.files[0])


def test_setup_keeps_other_rules_and_then_has_nothing_to_do(s3):
    s3.put_bucket_cors(Bucket=BUCKET, CORSConfiguration={"CORSRules": [{"ID": "theirs", "AllowedMethods": ["PUT"], "AllowedOrigins": ["https://x"]}]})
    assert len(bucket.setup(s3, BUCKET, apply=True, log=quiet)) == 4
    assert {r["ID"] for r in s3.get_bucket_cors(Bucket=BUCKET)["CORSRules"]} == {"theirs", "studio-data"}
    assert bucket.setup(s3, BUCKET, apply=True, log=quiet) == []


def test_check_reads_the_collection_over_http(items):
    server = ThreadedMotoServer(port=0)
    server.start()
    try:
        host, port = server.get_host_and_port()
        url = f"http://{host}:{port}"
        s3 = boto3.client("s3", region_name=REGION, endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test")
        s3.create_bucket(Bucket=BUCKET)
        bucket.setup(s3, BUCKET, apply=True, log=quiet)
        a = items("GSE1", {"data/counts.tsv.gz": b"\x1f\x8bcounts"})
        publish(S3Storage(BUCKET, REGION, client=s3), EXPRESSION, [a], [], "first", log=quiet)
        assert bucket.check(f"{url}/{BUCKET}", "expression", log=quiet) == []
        s3.delete_object(Bucket=BUCKET, Key=f"collections/expression/items/GSE1/{a.hash}/data/counts.tsv.gz")
        assert any("counts.tsv.gz: HTTP 404" in p for p in bucket.check(f"{url}/{BUCKET}", "expression", log=quiet))
    finally:
        server.stop()
