import os
import json
import tomllib
import argparse
import requests
import shlex
import subprocess
from pathlib import Path
from packaging.version import Version
from datetime import datetime
from utils import *

CONFIG_FILE = "config.toml"
VERSIONS_FILE = "versions.json"

ap = argparse.ArgumentParser()
ap.add_argument("--source")
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()

BUILD_SOURCE = args.source
DRY = args.dry_run

SIGNING_KEYSTORE_PASSWORD = require_env("SIGNING_KEYSTORE_PASSWORD")
SIGNING_KEY_ALIAS = require_env("SIGNING_KEY_ALIAS")
SIGNING_KEY_PASSWORD = require_env("SIGNING_KEY_PASSWORD")
PEACHMEOW_GITHUB_PAT = require_env("PEACHMEOW_GITHUB_PAT")

OWNER = os.environ.get("GITHUB_REPOSITORY")
if not OWNER:
    die("GITHUB_REPOSITORY missing")

HEAD = {"Authorization": f"token {PEACHMEOW_GITHUB_PAT}"}

def gh(url):
    r = requests.get(url, headers=HEAD, timeout=60)
    if r.status_code != 200:
        die(f"GitHub API failed: {url}")
    return r.json()

def cleanup_old_releases(active_brands, keep_tag):
    r = subprocess.run(
        [
            "gh","release","list",
            "--limit","200",
            "--json","tagName,isPrerelease,createdAt",
            "-q",".[] | [.tagName, .isPrerelease, .createdAt] | @tsv"
        ],
        capture_output=True,
        text=True,
        check=True
    )

    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        tag, is_pre, created_at = parts
        if "-v" not in tag:
            continue
        rows.append((tag, is_pre == "true", created_at))

    if not rows:
        return

    parsed = []
    for tag, is_pre, created_at in rows:
        brand = tag.split("-v", 1)[0]
        version = tag.split("-v", 1)[1]
        try:
            vobj = Version(version)
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            parsed.append((tag, brand, vobj, dt, is_pre))
        except:
            continue

    parsed_by_version = sorted(parsed, key=lambda x: x[2], reverse=True)

    brands = {}
    for tag, brand, version, dt, is_pre in parsed_by_version:
        brands.setdefault(brand, []).append((tag, version, dt, is_pre))

    for brand in list(brands.keys()):
        if brand not in active_brands:
            for tag, _, _, _ in brands[brand]:
                subprocess.run(["gh", "release", "delete", tag, "-y"], check=False)
                subprocess.run(["git", "push", "origin", f":refs/tags/{tag}"], check=False)
            del brands[brand]

    brand_count = len(brands)
    keep = set()

    for brand, items in brands.items():
        stable = [x for x in items if not x[3]]
        dev = [x for x in items if x[3]]
        if stable:
            keep.add(stable[0][0])
        if dev:
            keep.add(dev[0][0])

    if brand_count <= 5:
        target = 10
        parsed_by_date = sorted(parsed, key=lambda x: x[3], reverse=True)
        for tag, brand, version, dt, is_pre in parsed_by_date:
            if len(keep) >= target:
                break
            if brand in brands and tag not in keep:
                keep.add(tag)

    for tag, brand, version, dt, is_pre in parsed:
        if brand not in brands:
            continue
        if tag in keep:
            continue
        subprocess.run(["gh", "release", "delete", tag, "-y"], check=False)
        subprocess.run(["git", "push", "origin", f":refs/tags/{tag}"], check=False)

cfg = tomllib.loads(Path(CONFIG_FILE).read_text())

global_patches = cfg.get("patches-source") or "MorpheApp/morphe-patches"
global_cli = cfg.get("cli-source") or "MorpheApp/morphe-cli"
global_brand = cfg.get("morphe-brand") or "Morphe"
global_patch_mode = cfg.get("patches-version") or "latest"
global_cli_mode = cfg.get("cli-version") or "latest"

apps = {k: v for k, v in cfg.items() if isinstance(v, dict)}

def resolve(repo, mode):
    rel = gh(f"https://api.github.com/repos/{repo}/releases")
    if not rel:
        die(repo)

    if mode == "latest":
        for r in rel:
            if not r["prerelease"]:
                return r["tag_name"].lstrip("v"), False

    if mode == "dev":
        for r in rel:
            if r["prerelease"]:
                return r["tag_name"].lstrip("v"), True
        die(f"No prerelease found for {repo}")

    if mode == "all":
        return rel[0]["tag_name"].lstrip("v"), rel[0]["prerelease"]

    tag = mode.lstrip("v")
    for r in rel:
        if r["tag_name"].lstrip("v") == tag:
            return tag, r["prerelease"]

    return tag, False

