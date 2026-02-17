import os
import sys
import time
import subprocess
from pathlib import Path

# -------------------------
# Fatal exit
# -------------------------
def die(m):
    print(m)
    sys.exit(1)

# -------------------------
# Required env vars
# -------------------------
def require_env(n):
    v = os.environ.get(n)
    if not v:
        die(n)
    return v

# -------------------------------------------------
# curl download (Termux safe)
# SUCCESS = file exists AND > 10KB
# -------------------------------------------------
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

        # Let Android FS settle
        time.sleep(1)

        p = Path(output)

        # Accept any real file (>10KB)
        if p.exists() and p.stat().st_size > 10_000:
            return 0

        time.sleep(2)

    return 1

# -------------------------
# APK sanity check
# -------------------------
def ensure_apk(p):
    r = subprocess.run(["file", p], capture_output=True, text=True)
    if "android" not in r.stdout.lower():
        die("bad apk")

# -------------------------
# Clean + recreate dirs
# -------------------------
def mkdir_clean(*dirs):
    for d in dirs:
        p = Path(d)
        if p.exists():
            subprocess.run(["rm", "-rf", d])
        p.mkdir(parents=True, exist_ok=True)

# -------------------------
# GitHub blob → raw
# -------------------------
def gh_blob_to_raw(u):
    if "github.com" in u and "/blob/" in u:
        return u.replace(
            "https://github.com/",
            "https://raw.githubusercontent.com/"
        ).replace("/blob/", "/")
    return u

# -------------------------
# Run command or die
# -------------------------
def run(cmd):
    r = subprocess.run(cmd)
    if r.returncode != 0:
        die("command failed: " + " ".join(cmd))