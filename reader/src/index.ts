// Reading a collection published by studio-data: catalog.json names the current release, the
// release's index lists its items, and every path in the index resolves against the index's URL.

export type Credit = { source: string; url: string; by: string; license: string; licenseUrl: string; changes: string };

export type Item = {
  id: string;
  label: string;
  description?: string;
  folder?: string;
  credit: Credit;
  fields?: Record<string, unknown>;
  // Relative to the index; ends in "/" when the item is a folder.
  path: string;
};

export type Release = {
  collection: string;
  release: string;
  title?: string;
  // What item and reference paths resolve against.
  indexUrl: string;
  items: Item[];
  reference: Record<string, string>;
};

async function readJson<T>(url: string, fetchFn: typeof fetch): Promise<T> {
  const res = await fetchFn(url);
  if (!res.ok) throw new Error(`Couldn't read ${url} (HTTP ${res.status}).`);
  return (await res.json()) as T;
}

// The current release of the collection whose catalog.json is in `collectionUrl`, such as
// https://<bucket address>/collections/expression. catalog.json is served no-cache, so a browser
// always asks for the current one; everything it leads to never changes.
export async function readRelease(collectionUrl: string, fetchFn: typeof fetch = fetch): Promise<Release> {
  const pointerUrl = new URL("catalog.json", collectionUrl.replace(/\/?$/, "/")).href;
  const pointer = await readJson<{ release: string; index: string }>(pointerUrl, fetchFn);
  const indexUrl = new URL(pointer.index, pointerUrl).href;
  const index = await readJson<Omit<Release, "indexUrl">>(indexUrl, fetchFn);
  if (!Array.isArray(index.items)) throw new Error(`${indexUrl} lists no items.`);
  return { ...index, indexUrl };
}

// The address of an item, or of a file inside an item that is a folder.
export function itemUrl(release: Release, item: Item, file = ""): string {
  return new URL(file, new URL(item.path, release.indexUrl)).href;
}

export function referenceUrl(release: Release, name: string): string {
  const path = release.reference[name];
  if (path === undefined) throw new Error(`${release.collection} release ${release.release} has no reference ${name}.`);
  return new URL(path, release.indexUrl).href;
}
