"""An item folder as a team's builder writes it: item.json plus the files it publishes."""
import base64
import hashlib
import json
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

DESCRIPTION = "item.json"
# Written last into each published item folder: its presence means every file arrived and was checked.
FILE_LIST = "studio-data.json"
CREDIT_KEYS = ("source", "url", "by", "license", "licenseUrl", "changes")
KINDS = ("item", "reference")
DESCRIPTION_KEYS = {"id", "kind", "entry", "label", "description", "folder", "credit", "fields"}
# Files larger than this go up in parts. S3 keeps no whole-file SHA-256 for those, so they are
# also checked by a whole-file CRC32, which S3 verifies on completion.
PART_SIZE = 64 * 1024 * 1024
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
READ_SIZE = 8 * 1024 * 1024


class ItemError(ValueError):
    pass


@dataclass(frozen=True)
class FileEntry:
    path: str  # relative to the item folder, '/'-separated
    size: int
    sha256: str  # base64, the form S3 reports
    crc32: str | None = None  # base64; only for files uploaded in parts

    def as_json(self) -> dict:
        return {k: v for k, v in vars(self).items() if v is not None}


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    entry: str  # what the index path points at, relative to the item folder; "" is the folder itself
    description: dict  # what the index says of the item besides its path
    files: tuple[FileEntry, ...]
    folder: Path

    @property
    def file_list(self) -> bytes:
        return file_list_json(self.files)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.file_list).hexdigest()[:16]


def file_list_json(files) -> bytes:
    return (json.dumps({"files": [f.as_json() for f in files]}, indent=1, sort_keys=True) + "\n").encode()


def _b64(digest: bytes) -> str:
    return base64.b64encode(digest).decode()


def describe_file(path: Path, relative: str) -> FileEntry:
    sha, crc, size = hashlib.sha256(), 0, path.stat().st_size
    with path.open("rb") as f:
        while chunk := f.read(READ_SIZE):
            sha.update(chunk)
            if size > PART_SIZE:
                crc = zlib.crc32(chunk, crc)
    return FileEntry(relative, size, _b64(sha.digest()), _b64(crc.to_bytes(4, "big")) if size > PART_SIZE else None)


def _check_description(d: dict, where: Path) -> None:
    def fail(msg: str):
        raise ItemError(f"{where}: {msg}")

    unknown = set(d) - DESCRIPTION_KEYS
    if unknown:
        fail(f"unknown keys {sorted(unknown)}; allowed: {sorted(DESCRIPTION_KEYS)}")
    if not isinstance(d.get("id"), str) or not ID.match(d["id"]):
        fail("`id` must be letters, digits, '.', '_' or '-', starting with a letter or digit")
    if d.get("kind", "item") not in KINDS:
        fail(f"`kind` must be one of {KINDS}")
    if "fields" in d and not isinstance(d["fields"], dict):
        fail("`fields` must be an object")
    if d.get("kind", "item") == "reference":
        return
    if not isinstance(d.get("label"), str) or not d["label"].strip():
        fail("`label` is required")
    credit = d.get("credit")
    if not isinstance(credit, dict):
        fail(f"`credit` is required, with {', '.join(CREDIT_KEYS)}")
    missing = [k for k in CREDIT_KEYS if not isinstance(credit.get(k), str) or not credit[k].strip()]
    if missing:
        fail(f"`credit` lacks {', '.join(missing)}")
    extra = set(credit) - set(CREDIT_KEYS)
    if extra:
        fail(f"`credit` has unknown keys {sorted(extra)}")


def read_item(folder: Path) -> Item:
    folder = Path(folder)
    try:
        d = json.loads((folder / DESCRIPTION).read_text())
    except FileNotFoundError:
        raise ItemError(f"{folder} has no {DESCRIPTION}") from None
    _check_description(d, folder / DESCRIPTION)
    entry = d.get("entry", "")
    if entry and not (folder / entry).exists():
        raise ItemError(f"{folder / DESCRIPTION}: `entry` {entry!r} is not in the folder")
    if (folder / FILE_LIST).exists():
        raise ItemError(f"{folder}: {FILE_LIST} is the tool's own name; rename that file")
    paths = sorted(p for p in folder.rglob("*") if p.is_file() and p != folder / DESCRIPTION)
    if not paths:
        raise ItemError(f"{folder} has no files besides {DESCRIPTION}")
    files = tuple(describe_file(p, p.relative_to(folder).as_posix()) for p in paths)
    description = {k: v for k, v in d.items() if k not in ("kind", "entry")}
    return Item(d["id"], d.get("kind", "item"), entry, description, files, folder)


def parse_file_list(data: bytes) -> tuple[FileEntry, ...]:
    return tuple(FileEntry(**f) for f in json.loads(data)["files"])
