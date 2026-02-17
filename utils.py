import os
import sys
import time
import subprocess
from pathlib import Path

def die(m):
    print(m)
    sys.exit(1)

def require_env(n):
    v = os.environ.get(n)
    if not v:
        die(n)
    return v

def download_with_retry(url, output, retries=3):
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    for _ in range(retries):
        subprocess.run([
            "curl",
            "-L",
            "--fail",
            "-o",
            output,
            url
        ])

        time.sleep(1)

        p = Path(output)

        if p.exists() and p.stat().st_size > 10_000:
            return 0

        time.sleep(2)

    return 1

def ensure_apk(p):
    r = subprocess.run(["file", p], capture_output=True, text=True)
    if "android" not in r.stdout.lower():
        die("bad apk")

def mkdir_clean(*dirs):
    for d in dirs:
        p = Path(d)
        if p.exists():
            subprocess.run(["rm", "-rf", d])
        p.mkdir(parents=True, exist_ok=True)

def gh_blob_to_raw(u):
    if "github.com" in u and "/blob/" in u:
        return u.replace(
            "https://github.com/",
            "https://raw.githubusercontent.com/"
        ).replace("/blob/", "/")
    return u

def run(cmd):
    r = subprocess.run(cmd)
    if r.returncode != 0:
        die("command failed: " + " ".join(cmd))
