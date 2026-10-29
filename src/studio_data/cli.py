"""studio-data: publish and look after the public example data of the Cirro studios."""
import argparse
import sys
from pathlib import Path

import boto3

from . import bucket, settings
from .gc import gc
from .items import ItemError, read_item
from .publish import PublishError, make_plan, publish, read_current, rollback, summary, to_upload
from .storage import LocalStorage, S3Storage, bucket_url


def storage_for(args, s: settings.Settings):
    if args.to:
        return LocalStorage(Path(args.to))
    if not s.bucket:
        raise SystemExit("collections.toml doesn't name the bucket yet; publish to a local folder with --to FOLDER.")
    return S3Storage(s.bucket, s.region, s.public_url)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="studio-data", description=__doc__)
    p.add_argument("--settings", type=Path, help="a collections.toml to use instead of the one shipped with the tool")
    commands = p.add_subparsers(dest="command", required=True)

    def command(name: str, text: str) -> argparse.ArgumentParser:
        c = commands.add_parser(name, help=text, description=text)
        c.add_argument("collection")
        c.add_argument("--to", metavar="FOLDER", help="a local folder in the bucket's layout, instead of the bucket")
        return c

    for name, text in (("status", "Show what publishing these items and removals would change, and how much would be uploaded."),
                       ("publish", "Publish these items and removals as the collection's new release.")):
        c = command(name, text)
        c.add_argument("items", nargs="*", type=Path, metavar="ITEM", help="an item folder: item.json and its files")
        c.add_argument("--remove", action="append", default=[], metavar="ID", help="remove a published item (repeatable)")
        if name == "publish":
            c.add_argument("-m", "--message", required=True, help="why, recorded in release.json")
            c.add_argument("--allow-removals", action="store_true", help="allow removing many items at once")
    c = command("rollback", "Serve an earlier release: the one named, or the one the current release was built on.")
    c.add_argument("release", nargs="?")
    c = command("gc", "List, and with --delete delete, files that no kept release uses.")
    c.add_argument("--keep-days", type=int, default=30)
    c.add_argument("--keep-releases", type=int, default=5)
    c.add_argument("--delete", action="store_true")
    c = commands.add_parser("setup", help="Show, and with --apply set, the bucket settings the collections need.")
    c.add_argument("--apply", action="store_true")
    c = commands.add_parser("check", help="Read a collection as a browser would, and every file its release lists.")
    c.add_argument("collection")
    c.add_argument("--url", help="the address apps read the bucket at (default: from collections.toml)")
    return p


def run(args) -> int:
    s = settings.load(args.settings)
    if args.command == "setup":
        if not s.bucket:
            raise SystemExit("collections.toml doesn't name the bucket yet.")
        bucket.setup(boto3.client("s3", region_name=s.region), s.bucket, apply=args.apply)
        return 0
    if args.command == "check":
        s.collection(args.collection)
        if not (args.url or s.bucket):
            raise SystemExit("collections.toml doesn't name the bucket yet; give the address to read with --url.")
        return 1 if bucket.check(args.url or bucket_url(s.bucket, s.region, s.public_url), args.collection) else 0
    collection = s.collection(args.collection)
    storage = storage_for(args, s)
    if args.command == "status":
        staged = [read_item(p) for p in args.items]
        print(summary(collection.name, make_plan(read_current(storage, collection.name), staged, args.remove)))
        pending = to_upload(storage, collection.name, staged)
        print(f"{len(pending)} of {len(staged)} staged items have content to upload: {sum(f.size for i in pending for f in i.files):,} bytes.")
    elif args.command == "publish":
        publish(storage, collection, [read_item(p) for p in args.items], args.remove, args.message, args.allow_removals)
    elif args.command == "rollback":
        rollback(storage, collection.name, args.release)
    elif args.command == "gc":
        gc(storage, collection.name, args.keep_days, args.keep_releases, args.delete)
    return 0


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        sys.exit(run(args))
    except (ItemError, PublishError, settings.SettingsError) as e:
        sys.exit(f"studio-data: {e}")
