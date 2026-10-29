import io
import json
import urllib.parse

import pytest

from conftest import CREDIT, make_item, quiet
from studio_data import publish as publish_module
from studio_data.gc import gc
from studio_data.items import ItemError, read_item
from studio_data.publish import PublishError, publish, read_current, rollback
from studio_data.settings import Collection
from studio_data.storage import LocalStorage

EXPRESSION = Collection("expression")


def index(storage, name="expression"):
    return read_current(storage, name).index


def resolve(storage, name, path):
    """The bucket key an index path names, read against the index's own location."""
    current = read_current(storage, name)
    base = f"https://bucket/collections/{name}/releases/{current.release}/index.json"
    return urllib.parse.urljoin(base, path).removeprefix("https://bucket/")


def test_first_publish_writes_the_pointer_the_index_and_the_content(local, items):
    a = items("GSE1", {"data/counts.tsv.gz": b"counts", "data/samplesheet.tsv": b"sheet"}, folder="Human/Eye", fields={"samples": 6})
    rid = publish(local, EXPRESSION, [a], [], "first", log=quiet)
    pointer = json.loads(local.read("collections/expression/catalog.json")[0])
    assert pointer == {"release": rid, "index": f"releases/{rid}/index.json"}
    [entry] = index(local)["items"]
    assert entry == {"id": "GSE1", "label": "Series GSE1", "credit": CREDIT, "folder": "Human/Eye", "fields": {"samples": 6},
                     "path": f"../../items/GSE1/{a.hash}/"}
    folder = resolve(local, "expression", entry["path"])
    assert local.read(folder + "data/counts.tsv.gz")[0] == b"counts"
    assert local.read(folder + "studio-data.json") is not None
    record = read_current(local, "expression").record
    assert (record["base"], record["message"], record["added"], record["items"]) == (None, "first", ["GSE1"], {"GSE1": a.hash})


def test_publishing_the_same_items_again_changes_nothing(local, items):
    a = items("GSE1", {"x.tsv": b"1"})
    first = publish(local, EXPRESSION, [a], [], "first", log=quiet)
    assert publish(local, EXPRESSION, [a], [], "again", log=quiet) is None
    assert read_current(local, "expression").release == first


def test_a_changed_item_gets_a_new_folder_and_the_old_one_stays(local, items, tmp_path):
    old = items("GSE1", {"x.tsv": b"1"})
    publish(local, EXPRESSION, [old], [], "first", log=quiet)
    new = read_item(make_item(tmp_path / "v2", "GSE1", {"x.tsv": b"2"}))
    publish(local, EXPRESSION, [new], [], "second", log=quiet)
    assert new.hash != old.hash
    assert index(local)["items"][0]["path"] == f"../../items/GSE1/{new.hash}/"
    assert local.read(f"collections/expression/items/GSE1/{old.hash}/x.tsv")[0] == b"1"
    assert read_current(local, "expression").record["changed"] == ["GSE1"]


def test_an_item_missing_locally_is_not_removed(local, items):
    publish(local, EXPRESSION, [items("GSE1", {"x": b"1"}), items("GSE2", {"x": b"2"})], [], "first", log=quiet)
    publish(local, EXPRESSION, [items("GSE3", {"x": b"3"})], [], "add one", log=quiet)
    assert [e["id"] for e in index(local)["items"]] == ["GSE1", "GSE2", "GSE3"]


def test_removals_are_named_and_must_exist(local, items):
    publish(local, EXPRESSION, [items("GSE1", {"x": b"1"}), items("GSE2", {"x": b"2"})], [], "first", log=quiet)
    with pytest.raises(PublishError, match="nothing to remove"):
        publish(local, EXPRESSION, [], ["GSE9"], "typo", log=quiet)
    publish(local, EXPRESSION, [], ["GSE1"], "remove", log=quiet)
    assert [e["id"] for e in index(local)["items"]] == ["GSE2"]
    assert read_current(local, "expression").record["removed"] == ["GSE1"]


def test_many_removals_need_the_flag(local, items):
    ids = [f"GSE{n}" for n in range(publish_module.REMOVALS_WITHOUT_FLAG)]
    publish(local, EXPRESSION, [items(i, {"x": i.encode()}) for i in ids], [], "first", log=quiet)
    with pytest.raises(PublishError, match="allow_removals"):
        publish(local, EXPRESSION, [], ids, "remove all", log=quiet)
    publish(local, EXPRESSION, [], ids, "remove all", allow_removals=True, log=quiet)
    assert index(local)["items"] == []


def test_a_new_label_publishes_without_uploading(local, items, tmp_path):
    a = items("GSE1", {"x": b"1"})
    publish(local, EXPRESSION, [a], [], "first", log=quiet)
    relabelled = read_item(make_item(tmp_path / "v2", "GSE1", {"x": b"1"}, label="Better name"))
    publish(local, EXPRESSION, [relabelled], [], "rename", log=quiet)
    assert index(local)["items"][0]["label"] == "Better name"
    assert [k for k, _ in local.keys("collections/expression/items/")] == [
        f"collections/expression/items/GSE1/{a.hash}/studio-data.json", f"collections/expression/items/GSE1/{a.hash}/x"]


def test_a_folder_entry_ends_in_a_slash_and_a_file_entry_does_not(local, items):
    zarr = items("mif", {"slide.ome.zarr/zarr.json": b"{}", "slide.ome.zarr/0/c/0": b"chunk"}, entry="slide.ome.zarr")
    tiff = items("he", {"slide.ome.tif": b"tiff"}, entry="slide.ome.tif")
    publish(local, EXPRESSION, [zarr, tiff], [], "slides", log=quiet)
    paths = {e["id"]: e["path"] for e in index(local)["items"]}
    assert paths == {"mif": f"../../items/mif/{zarr.hash}/slide.ome.zarr/", "he": f"../../items/he/{tiff.hash}/slide.ome.tif"}


