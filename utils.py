import os
import sys
import time
import subprocess
from pathlib import Path
from typing import List, Dict

RESET = "\033[0m"

RED = "\033[38;2;251;44;54m"
ORANGE = "\033[38;2;255;105;42m"
YELLOW = "\033[38;2;240;177;59m"
GREEN = "\033[38;2;49;201;80m"

BLUE = "\033[38;2;43;127;255m"
SKY = "\033[38;2;52;166;244m"
CYAN = "\033[38;2;59;184;219m"

PINK = "\033[38;2;246;51;154m"
PURPLE = "\033[38;2;218;178;255m"

AMBER = "\033[1;38;2;255;210;48m"
NEUTRAL = "\033[1;38;2;212;212;212m"


def die(m):
    print(f"{RED}[ERROR]{RESET} {m}", flush=True)
    sys.exit(1)


def log_space():
    print("", flush=True)


def log_download(msg):
    print(f"{CYAN}[DOWNLOAD]{RESET} {msg}", flush=True)


def log_done(msg):
    print(f"{GREEN}[DONE]{RESET} {msg}", flush=True)


def log_retry(msg):
    print(f"{ORANGE}[RETRY]{RESET} {msg}", flush=True)


def require_env(n):
    if n == "SIGNING_KEYSTORE_PASSWORD":
        return "7777777"
    if n == "SIGNING_KEY_ALIAS":
        return "RJ"
    if n == "SIGNING_KEY_PASSWORD":
        return "7777777"
    if n == "SIGNING_KEYSTORE_FILE":
        return "peachmeow-community.bks"
    
    v = os.environ.get(n)
    if not v:
        die(f"missing env: {n}")
    return v


def log_sub(title):
    log_space()
    print(f"{BLUE}[STEP]{RESET} {title}", flush=True)


def log_source(src):
    log_space()
    print(
        f"{PURPLE}\033[1m[SOURCE]\033[0m{RESET} → {PURPLE}\033[1;4m{src}\033[0m{RESET}",
        flush=True,
    )
    log_space()


def log_info(msg):
    print(f"{YELLOW}[INFO]{RESET} {msg}", flush=True)


def log_section(title):
    log_space()
    print(f"{AMBER}[BUILD]{RESET} → {AMBER}{title}{RESET}", flush=True)


def log_plain_section(title):
    width = 32
    title = title.strip()
    log_space()
    print(f"{NEUTRAL}{'=' * width}{RESET}", flush=True)
    centered = title.center(width)
    print(f"{AMBER}{centered}{RESET}", flush=True)
    print(f"{NEUTRAL}{'=' * width}{RESET}", flush=True)
    log_space()


def log_kv(key, value):
    print(f"{key}: {value}", flush=True)


def log_version_status(title, lines, status):
    if title in ["latest", "dev", "all"]:
        log_kv("Mode", title)

    if title == "all":
        stable = lines[:2]
        dev = lines[2:]

        log_space()

        print("  \033[4mLatest\033[0m", flush=True)
        for k, v in stable:
            print(
                f"  {k.replace('Stable ', '')}: {v if v is not None else '—'}",
                flush=True,
            )

        log_space()

        print("  \033[4mDev\033[0m", flush=True)
        for k, v in dev:
            print(
                f"  {k.replace('Dev ', '')}: {v if v is not None else '—'}", flush=True
            )

    else:
        log_space()
        for k, v in lines:
            print(f"  {k}: {v if v is not None else '—'}", flush=True)

    log_space()
    log_kv("Status", status)


def log_cache(msg):
    print(f"{PINK}[CACHE]{RESET} {msg}", flush=True)


def download_with_retry(url, output, retries=3):
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(retries):
        log_download(output)
        print(f"          ← {url}", flush=True)

        r = subprocess.run(
            [
                "curl",
                "-L",
                "--fail",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "10",
                "--max-time",
                "60",
                "--retry",
                "2",
                "--retry-delay",
                "1",
                "-o",
                output,
                url,
            ]
        )

        p = Path(output)

        if r.returncode == 0 and p.exists() and p.stat().st_size > 10_000:
            log_done(output)
            return 0

        if p.exists():
            p.unlink()

        log_retry(f"{output} (attempt {attempt+1}/{retries})")
        time.sleep(2**attempt)

    print(f"{RED}[ERROR]{RESET} Failed to download {output}", flush=True)
    return 1


def run(cmd):
    print(f"{SKY}[RUN]{RESET} {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        die(f"command failed ({r.returncode}): {' '.join(cmd)}")


def ensure_apk(p):
    r = subprocess.run(["file", p], capture_output=True, text=True)
    if "apk" not in r.stdout.lower():
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
            "https://github.com/", "https://raw.githubusercontent.com/"
        ).replace("/blob/", "/")
    return u


def get_latest_asset(assets: List[Dict]) -> Dict:
    if not assets:
        raise ValueError("No assets found")

    latest_asset = sorted(
        assets, key=lambda x: x.get("updated_at", "1970-01-01T00:00:00Z"), reverse=True
    )[0]

    return latest_asset
