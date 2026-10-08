#!/usr/bin/env python3
"""
Test S3 access for SBI MIS bucket.

Run from the staging EC2 server (IAM role required):
    python3 test_sbi_s3_access.py

Prints bucket reachability + full listing of each SBI MIS folder so you
can inspect the real filename convention before implementing month extraction.
"""

import sys

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    print("ERROR: boto3 not installed. Run:  pip install boto3")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

BUCKET = "sbi-mis"
REGION = "ap-south-1"

FOLDERS = [
    "sbi_pharmacy_dump",           # → raw (main Metabase dump)
    "sbi_ahc_dump",                # → ahc
    "sbi_wallet_utilization_dump", # → pf_summary
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def hr(char="-", width=60):
    print(char * width)


def sizeof_fmt(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    s3 = boto3.client("s3", region_name=REGION)

    # 1. Bucket reachability
    hr("=")
    print(f"Bucket : s3://{BUCKET}  (region: {REGION})")
    hr("=")
    try:
        s3.head_bucket(Bucket=BUCKET)
        print("✓ Bucket is reachable\n")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        print(f"✗ Cannot reach bucket — {code}: {e.response['Error']['Message']}")
        sys.exit(1)
    except (BotoCoreError, Exception) as e:
        print(f"✗ Unexpected error: {e}")
        sys.exit(1)

    # 2. List each folder
    for folder in FOLDERS:
        hr()
        print(f"Folder: {folder}/")
        hr()

        try:
            paginator = s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=BUCKET, Prefix=f"{folder}/")

            objects = []
            for page in pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    # Skip the folder placeholder object itself
                    if key == f"{folder}/" or key.endswith("/"):
                        continue
                    objects.append(obj)

            if not objects:
                print("  (empty — no files found)\n")
                continue

            # Sort newest first
            objects.sort(key=lambda o: o["LastModified"], reverse=True)

            for obj in objects:
                filename    = obj["Key"].split("/")[-1]
                size        = sizeof_fmt(obj["Size"])
                last_mod    = obj["LastModified"].strftime("%Y-%m-%d %H:%M:%S UTC")
                etag        = obj["ETag"].strip('"')
                latest_tag  = "  ← LATEST" if obj is objects[0] else ""

                print(f"  {filename}")
                print(f"    size:          {size}")
                print(f"    last_modified: {last_mod}{latest_tag}")
                print(f"    etag:          {etag}")
                print()

        except ClientError as e:
            print(f"  ERROR listing folder: {e.response['Error']['Message']}\n")
        except Exception as e:
            print(f"  ERROR: {e}\n")

    hr("=")
    print("Done.")
    hr("=")


if __name__ == "__main__":
    main()
