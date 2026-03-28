import os
import json
import tomllib
import argparse
import requests
import shlex
import subprocess
from pathlib import Path
from packaging.version import Version
from utils import *

CONFIG_FILE = "config.toml"
VERSIONS_FILE = "versions.json"

ap = argparse.ArgumentParser()
ap.add_argument("--source")
ap.add_argument("--mode")
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()

BUILD_SOURCE = args.source
BUILD_MODE = args.mode
DRY = args.dry_run

SIGNING_KEYSTORE_PASSWORD = require_env("SIGNING_KEYSTORE_PASSWORD")
SIGNING_KEY_ALIAS = require_env("SIGNING_KEY_ALIAS")
SIGNING_KEY_PASSWORD = require_env("SIGNING_KEY_PASSWORD")
PEACHMEOW_GITHUB_PAT = require_env("PEACHMEOW_GITHUB_PAT")

OWNER = os.environ.get("GITHUB_REPOSITORY")
if not OWNER:
    die("GITHUB_REPOSITORY missing")

HEAD = {"Authorization": f"token {PEACHMEOW_GITHUB_PAT}"}

STATE_BRANCH = "state"
INIT_MSG = "state: initial 🐱 PeachMeow metadata"


def gh(url):
    r = requests.get(url, headers=HEAD, timeout=60)
    if r.status_code != 200:
        die(f"GitHub API failed: {url}")
    return r.json()


