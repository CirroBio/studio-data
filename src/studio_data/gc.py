"""Deleting files that no recent release uses.

Only files older than keep_days are deleted, so content a publish has just uploaded, before its
release exists, is safe. Don't run gc while publishing content last used more than keep_days ago:
the publish reuses that content without uploading it again.
"""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from .publish import prefix, read_current, read_json
from .storage import Storage


def gc(storage: Storage, name: str, keep_days: int = 30, keep_releases: int = 5, delete: bool = False,
       log: Callable[[str], None] = print) -> list[str]:
    """Lists, and with delete=True deletes, files no kept release uses. Kept: the current release,
    the newest keep_releases, and every release from the last keep_days."""
    root = prefix(name)
    current = read_current(storage, name)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    release_keys = list(storage.keys(f"{root}/releases/"))
    records = {key.split("/")[3]: read_json(storage, key) for key, _ in release_keys if key.endswith("/release.json")}
    newest = sorted(records, key=lambda r: records[r]["publishedAt"], reverse=True)
    kept = set(newest[:keep_releases]) | {r for r in records if datetime.fromisoformat(records[r]["publishedAt"]) >= cutoff}
    if current.release:
        kept.add(current.release)
    used = {f"{root}/{folder}/{id_}/{content}/" for r in kept
            for folder, table in (("items", records[r]["items"]), ("reference", records[r]["reference"]))
            for id_, content in table.items()}
    doomed = [key for folder in ("items", "reference") for key, modified in storage.keys(f"{root}/{folder}/")
              if modified < cutoff and "/".join(key.split("/")[:5]) + "/" not in used]
    doomed += [key for key, modified in release_keys if modified < cutoff and key.split("/")[3] not in kept]
    log(f"{name}: keeping {len(kept)} of {len(records)} releases; {len(doomed)} files are used by none of them.")
    if delete and doomed:
        storage.delete(doomed)
        log(f"Deleted {len(doomed)} files.")
    return doomed
