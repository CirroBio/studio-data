"""The bucket and the collections the tool publishes to: collections.toml, shipped with the tool."""
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Collection:
    name: str
    title: str | None = None
    # The format version each item records under `fields`, and the deployed app's formats.json naming
    # the newest version it reads. A release is refused while any item is newer than that.
    format_field: str | None = None
    formats_url: str | None = None
    # Run with the new release's index URL as its last argument, before the switch; non-zero refuses it.
    check_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    bucket: str
    region: str
    public_url: str
    collections: dict[str, Collection]

    def collection(self, name: str) -> Collection:
        if name not in self.collections:
            raise SettingsError(f"No collection {name!r}; collections.toml has {', '.join(sorted(self.collections))}.")
        return self.collections[name]


def load(path: Path | None = None) -> Settings:
    text = Path(path).read_text() if path else resources.files("studio_data").joinpath("collections.toml").read_text()
    raw = tomllib.loads(text)
    bucket = raw.get("bucket", {})
    collections = {}
    for name, c in raw.get("collections", {}).items():
        collection = Collection(name, **{**c, "check_command": tuple(c.get("check_command", ()))})
        if bool(collection.format_field) != bool(collection.formats_url):
            raise SettingsError(f"Collection {name}: format_field and formats_url go together.")
        collections[name] = collection
    return Settings(bucket.get("name", ""), bucket.get("region", ""), bucket.get("public_url", ""), collections)
