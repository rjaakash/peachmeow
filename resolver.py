import os
import json
import tomllib
import requests
import subprocess
from pathlib import Path

CONFIG_FILE = "config.toml"
VERSIONS_FILE = "versions.json"

PUBLISH_PAT = os.environ.get("PUBLISH_PAT")

HEADERS = {}
if PUBLISH_PAT:
    HEADERS["Authorization"] = f"token {PUBLISH_PAT}"

def die(m):
    print(m, flush=True)
    raise SystemExit(1)

def load_config():
    if not Path(CONFIG_FILE).exists():
        die("config.toml missing")
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)

def load_versions():
    if not Path(VERSIONS_FILE).exists():
        return {}
    return json.loads(Path(VERSIONS_FILE).read_text())

def resolve(repo, mode):
    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases",
        headers=HEADERS,
        timeout=60
    )
    if r.status_code != 200:
        die(f"Failed to fetch {repo}")

    rel = r.json()

    if not rel:
        return None

    if mode == "latest":
        for x in rel:
            if not x["prerelease"]:
                return x["tag_name"].lstrip("v")

    if mode in ("dev", "all"):
        return rel[0]["tag_name"].lstrip("v")

    return mode

def trigger(src):
    print(f"[+] Trigger build: {src}")
    subprocess.run(
        ["gh", "workflow", "run", "build.yml", "-f", f"source={src}"],
        check=True
    )

def main():
    print("[+] Resolver started")

    cfg = load_config()
    old = load_versions()

    global_patches = cfg.get("patches-source") or "MorpheApp/morphe-patches"
    global_mode = cfg.get("patches-version") or "latest"

    apps = {k: v for k, v in cfg.items() if isinstance(v, dict)}

    sources = {}

    for app in apps.values():
        if app.get("enabled", True) is False:
            continue

        src = app.get("patches-source") or global_patches
        mode = app.get("patches-version") or global_mode

        sources[src] = mode

    changed = []

    for src, mode in sources.items():
        latest = resolve(src, mode)
        stored = old.get(src, {})

        prev_version = stored.get("version")

        print(src)
        print("  latest :", latest)
        print("  stored :", prev_version)

        # ONLY patch version matters (mode ignored)
        if latest and latest != prev_version:
            changed.append(src)

    if not changed:
        print("[✓] No patch updates")
        return

    for s in changed:
        trigger(s)

    print("[✓] Resolver done")

if __name__ == "__main__":
    main()