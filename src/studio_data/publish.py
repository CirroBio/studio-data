"""Publishing a change to a collection, and rolling one back.

A publish applies a list of changes to the current release; it never mirrors a folder, so an item
missing on someone's laptop is never a deletion. Item files are written once under a folder named by
their content hash. The release's index.json and release.json are written once too. The only file
ever overwritten is catalog.json, and only if it still names the release the changes were applied to.
"""
import getpass
import hashlib
import json
import subprocess
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .items import FILE_LIST, FileEntry, Item
from .settings import Collection
from .storage import NO_CACHE, Conflict, Storage, matches

POINTER = "catalog.json"
MAX_ATTEMPTS = 5
# Removals are always named; this many in one publish also need allow_removals.
REMOVALS_WITHOUT_FLAG = 10
WORKERS = 16


class PublishError(RuntimeError):
    pass


def prefix(name: str) -> str:
    return f"collections/{name}"


def folder_of(kind: str) -> str:
    return "reference" if kind == "reference" else "items"


def dumps(value) -> bytes:
    return (json.dumps(value, indent=1) + "\n").encode()


@dataclass
class Current:
    """The release catalog.json names; empty for a collection never published."""
    release: str | None = None
    etag: str | None = None
    index: dict = field(default_factory=dict)
    record: dict = field(default_factory=dict)  # release.json


def read_json(storage: Storage, key: str) -> dict:
    got = storage.read(key)
    if got is None:
        raise PublishError(f"{key} is missing.")
    return json.loads(got[0])


def read_current(storage: Storage, name: str) -> Current:
    got = storage.read(f"{prefix(name)}/{POINTER}")
    if got is None:
        return Current()
    release = json.loads(got[0])["release"]
    base = f"{prefix(name)}/releases/{release}"
    return Current(release, got[1], read_json(storage, f"{base}/index.json"), read_json(storage, f"{base}/release.json"))


@dataclass
class Plan:
    base: Current
    entries: dict[str, dict]  # item id -> its index entry
    hashes: dict[str, str]  # item id -> content hash
    reference: dict[str, str]  # reference id -> path
    reference_hashes: dict[str, str]
    added: list[str]
    changed: list[str]
    removed: list[str]

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


def index_path(item: Item) -> str:
    path = f"../../{folder_of(item.kind)}/{item.id}/{item.hash}/{item.entry}"
    # A folder's path ends in '/', which is how Zarr readers tell a folder from a file.
    return path if path.endswith("/") or not (item.folder / item.entry).is_dir() else path + "/"


def make_plan(current: Current, staged: list[Item], removals: list[str]) -> Plan:
    entries = {e["id"]: e for e in current.index.get("items", [])}
    hashes = dict(current.record.get("items", {}))
    reference = dict(current.index.get("reference", {}))
    reference_hashes = dict(current.record.get("reference", {}))
    added, changed, removed = [], [], []
    seen = set()
    for item in staged:
        if item.id in seen:
            raise PublishError(f"{item.id} is staged twice.")
        if item.id in removals:
            raise PublishError(f"{item.id} is both staged and removed.")
        seen.add(item.id)
        if item.kind == "reference":
            table, table_hashes, other, new = reference, reference_hashes, entries, index_path(item)
        else:
            table, table_hashes, other, new = entries, hashes, reference, {**item.description, "path": index_path(item)}
        if item.id in other:
            raise PublishError(f"{item.id} is already published as {'an item' if other is entries else 'a reference'}.")
        if item.id not in table:
            added.append(item.id)
        elif table[item.id] != new:
            changed.append(item.id)
        table[item.id], table_hashes[item.id] = new, item.hash
    for rid in removals:
        for table, table_hashes in ((entries, hashes), (reference, reference_hashes)):
            if rid in table:
                del table[rid], table_hashes[rid]
                removed.append(rid)
                break
        else:
            raise PublishError(f"{rid} is not in release {current.release}; there is nothing to remove.")
    return Plan(current, entries, hashes, reference, reference_hashes, added, changed, removed)


def summary(name: str, plan: Plan) -> str:
    lines = [f"{name}: {len(plan.added)} added, {len(plan.changed)} changed, {len(plan.removed)} removed "
             f"(from {plan.base.release or 'nothing published yet'})"]
    for word, ids in (("added", plan.added), ("changed", plan.changed), ("removed", plan.removed)):
        if ids:
            lines.append(f"  {word}: {', '.join(ids[:20])}{f' and {len(ids) - 20} more' if len(ids) > 20 else ''}")
    return "\n".join(lines)


def content_key(name: str, item: Item, path: str) -> str:
    return f"{prefix(name)}/{folder_of(item.kind)}/{item.id}/{item.hash}/{path}"


def to_upload(storage: Storage, name: str, staged: list[Item]) -> list[Item]:
    """The staged items whose content isn't in the bucket yet."""
    with ThreadPoolExecutor(WORKERS) as pool:
        done = list(pool.map(lambda i: storage.stat(content_key(name, i, FILE_LIST)) is not None, staged))
    return [i for i, d in zip(staged, done, strict=True) if not d]