if BUILD_SOURCE:
    targets = {BUILD_SOURCE}
else:
    targets = {
        (a.get("patches-source") or global_patches)
        for a in apps.values()
        if a.get("enabled", True)
    }

if not DRY:
    mkdir_clean("temp", "tools", "patches", "build")

apkeditor = ""
for r in gh("https://api.github.com/repos/REAndroid/APKEditor/releases"):
    if not r["prerelease"]:
        for a in r["assets"]:
            if a["name"].lower().endswith(".jar"):
                apkeditor = a["browser_download_url"]
                break
        break

if apkeditor and not DRY:
    if download_with_retry(apkeditor, "tools/apkeditor.jar") != 0:
        die("apkeditor download failed")

built = []
used_patch_versions = {}
release_brand = global_brand
cli_cache = {}
cli_version_cache = {}

for table, app in apps.items():

    if app.get("enabled", True) is False:
        continue

    src = app.get("patches-source") or global_patches
    if src not in targets:
        continue

    mode = app.get("patches-version") or global_patch_mode
    PATCH_VERSION, IS_PRE = resolve(src, mode)

    used_patch_versions[src] = PATCH_VERSION

    cli_src = app.get("cli-source") or global_cli
    cli_mode = app.get("cli-version") or global_cli_mode

    version_key = f"{cli_src}@{cli_mode}"

    if version_key not in cli_version_cache:
        cli_version_cache[version_key] = resolve(cli_src, cli_mode)

    CLI_VERSION, _ = cli_version_cache[version_key]

    cli_key = f"{cli_src}@{CLI_VERSION}"

    if cli_key not in cli_cache:

        cli_rel = gh(f"https://api.github.com/repos/{cli_src}/releases/tags/v{CLI_VERSION}")

        CLI_URL = None
        for a in cli_rel.get("assets", []):
            n = a["name"].lower()
            if n.startswith("morphe-cli") and n.endswith("-all.jar"):
                CLI_URL = a["browser_download_url"]
                break

        if not CLI_URL:
            die(f"morphe-cli all.jar not found for v{CLI_VERSION}")

        if not DRY:
            if download_with_retry(CLI_URL, "tools/morphe-cli.jar") != 0:
                die("CLI download failed")

        cli_cache[cli_key] = True

    patch_file = f"patches/{src.split('/')[-1]}-{PATCH_VERSION}.mpp"
    PATCH_URL = f"https://github.com/{src}/releases/download/v{PATCH_VERSION}/patches-{PATCH_VERSION}.mpp"

    if not DRY:
        if download_with_retry(PATCH_URL, patch_file) != 0:
            die("patch download failed")

    pkg = app.get("package-name") or die(table)
    repo = app.get("app-source") or die(table)
    brand = app.get("morphe-brand") or global_brand
    release_brand = brand
    name = app.get("app-name") or table
    variant = app.get("variant")
    vm = app.get("version") or "auto"

    if app.get("patches-list"):
        plist = gh_blob_to_raw(app.get("patches-list"))
    else:
        branch = "dev" if IS_PRE else "main"
        plist = f"https://raw.githubusercontent.com/{src}/{branch}/patches-list.json"

    if vm == "auto":
        pj = requests.get(plist, timeout=60).json()

        compat = set()
        wildcard = False

        for p in pj.get("patches", []):
            cp = p.get("compatiblePackages") or {}
            if pkg in cp:
                if cp[pkg] is None:
                    wildcard = True
                    break
                compat |= set(cp[pkg] or [])

        rel = gh(f"https://api.github.com/repos/{repo}/releases?per_page=100")

        avail = [
            x["tag_name"].replace(f"{name}-", "")
            for x in rel
            if x["tag_name"].startswith(f"{name}-")
        ]

        cand = sorted(avail if wildcard else set(compat) & set(avail), key=Version)

        if not cand:
            die(table)

        APP = cand[-1]
    else:
        APP = vm

    parts = [name]

    if vm != "auto":
        parts.append(f"v{APP}")

    parts.append(brand)

    if variant:
        parts.append(variant)

    parts.append(f"v{PATCH_VERSION}")

    final = "-".join(parts) + ".apk"

    print("Build:", final)

    if DRY:
        continue

    APK = f"https://github.com/{repo}/releases/download/{name}-{APP}/{name}-{APP}.apk"
    APKM = f"https://github.com/{repo}/releases/download/{name}-{APP}/{name}-{APP}.apkm"

    out = f"temp/{name}.apk"

    if download_with_retry(APK, out) != 0:
        if download_with_retry(APKM, f"temp/{name}.apkm") == 0:
            run(["java","-jar","tools/apkeditor.jar","m","-f","-i",f"temp/{name}.apkm","-o",out])
        else:
            die(table)

    ensure_apk(out)

    run([
        "java","-jar","tools/morphe-cli.jar","patch",
        "--keystore","morphe-release.bks",
        "--keystore-password",SIGNING_KEYSTORE_PASSWORD,
        "--keystore-entry-alias",SIGNING_KEY_ALIAS,
        "--keystore-entry-password",SIGNING_KEY_PASSWORD,
        "-p",patch_file,
        "-o",f"build/{final}",
        "--purge",
        out
    ] + shlex.split(app.get("patcher-args", "")))

    built.append((name, final, APP, variant))

