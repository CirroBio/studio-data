import json
from pathlib import Path

import pytest

from studio_data.items import read_item
from studio_data.storage import LocalStorage

CREDIT = {"source": "GEO series GSE52778", "url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE52778",
          "by": "Himes BE et al.", "license": "No restrictions (NCBI GEO)",
          "licenseUrl": "https://www.ncbi.nlm.nih.gov/geo/info/disclaimer.html", "changes": "Counts stored compactly."}


def make_item(root: Path, id_: str, files: dict[str, bytes], **description) -> Path:
    folder = root / id_
    for rel, content in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(content)
    (folder / "item.json").write_text(json.dumps({"id": id_, "label": f"Series {id_}", "credit": CREDIT, **description}))
    return folder


@pytest.fixture
def items(tmp_path):
    """Writes an item folder under tmp_path/items and reads it back as the tool would."""
    return lambda id_, files, **d: read_item(make_item(tmp_path / "items", id_, files, **d))


@pytest.fixture
def local(tmp_path):
    return LocalStorage(tmp_path / "bucket")


def quiet(_):
    pass
