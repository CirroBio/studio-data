import assert from "node:assert/strict";
import { test } from "node:test";
import { itemUrl, readRelease, referenceUrl } from "../dist/index.js";

const ROOT = "https://data.example/collections/expression";
const CREDIT = { source: "GEO", url: "https://geo", by: "Lab", license: "No restrictions (NCBI GEO)", licenseUrl: "https://ncbi", changes: "none" };

const serve = (files) => async (url) => (url in files ? new Response(JSON.stringify(files[url])) : new Response("", { status: 404 }));

test("reads the pointer, then the release index, and resolves paths against the index", async () => {
  const fetchFn = serve({
    [`${ROOT}/catalog.json`]: { release: "r2", index: "releases/r2/index.json" },
    [`${ROOT}/releases/r2/index.json`]: {
      collection: "expression", release: "r2", reference: { "genes-hsapiens": "../../reference/genes-hsapiens/ab/hsapiens.tsv.gz" },
      items: [{ id: "GSE1", label: "One", credit: CREDIT, path: "../../items/GSE1/cd/" }],
    },
  });
  const release = await readRelease(ROOT, fetchFn);
  assert.equal(release.release, "r2");
  assert.equal(release.indexUrl, `${ROOT}/releases/r2/index.json`);
  assert.equal(itemUrl(release, release.items[0]), `${ROOT}/items/GSE1/cd/`);
  assert.equal(itemUrl(release, release.items[0], "datasets/counts/data/samplesheet.tsv"), `${ROOT}/items/GSE1/cd/datasets/counts/data/samplesheet.tsv`);
  assert.equal(referenceUrl(release, "genes-hsapiens"), `${ROOT}/reference/genes-hsapiens/ab/hsapiens.tsv.gz`);
  assert.throws(() => referenceUrl(release, "genes-mmusculus"), /no reference genes-mmusculus/);
});

test("resolves a file item from a collection URL ending in a slash, and refuses an index without items", async () => {
  const root = "https://data.example/collections/spatial/";
  const lung = { id: "lung", label: "Lung", credit: CREDIT, path: "../../items/lung/ef/lung.sdata.zarr.zip" };
  const release = await readRelease(root, serve({
    [`${root}catalog.json`]: { release: "r1", index: "releases/r1/index.json" },
    [`${root}releases/r1/index.json`]: { collection: "spatial", release: "r1", reference: {}, items: [lung] },
  }));
  assert.deepEqual(release.items.map((i) => itemUrl(release, i)), [`${root}items/lung/ef/lung.sdata.zarr.zip`]);
  await assert.rejects(readRelease(root, serve({
    [`${root}catalog.json`]: { release: "r1", index: "releases/r1/index.json" },
    [`${root}releases/r1/index.json`]: { collection: "spatial", release: "r1", reference: {}, checkpoints: [lung] },
  })), /lists no items/);
});

test("names the address it couldn't read", async () => {
  await assert.rejects(readRelease(ROOT, serve({})), new RegExp(`${ROOT}/catalog.json \\(HTTP 404\\)`));
});
