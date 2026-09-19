#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path

import boto3
from botocore.config import Config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="graph/v1/TW")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--delete-stale", action="store_true")
    args = ap.parse_args()

    endpoint = os.environ["R2_ENDPOINT"]
    access = os.environ["R2_ACCESS_KEY_ID"]
    secret = os.environ["R2_SECRET_ACCESS_KEY"]

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="auto",
        config=Config(
            retries={"max_attempts": 10, "mode": "adaptive"},
            max_pool_connections=max(20, args.workers * 2),
        ),
    )

    root = Path(args.dir)
    files = sorted(p for p in root.rglob("*") if p.is_file())
    wanted = {}

    for p in files:
        rel = p.relative_to(root).as_posix()
        key = f"{args.prefix.rstrip('/')}/{rel}"
        wanted[key] = p

    def upload(item):
        key, path = item
        suffix = path.suffix.lower()
        if suffix == ".json":
            content_type = "application/json"
        elif suffix == ".bin":
            content_type = "application/octet-stream"
        else:
            content_type = "application/octet-stream"
        s3.upload_file(
            str(path),
            args.bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return key

    print(f"Uploading {len(wanted)} objects to r2://{args.bucket}/{args.prefix}/")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, key in enumerate(ex.map(upload, wanted.items()), 1):
            if i % 100 == 0 or i == len(wanted):
                print(f"  uploaded {i}/{len(wanted)}")

    deleted = 0
    if args.delete_stale:
        token = None
        stale = []
        prefix = args.prefix.rstrip("/") + "/"
        while True:
            kw = {"Bucket": args.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kw)
            for o in resp.get("Contents", []):
                k = o["Key"]
                if k not in wanted:
                    stale.append({"Key": k})
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")

        for i in range(0, len(stale), 1000):
            chunk = stale[i:i+1000]
            if chunk:
                s3.delete_objects(Bucket=args.bucket, Delete={"Objects": chunk, "Quiet": True})
                deleted += len(chunk)

    print(json.dumps({
        "uploaded": len(wanted),
        "deletedStale": deleted,
        "bucket": args.bucket,
        "prefix": args.prefix,
    }))


if __name__ == "__main__":
    main()