def upload_file(storage: Storage, name: str, item: Item, f: FileEntry) -> None:
    key = content_key(name, item, f.path)
    try:
        storage.upload(key, item.folder / f.path, f)
    except Conflict:
        pass  # left by an earlier attempt; checked below like any other
    if not matches(storage.stat(key), f):
        raise PublishError(f"{key} doesn't match {item.folder / f.path} after uploading it.")


def upload(storage: Storage, name: str, items: list[Item]) -> None:
    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(lambda job: upload_file(storage, name, *job), [(i, f) for i in items for f in i.files]))
    for item in items:
        try:
            storage.write(content_key(name, item, FILE_LIST), item.file_list, create_only=True)
        except Conflict:
            pass  # another publisher finished the same content


def check_formats(collection: Collection, entries: dict[str, dict]) -> list[str]:
    """Refuses items newer than the deployed app reads; returns warnings for older ones."""
    if not collection.format_field:
        return []
    # GitHub Pages caches for 10 minutes; the query string asks for what is deployed now.
    with urllib.request.urlopen(f"{collection.formats_url}?t={int(time.time())}", timeout=30) as r:
        readable = json.load(r)[collection.format_field]
    newer, older = [], []
    for id_, e in entries.items():
        version = e.get("fields", {}).get(collection.format_field)
        if version is None:
            continue  # the app works out what it needs from the item itself
        if version > readable:
            newer.append(f"{id_} ({version})")
        elif version < readable:
            older.append(f"{id_} ({version})")
    if newer:
        raise PublishError(f"The deployed app reads {collection.format_field} {readable}; these are newer: {', '.join(newer)}. Deploy the app first.")
    return [f"Older than the deployed app's {collection.format_field} {readable}, so some features may be missing: {', '.join(older)}"] if older else []


def release_id(plan: Plan) -> str:
    content = hashlib.sha256(dumps([plan.entries, plan.reference])).hexdigest()[:6]
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{content}"


def publish(storage: Storage, collection: Collection, staged: list[Item], removals: list[str], message: str,
            allow_removals: bool = False, log: Callable[[str], None] = print) -> str | None:
    """Publishes the staged items and removals as a new release; returns its id, or None if nothing changed."""
    name = collection.name
    uploaded = False
    for _ in range(MAX_ATTEMPTS):
        current = read_current(storage, name)
        plan = make_plan(current, staged, removals)
        log(summary(name, plan))
        if plan.empty:
            return None
        if len(plan.removed) >= REMOVALS_WITHOUT_FLAG and not allow_removals:
            raise PublishError(f"This removes {len(plan.removed)} items; pass allow_removals (--allow-removals) if that is meant.")
        if not uploaded:  # content doesn't depend on the release it joins
            pending = to_upload(storage, name, staged)
            log(f"Uploading {len(pending)} items, {sum(f.size for i in pending for f in i.files):,} bytes.")
            upload(storage, name, pending)
            uploaded = True
        for warning in check_formats(collection, plan.entries):
            log(warning)
        rid = release_id(plan)
        base = f"{prefix(name)}/releases/{rid}"
        index = {"collection": name, "release": rid, **({"title": collection.title} if collection.title else {}),
                 "items": [plan.entries[k] for k in sorted(plan.entries)],
                 "reference": dict(sorted(plan.reference.items()))}
        record = {"release": rid, "base": current.release, "message": message, "publishedBy": getpass.getuser(),
                  "publishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "added": plan.added, "changed": plan.changed, "removed": plan.removed,
                  "items": dict(sorted(plan.hashes.items())), "reference": dict(sorted(plan.reference_hashes.items()))}
        storage.write(f"{base}/index.json", dumps(index), create_only=True)
        storage.write(f"{base}/release.json", dumps(record), create_only=True)
        if collection.check_command:
            if subprocess.run([*collection.check_command, storage.url(f"{base}/index.json")]).returncode:
                raise PublishError(f"{' '.join(collection.check_command)} refused release {rid}; catalog.json is unchanged.")
        pointer = dumps({"release": rid, "index": f"releases/{rid}/index.json"})
        try:
            storage.write(f"{prefix(name)}/{POINTER}", pointer, cache_control=NO_CACHE,
                          if_match=current.etag, create_only=current.etag is None)
        except Conflict:
            log(f"Someone else published {name} meanwhile; applying these changes to their release.")
            continue
        log(f"Published {name} release {rid}.")
        return rid
    raise PublishError(f"{name} kept changing under this publish; gave up after {MAX_ATTEMPTS} attempts.")


def rollback(storage: Storage, name: str, release: str | None = None, log: Callable[[str], None] = print) -> str:
    """Points catalog.json at an earlier release: the one given, or the one the current release was built on."""
    current = read_current(storage, name)
    if current.release is None:
        raise PublishError(f"{name} has never been published.")
    target = release or current.record.get("base")
    if not target:
        raise PublishError(f"{name} release {current.release} was the first; there is nothing to roll back to.")
    if storage.read(f"{prefix(name)}/releases/{target}/index.json") is None:
        raise PublishError(f"{name} has no release {target}.")
    try:
        storage.write(f"{prefix(name)}/{POINTER}", dumps({"release": target, "index": f"releases/{target}/index.json"}),
                      cache_control=NO_CACHE, if_match=current.etag)
    except Conflict:
        raise PublishError(f"{name} changed while rolling back; look at it again and rerun.") from None
    log(f"{name} now serves release {target} (was {current.release}).")
    return target
