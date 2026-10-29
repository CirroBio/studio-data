# studio-data

Publishes the public example data of Expression Studio, Cirro Studios, Spatial Data Studio and Slide Studio to one S3 bucket, and reads it back in the browser. Data is added from a laptop with the `studio-data` command; nothing about data goes through git.

The design and the decisions behind it: [Shared Public Data Catalog](https://claude.ai/artifact/U8pgsUxWHTikDtJ1ZfGbR8).

## How it works

Each studio has a collection in the bucket:

```
collections/<collection>/
  catalog.json                       the only file ever overwritten: names the current release
  releases/<release>/index.json      the release's items, with what a browse page needs
  releases/<release>/release.json    message, who, when, the release it was built on, what changed
  items/<id>/<content-hash>/         an item's files, written once, plus studio-data.json listing them
  reference/<id>/<content-hash>/     files several items share, such as gene maps
```

- **Apps** read `catalog.json`, then the index it names, then the paths in the index, which resolve against the index's own URL. They never list the bucket.
- **Publishing** applies changes to the current release; it never mirrors a folder, so an item missing on your laptop is never a deletion.
  1. Compare the item folders you give it with the current release.
  2. Upload only content not already there. S3 refuses to overwrite any key, and checks every file's SHA-256 as it arrives; files over 64 MB go up in parts, checked by a whole-file CRC32.
  3. Check every uploaded file's size and checksum.
  4. Write the release, then switch `catalog.json`, but only if it still names the release the changes were applied to. If someone published in between, the changes are applied again to theirs.
- **Caching:** `catalog.json` is served `no-cache`; every other file `max-age=31536000, immutable`. `.gz` and `.zip` files are stored as they are, with no `Content-Encoding`.

## Install

```bash
uv tool install git+https://github.com/CirroBio/studio-data
```

Publishing to the bucket uses your AWS credentials (the usual `AWS_PROFILE` and friends). Until the bucket is set in `src/studio_data/collections.toml`, publish to a local folder with `--to FOLDER`. That folder has the bucket's layout, so an app's dev data URL can serve it.

## An item folder

Your builder writes one folder per item: an `item.json` and the files to publish. Everything in the folder except `item.json` is published.

```json
{
  "id": "xenium-human-lung",
  "label": "Xenium human lung (raw counts)",
  "description": "11,898 cells x 289 genes as the instrument counted them.",
  "folder": "Xenium",
  "entry": "xenium-human-lung.sdata.zarr.zip",
  "credit": {
    "source": "Xenium Human Lung Gene Expression",
    "url": "https://www.10xgenomics.com/support/software/xenium-onboard-analysis/latest/resources/xenium-example-data",
    "by": "10x Genomics",
    "license": "CC BY 4.0",
    "licenseUrl": "https://creativecommons.org/licenses/by/4.0/",
    "changes": "Read with spatialdata-io and saved with its raw counts."
  },
  "fields": { "platform": "Xenium", "cells": 11898, "sidecarVersion": 2 }
}
```

| Key | Required | What it is |
| --- | --- | --- |
| `id` | yes | Letters, digits, `.`, `_`, `-`. Stays the same when the item is rebuilt; only its content-hash folder changes. |
| `label` | yes | The item's name in the browse list. |
| `credit` | yes | `source`, `url`, `by`, `license`, `licenseUrl` and `changes`, all non-empty: the attribution a CC BY license asks for. |
| `description` | no | A paragraph about the item. |
| `folder` | no | Where the app files it, such as `Human/Eye`. |
| `fields` | no | The studio's own facts for its browse page, such as organism, samples, platform, or a format version. |
| `entry` | no | The file or folder inside the item the index path points at. Without it, the path is the item folder itself (ending in `/`). |
| `kind` | no | `reference` publishes the folder as a shared file listed under the index's `reference` map, such as a gene map; it needs only `id` (and usually `entry`). |

In the release index, an item is its `item.json` without `kind` and `entry`, plus `path`.

Use `entry` only when the item is one file or one store, as a Spatial store is. When an item holds several things an app opens separately, such as a slide and the cells store made from it, leave `entry` out so `path` is the item folder. Name the parts in `fields` relative to that folder, ending a folder's name in `/` (for example `"slide": "CMU-1.ome.zarr/"` and `"cells": "CMU-1.cells.zarr/"`), and the app opens them with `itemUrl(release, item, item.fields.cells)`. Resolved against an `entry` instead, the same name would land beside a file but inside a folder.

## Commands

```bash
studio-data status expression path/to/GSE164073 --remove GSE52778         # what would change, and how much would be uploaded
studio-data publish expression path/to/GSE164073 -m "Add GSE164073"       # publish a new release
studio-data rollback expression [RELEASE]                                 # serve an earlier release (default: the one before)
studio-data gc expression [--keep-days 30] [--keep-releases 5] [--delete] # list, then delete, files no kept release uses
studio-data setup [--apply]                                               # the bucket settings the collections need
studio-data check expression [--url ADDRESS]                              # read a collection as a browser would
```

- Removals are always named with `--remove`. Ten or more in one publish also need `--allow-removals`.
- `gc` keeps the current release, the newest five, and everything from the last 30 days. It only deletes files older than that, so content just uploaded by a publish in progress is safe. Don't run it while republishing content last used more than 30 days ago: the publish reuses that content without uploading it again.
- `setup` leaves the bucket's other rules alone. It turns on versioning, lets anyone read `collections/`, allows GET and HEAD from any origin with `Range` (exposing `Content-Range`), and expires old versions after 30 days and abandoned uploads after 7.
- `check` reads `catalog.json` and the index from another origin, then every file the release lists, by a one-byte range request, as Spatial's reader does.

## Collections

`src/studio_data/collections.toml` names the bucket and the collections: `expression`, `microbiome`, `singlecell`, `flow`, `crispr`, `multiomics`, `spatial` and `slides`. Per collection:
- `format_field` and `formats_url`: each item's format version under `fields`, and the deployed app's `formats.json` naming the newest version it reads. A publish is refused while any item is newer, and warns about older ones. An item with no version counts as readable.
- `check_command`: run with the new release's index URL before the switch. A non-zero exit refuses the release.

## Reading a collection

The browser reader is a small TypeScript module in this repo (`reader/src/index.ts`, published as `@cirrobio/studio-data`):

```ts
import { readRelease, itemUrl, referenceUrl } from "@cirrobio/studio-data";

const release = await readRelease("https://<bucket address>/collections/expression");
for (const item of release.items) console.log(item.label, item.credit.license, itemUrl(release, item));
const samplesheet = itemUrl(release, release.items[0], "datasets/<dataset>/data/samplesheet.tsv");
const genes = referenceUrl(release, "genes-hsapiens");
```

Install it pinned to a commit: `npm install github:CirroBio/studio-data#<commit>` (npm builds it on install).

## Development

```bash
uv venv --python 3.12 && uv pip install -e '.[test]'
.venv/bin/python -m pytest      # the tool, against a local folder and moto's S3
npm install && npm test         # the reader
```

## License

Source available under the Cirro Bio Source Available License — see
[LICENSE.md](LICENSE.md). Viewing the source and preparing contributions is
permitted by default; any other use requires written authorization from
Cirro Bio, Inc.