def cleanup_old_releases(active_brands, current_tag=None):

    r = subprocess.run(
        [
            "gh",
            "release",
            "list",
            "--limit",
            "200",
            "--json",
            "tagName,isPrerelease",
            "-q",
            ".[] | @json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    releases = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
    parsed = []

    for rel in releases:
        tag = rel.get("tagName")
        if not tag or "-v" not in tag:
            continue

        brand = tag.split("-v", 1)[0]
        version = tag.split("-v", 1)[1]

        try:
            vobj = Version(version)
        except:
            continue

        is_prerelease = rel.get("isPrerelease", False)
        parsed.append((tag, brand, vobj, is_prerelease))

    by_brand = {}
    for tag, brand, version, is_pre in parsed:
        by_brand.setdefault(brand, []).append((tag, version, is_pre))

    keep = set()

    for brand, items in by_brand.items():

        if brand not in active_brands:
            continue

        mode = global_patch_mode
        for app in apps.values():
            b = app.get("morphe-brand") or global_brand
            if b == brand:
                mode = app.get("patches-version") or global_patch_mode
                break

        stable = sorted([x for x in items if not x[2]], key=lambda x: x[1])
        prerelease = sorted([x for x in items if x[2]], key=lambda x: x[1])

        if mode == "dev":
            if prerelease:
                keep.add(prerelease[-1][0])

        elif mode == "all":
            for x in stable[-3:]:
                keep.add(x[0])
            if prerelease:
                keep.add(prerelease[-1][0])

        elif mode == "latest":
            for x in stable[-3:]:
                keep.add(x[0])

        else:
            target = mode.lstrip("v")
            for tag, version, _ in items:
                if str(version) == target:
                    keep.add(tag)

    if current_tag:
        keep.add(current_tag)

    for tag, brand, version, is_pre in parsed:

        if brand not in active_brands:
            subprocess.run(["gh", "release", "delete", tag, "-y"], check=False)
            subprocess.run(["git", "push", "origin", f":refs/tags/{tag}"], check=False)
            continue

        if tag not in keep:
            subprocess.run(["gh", "release", "delete", tag, "-y"], check=False)
            subprocess.run(["git", "push", "origin", f":refs/tags/{tag}"], check=False)


cfg = tomllib.loads(Path(CONFIG_FILE).read_text())

global_patches = cfg.get("patches-source") or "MorpheApp/morphe-patches"
global_cli = cfg.get("cli-source") or "MorpheApp/morphe-cli"
global_brand = cfg.get("morphe-brand") or "Morphe"
global_patch_mode = cfg.get("patches-version") or "latest"
global_cli_mode = cfg.get("cli-version") or "latest"
global_striplibs = ""
for t in shlex.split(cfg.get("patcher-args", "")):
    if t.startswith("--striplibs="):
        global_striplibs = t
        break

apps = {k: v for k, v in cfg.items() if isinstance(v, dict)}

source_order = []
for app in apps.values():
    src = app.get("patches-source") or global_patches
    if src not in source_order:
        source_order.append(src)


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

    mode = (
        "latest"
        if BUILD_MODE == "stable"
        else "dev" if BUILD_MODE == "pre-release" else BUILD_MODE
    ) or (app.get("patches-version") or global_patch_mode)
    PATCH_VERSION, IS_PRE = resolve(src, mode)

    used_patch_versions[src] = PATCH_VERSION

    cli_src = app.get("cli-source") or global_cli
    cli_mode = (
        "latest"
        if BUILD_MODE == "stable"
        else "dev" if BUILD_MODE == "pre-release" else BUILD_MODE
    ) or (app.get("cli-version") or global_cli_mode)

    version_key = f"{cli_src}@{cli_mode}"

    if version_key not in cli_version_cache:
        cli_version_cache[version_key] = resolve(cli_src, cli_mode)

    CLI_VERSION, _ = cli_version_cache[version_key]

    cli_key = f"{cli_src}@{CLI_VERSION}"

    if cli_key not in cli_cache:

        cli_rel = gh(
            f"https://api.github.com/repos/{cli_src}/releases/tags/v{CLI_VERSION}"
        )

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

        avail = []

        for x in rel:
            tag = x["tag_name"]

            if tag.startswith(f"{name}-"):
                avail.append(tag.replace(f"{name}-", ""))
                continue

            try:
                Version(tag)
                avail.append(tag)
            except:
                continue

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

    tag = f"{name}-{APP}"

    try:
        rel = gh(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    except:
        tag = APP
        rel = gh(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")

    APK = None
    APKM = None

    for a in rel.get("assets", []):
        if a["name"].endswith(".apk"):
            APK = a["browser_download_url"]
        if a["name"].endswith(".apkm"):
            APKM = a["browser_download_url"]

    if not APK and not APKM:
        die(table)

    out = f"temp/{name}.apk"

    if APK:
        if download_with_retry(APK, out) != 0:
            die(table)
    else:
        apkm_path = f"temp/{name}.apkm"
        if download_with_retry(APKM, apkm_path) != 0:
            die(table)

        run(
            [
                "java",
                "-jar",
                "tools/apkeditor.jar",
                "m",
                "-f",
                "-i",
                apkm_path,
                "-o",
                out,
            ]
        )

    ensure_apk(out)

    app_args = shlex.split(app.get("patcher-args", ""))
    strip_override = next((t for t in app_args if t.startswith("--striplibs=")), None)

    args_final = (
        [strip_override]
        if strip_override
        else ([global_striplibs] if global_striplibs else [])
    ) + [t for t in app_args if not t.startswith("--striplibs=")]

    run(
        [
            "java",
            "-jar",
            "tools/morphe-cli.jar",
            "patch",
            "--keystore",
            require_env("SIGNING_KEYSTORE_FILE"),
            "--keystore-password",
            SIGNING_KEYSTORE_PASSWORD,
            "--keystore-entry-alias",
            SIGNING_KEY_ALIAS,
            "--keystore-entry-password",
            SIGNING_KEY_PASSWORD,
            "-p",
            patch_file,
            "-o",
            f"build/{final}",
            "--purge",
            out,
        ]
        + args_final
    )

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

check = subprocess.run(
    ["gh", "release", "view", tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)

if check.returncode == 0:
    subprocess.run(["gh", "release", "delete", tag, "-y"], check=False)
    subprocess.run(["git", "push", "origin", f":refs/tags/{tag}"], check=False)
    subprocess.run(["git", "tag", "-d", tag], check=False, stderr=subprocess.DEVNULL)

cmd = ["gh", "release", "create", tag, "-t", release_name, "-F", "release.md"] + [
    f"build/{x}" for _, x, _, _ in built
]

if is_prerelease:
    cmd.append("--prerelease")

subprocess.run(cmd, check=True)

subprocess.run(["git", "fetch", "origin", STATE_BRANCH], check=False)

remote_check = subprocess.run(
    ["git", "ls-remote", "--heads", "origin", STATE_BRANCH],
    capture_output=True,
    text=True,
)

if remote_check.stdout.strip() == "":
    subprocess.run(["git", "checkout", "--orphan", STATE_BRANCH], check=True)
    subprocess.run(["git", "rm", "-rf", "."], check=False)
    subprocess.run(["git", "clean", "-fd"], check=False)

    if not Path(VERSIONS_FILE).exists():
        Path(VERSIONS_FILE).write_text("{}\n")

    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        check=True,
    )

    subprocess.run(["git", "add", VERSIONS_FILE], check=True)
    subprocess.run(["git", "commit", "-m", INIT_MSG], check=True)

    subprocess.run(["git", "push", "-u", "origin", STATE_BRANCH], check=True)
    subprocess.run(["git", "fetch", "origin", STATE_BRANCH], check=False)

if remote_check.stdout.strip() != "":
    subprocess.run(["git", "reset", "--hard"], check=True)
    subprocess.run(["git", "clean", "-fd"], check=True)
    subprocess.run(
        ["git", "checkout", "-B", STATE_BRANCH, f"origin/{STATE_BRANCH}"], check=True
    )

versions = {}
if Path(VERSIONS_FILE).exists():
    versions = json.loads(Path(VERSIONS_FILE).read_text())

entry = versions.setdefault(patch_src, {})

if is_prerelease:
    entry["dev"] = {"patch": patch_ver, "cli": CLI_VERSION}
else:
    entry["latest"] = {"patch": patch_ver, "cli": CLI_VERSION}

ordered_versions = {}

for src in source_order:
    if src in versions:
        ordered_versions[src] = versions[src]

for src in versions:
    if src not in ordered_versions:
        ordered_versions[src] = versions[src]

Path(VERSIONS_FILE).write_text(json.dumps(ordered_versions, indent=2))

subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
subprocess.run(
    [
        "git",
        "config",
        "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com",
    ],
    check=True,
)

subprocess.run(["git", "add", VERSIONS_FILE], check=True)

msg = f"release: {patch_src} → {patch_ver}"

r = subprocess.run(["git", "diff", "--cached", "--quiet"])
if r.returncode != 0:
    subprocess.run(["git", "commit", "-m", msg], check=True)

for _ in range(5):
    r = subprocess.run(["git", "pull", "--rebase", "origin", STATE_BRANCH])

    if r.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], check=False)
        subprocess.run(["git", "reset", "--hard", "origin/" + STATE_BRANCH])
        subprocess.run(["git", "add", VERSIONS_FILE], check=True)

        r = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if r.returncode != 0:
            subprocess.run(["git", "commit", "-m", msg], check=True)

    push = subprocess.run(["git", "push", "origin", STATE_BRANCH])

    if push.returncode == 0:
        break

active_brands = {a.get("morphe-brand") or global_brand for a in apps.values()}
cleanup_old_releases(active_brands, current_tag=tag)

print("[✓] Release complete")