def test_reference_files_are_listed_apart_from_items(local, items):
    genes = items("genes-hsapiens", {"hsapiens.tsv.gz": b"genes"}, kind="reference", entry="hsapiens.tsv.gz")
    publish(local, EXPRESSION, [genes, items("GSE1", {"x": b"1"})], [], "first", log=quiet)
    assert index(local)["reference"] == {"genes-hsapiens": f"../../reference/genes-hsapiens/{genes.hash}/hsapiens.tsv.gz"}
    assert [e["id"] for e in index(local)["items"]] == ["GSE1"]


def test_a_publish_racing_another_is_applied_to_the_other_release(local, items):
    first = publish(local, EXPRESSION, [items("GSE1", {"x": b"1"})], [], "first", log=quiet)
    rival = items("GSE2", {"x": b"2"})

    class Racing(LocalStorage):
        raced = False

        def write(self, key, body, **kw):
            if key.endswith("catalog.json") and not Racing.raced:
                Racing.raced = True
                publish(LocalStorage(self.root), EXPRESSION, [rival], [], "rival", log=quiet)
            super().write(key, body, **kw)

    log = []
    mine = publish(Racing(local.root), EXPRESSION, [items("GSE3", {"x": b"3"})], [], "mine", log=log.append)
    assert any("Someone else published" in line for line in log)
    assert [e["id"] for e in index(local)["items"]] == ["GSE1", "GSE2", "GSE3"]
    rival_release = read_current(local, "expression").record["base"]
    assert read_current(local, "expression").release == mine
    assert json.loads(local.read(f"collections/expression/releases/{rival_release}/release.json")[0])["base"] == first


def test_rollback_serves_the_base_release_or_the_one_named(local, items, tmp_path):
    first = publish(local, EXPRESSION, [items("GSE1", {"x": b"1"})], [], "first", log=quiet)
    second = publish(local, EXPRESSION, [read_item(make_item(tmp_path / "v2", "GSE1", {"x": b"2"}))], [], "second", log=quiet)
    assert rollback(local, "expression", log=quiet) == first
    assert read_current(local, "expression").release == first
    with pytest.raises(PublishError, match="was the first"):
        rollback(local, "expression", log=quiet)
    assert rollback(local, "expression", second, log=quiet) == second


def test_item_descriptions_are_checked(tmp_path):
    with pytest.raises(ItemError, match="lacks changes"):
        read_item(make_item(tmp_path, "a", {"x": b"1"}, credit={k: v for k, v in CREDIT.items() if k != "changes"}))
    with pytest.raises(ItemError, match="unknown keys"):
        read_item(make_item(tmp_path, "b", {"x": b"1"}, organism="Homo sapiens"))
    with pytest.raises(ItemError, match="`id`"):
        read_item(make_item(tmp_path, "c", {"x": b"1"}, id="has space"))
    with pytest.raises(ItemError, match="not in the folder"):
        read_item(make_item(tmp_path, "d", {"x": b"1"}, entry="missing.zip"))


def test_items_newer_than_the_deployed_app_are_refused(local, items, monkeypatch):
    spatial = Collection("spatial", format_field="sidecarVersion", formats_url="https://app/formats.json")
    monkeypatch.setattr(publish_module.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b'{"sidecarVersion": 2}'))
    store = {"lung.sdata.zarr.zip": b"zip"}
    with pytest.raises(PublishError, match="Deploy the app first"):
        publish(local, spatial, [items("lung", store, entry="lung.sdata.zarr.zip", fields={"sidecarVersion": 3})], [], "too new", log=quiet)
    assert read_current(local, "spatial").release is None
    log = []
    publish(local, spatial, [items("colon", store, entry="lung.sdata.zarr.zip", fields={"sidecarVersion": 1}),
                             items("plain", store, entry="lung.sdata.zarr.zip")], [], "ok", log=log.append)
    assert any("colon (1)" in line for line in log)
    assert [c["id"] for c in index(local, "spatial")["items"]] == ["colon", "plain"]


def test_a_failing_check_command_leaves_the_pointer_alone(local, items):
    first = publish(local, EXPRESSION, [items("GSE1", {"x": b"1"})], [], "first", log=quiet)
    checked = Collection("expression", check_command=("false",))
    with pytest.raises(PublishError, match="refused"):
        publish(local, checked, [items("GSE2", {"x": b"2"})], [], "second", log=quiet)
    assert read_current(local, "expression").release == first


def test_gc_deletes_only_what_no_kept_release_uses(local, items, tmp_path):
    old = items("GSE1", {"x": b"1"})
    first = publish(local, EXPRESSION, [old], [], "first", log=quiet)
    new = read_item(make_item(tmp_path / "v2", "GSE1", {"x": b"2"}))
    publish(local, EXPRESSION, [new], [], "second", log=quiet)
    assert gc(local, "expression", keep_days=30, keep_releases=1, delete=True, log=quiet) == []
    doomed = gc(local, "expression", keep_days=0, keep_releases=1, delete=True, log=quiet)
    assert sorted(doomed) == sorted([f"collections/expression/items/GSE1/{old.hash}/x", f"collections/expression/items/GSE1/{old.hash}/studio-data.json",
                                     f"collections/expression/releases/{first}/index.json", f"collections/expression/releases/{first}/release.json"])
    assert local.read(resolve(local, "expression", index(local)["items"][0]["path"]) + "x")[0] == b"2"
