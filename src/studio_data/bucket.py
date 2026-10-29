"""The bucket's own settings (setup), and a check of a collection as a browser reads it (check)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from botocore.exceptions import ClientError

from .items import FILE_LIST, parse_file_list
from .publish import POINTER, WORKERS, prefix

POLICY_SID = "StudioDataPublicRead"
RULE_ID = "studio-data"
NONCURRENT_DAYS = 30
ABANDONED_UPLOAD_DAYS = 7
# Spatial's reader learns a store's length from the Content-Range of a bytes=0-0 GET.
EXPOSED = ["Content-Range", "Accept-Ranges", "Content-Length", "Content-Type", "ETag"]
CHECK_ORIGIN = "https://studio-data-check.example"


def policy_statement(bucket: str) -> dict:
    return {"Sid": POLICY_SID, "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket}/collections/*"}


CORS_RULE = {"ID": RULE_ID, "AllowedMethods": ["GET", "HEAD"], "AllowedOrigins": ["*"],
             "AllowedHeaders": ["Range", "If-None-Match", "If-Modified-Since"], "ExposeHeaders": EXPOSED, "MaxAgeSeconds": 3600}
LIFECYCLE_RULE = {"ID": RULE_ID, "Filter": {"Prefix": "collections/"}, "Status": "Enabled",
                  "Expiration": {"ExpiredObjectDeleteMarker": True},
                  "NoncurrentVersionExpiration": {"NoncurrentDays": NONCURRENT_DAYS},
                  "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": ABANDONED_UPLOAD_DAYS}}


def _get(call, missing: str, key: str, default, **args):
    try:
        return call(**args)[key]
    except ClientError as e:
        if e.response["Error"]["Code"] == missing:
            return default
        raise


def _with(rules: list[dict], rule: dict, id_key: str) -> list[dict]:
    """The bucket may be shared: keep everyone else's rules and replace only ours."""
    return [r for r in rules if r.get(id_key) != rule[id_key]] + [rule]


def setup(s3, bucket: str, apply: bool = False, log: Callable[[str], None] = print) -> list[str]:
    changes = []
    if s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
        changes.append(("versioning: on", lambda: s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})))
    policy = json.loads(_get(s3.get_bucket_policy, "NoSuchBucketPolicy", "Policy", '{"Version": "2012-10-17", "Statement": []}', Bucket=bucket))
    if policy_statement(bucket) not in policy["Statement"]:
        new_policy = {**policy, "Statement": _with(policy["Statement"], policy_statement(bucket), "Sid")}
        changes.append(("policy: anyone may read collections/", lambda: s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(new_policy))))
    cors = _get(s3.get_bucket_cors, "NoSuchCORSConfiguration", "CORSRules", [], Bucket=bucket)
    if CORS_RULE not in cors:
        changes.append(("CORS: GET and HEAD from any origin, Range allowed, Content-Range exposed",
                        lambda: s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": _with(cors, CORS_RULE, "ID")})))
    lifecycle = _get(s3.get_bucket_lifecycle_configuration, "NoSuchLifecycleConfiguration", "Rules", [], Bucket=bucket)
    if LIFECYCLE_RULE not in lifecycle:
        changes.append((f"lifecycle: old versions expire after {NONCURRENT_DAYS} days, abandoned uploads after {ABANDONED_UPLOAD_DAYS}",
                        lambda: s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": _with(lifecycle, LIFECYCLE_RULE, "ID")})))
    for text, apply_change in changes:
        log(f"{'Setting' if apply else 'Would set'} {text}")
        if apply:
            apply_change()
    if not changes:
        log(f"{bucket} already has studio-data's settings.")
    return [text for text, _ in changes]


def _request(url: str, method: str = "GET", headers: dict | None = None):
    request = urllib.request.Request(url, method=method, headers={"Origin": CHECK_ORIGIN, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, b""


def _check_folder(url: str) -> list[str]:
    status, _, body = _request(url + FILE_LIST)
    if status != 200:
        return [f"{url}{FILE_LIST}: HTTP {status}"]
    problems = []
    for f in parse_file_list(body):
        # As Spatial's reader does: the length from the Content-Range of a one-byte read, never HEAD.
        status, headers, _ = _request(url + urllib.parse.quote(f.path), headers={"Range": "bytes=0-0"} if f.size else None)
        length = int(headers.get("Content-Range", "/-1").rsplit("/", 1)[1]) if status == 206 else int(headers.get("Content-Length", -1))
        if status not in (200, 206) or length != f.size:
            problems.append(f"{url}{f.path}: HTTP {status}, {length} bytes where {f.size} were published")
        elif headers.get("Content-Encoding"):
            problems.append(f"{url}{f.path}: served with Content-Encoding {headers['Content-Encoding']}")
    return problems


def check(base_url: str, name: str, log: Callable[[str], None] = print) -> list[str]:
    """Reads a collection the way an app does, from another origin, and every file its release lists."""
    root = f"{base_url.rstrip('/')}/{prefix(name)}"
    status, headers, body = _request(f"{root}/{POINTER}?t={int(time.time())}")
    if status != 200:
        problems = [f"{root}/{POINTER}: HTTP {status}"]
        log(problems[0])
        return problems
    problems = []
    if not headers.get("Access-Control-Allow-Origin"):
        problems.append(f"{POINTER}: no Access-Control-Allow-Origin, so browsers on other sites can't read it (CORS)")
    if "no-cache" not in (headers.get("Cache-Control") or ""):
        problems.append(f"{POINTER}: Cache-Control is {headers.get('Cache-Control')!r}, not no-cache")
    pointer = json.loads(body)
    status, headers, _ = _request(urllib.parse.urljoin(f"{root}/{POINTER}", pointer["index"]), headers={"Range": "bytes=0-0"})
    if status != 206 or not headers.get("Content-Range"):
        problems.append(f"index.json: a bytes=0-0 request gave HTTP {status} without Content-Range")
    if "content-range" not in (headers.get("Access-Control-Expose-Headers") or "").lower():
        problems.append("Content-Range isn't exposed to browsers (CORS ExposeHeaders)")
    if "immutable" not in (headers.get("Cache-Control") or ""):
        problems.append(f"index.json: Cache-Control is {headers.get('Cache-Control')!r}, not immutable")
    record = json.loads(_request(f"{root}/releases/{pointer['release']}/release.json")[2])
    folders = [f"{root}/{folder}/{id_}/{content}/" for folder, table in (("items", record["items"]), ("reference", record["reference"]))
               for id_, content in table.items()]
    with ThreadPoolExecutor(WORKERS) as pool:
        problems += [p for found in pool.map(_check_folder, folders) for p in found]
    for p in problems:
        log(p)
    log(f"{name} release {pointer['release']}: {len(folders)} items read, {len(problems)} problems.")
    return problems