if DRY:
    print("[✓] Dry run complete")
    exit(0)

if not built:
    die("Nothing built")

patch_src = list(used_patch_versions.keys())[0]
patch_ver = list(used_patch_versions.values())[0]

rel = gh(f"https://api.github.com/repos/{patch_src}/releases/tags/v{patch_ver}")
changelog = rel.get("body") or ""
is_prerelease = rel.get("prerelease", False)

lines = []

grouped = {}
for table, _, appv, variant in built:
    grouped.setdefault(table, []).append((variant, appv))

has_variants = any(
    len(items) > 1 or (len(items) == 1 and items[0][0] is not None)
    for items in grouped.values()
)

priority = ["youtube", "music"]

def app_sort_key(app):
    if app.lower() in priority:
        return (0, priority.index(app.lower()))
    return (1, app.lower())

if not has_variants:

    lines.append("## App Versions\n")

    for app in sorted(grouped.keys(), key=app_sort_key):
        variant, appv = grouped[app][0]
        lines.append(f"{app.replace('-', ' ')}: {appv}")

    lines.append("")

else:

    lines.append("## App Versions\n")

    for app in sorted(grouped.keys(), key=app_sort_key):

        lines.append(f"### {app.replace('-', ' ')}")

        items = grouped[app]

        def variant_sort_key(item):
            variant, _ = item
            if variant is None:
                return (0, "")
            return (1, variant.lower())

        for variant, appv in sorted(items, key=variant_sort_key):

            if len(items) == 1 and variant is None:
                lines.append(f"- {appv}")
            else:
                if variant is None:
                    lines.append(f"- Base: {appv}")
                else:
                    label = variant.replace("-", " ")
                    lines.append(f"- {label}: {appv}")

        lines.append("")

lines.append("## Build Info\n")
lines.append(f"- Patch: {patch_ver}")
lines.append(f"- CLI: {CLI_VERSION}")
lines.append("")

lines.append("## Patch Changelog\n")
lines.append(changelog)

Path("release.md").write_text("\n".join(lines))

tag = f"{release_brand}-v{patch_ver}"
release_name = f"{release_brand.replace('-', ' ')} 🐱 PeachMeow v{patch_ver}"

cmd = ["gh","release","create",tag,"-t",release_name,"-F","release.md"] + [f"build/{x}" for _, x,_,_ in built]

if is_prerelease:
    cmd.append("--prerelease")

subprocess.run(cmd, check=True)

versions = {}
if Path(VERSIONS_FILE).exists():
    versions = json.loads(Path(VERSIONS_FILE).read_text())

versions[patch_src] = {"version": patch_ver, "cli": CLI_VERSION}

Path(VERSIONS_FILE).write_text(json.dumps(versions, indent=2))

subprocess.run(["git","config","user.name","github-actions[bot]"],check=True)
subprocess.run(["git","config","user.email","41898282+github-actions[bot]@users.noreply.github.com"],check=True)
subprocess.run(["git","add",VERSIONS_FILE],check=True)

msg = f"chore: {patch_src} → {patch_ver}"

subprocess.run(["git","commit","-m",msg],check=True)
subprocess.run(["git","pull","--rebase"],check=True)
subprocess.run(["git","push"],check=True)

active_brands = {a.get("morphe-brand") or global_brand for a in apps.values() if a.get("enabled", True)}
cleanup_old_releases(active_brands, tag)

print("[✓] Release complete")
