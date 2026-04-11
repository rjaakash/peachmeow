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
from zoneinfo import ZoneInfo
import getpass
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

log_plain_section("Build Start")
log_kv("Mode", BUILD_MODE)
log_kv("Source", BUILD_SOURCE or "all")
log_kv("Dry Run", DRY)

PEACHMEOW_GITHUB_PAT = os.environ.get("PEACHMEOW_GITHUB_PAT")

if not PEACHMEOW_GITHUB_PAT:
    log_space()
    PEACHMEOW_GITHUB_PAT = getpass.getpass("Enter your GitHub PAT or press Enter to skip: ").strip()
    log_space()

if PEACHMEOW_GITHUB_PAT:
    log_done("PAT accepted")
else:
    log_info("No PAT entered. Running unauthenticated (may hit rate limits)")

SIGNING_KEYSTORE_PASSWORD = require_env("SIGNING_KEYSTORE_PASSWORD")
SIGNING_KEY_ALIAS = require_env("SIGNING_KEY_ALIAS")
SIGNING_KEY_PASSWORD = require_env("SIGNING_KEY_PASSWORD")

HEAD = {"Authorization": f"token {PEACHMEOW_GITHUB_PAT}"} if PEACHMEOW_GITHUB_PAT else {}

STATE_BRANCH = "state"
INIT_MSG = "state: initial 🐱 PeachMeow metadata"


def gh(url):
    r = requests.get(url, headers=HEAD, timeout=60)
    if r.status_code != 200:
        die(f"GitHub API failed: {url}")
    return r.json()


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
        die(f"No releases found for {repo}")

    if mode == "latest":
        for r in rel:
            if not r["prerelease"]:
                return r["tag_name"], False
        die(f"No stable release found for {repo}")

    if mode == "dev":
        for r in rel:
            if r["prerelease"]:
                return r["tag_name"], True
        die(f"No prerelease found for {repo}")

    if mode == "all":
        r = rel[0]
        return r["tag_name"], r["prerelease"]

    for r in rel:
        if r["tag_name"] == mode:
            return r["tag_name"], r["prerelease"]

    die(f"Version '{mode}' not found for {repo}")


if BUILD_SOURCE:
    targets = {BUILD_SOURCE}
else:
    targets = {
        (a.get("patches-source") or global_patches)
        for a in apps.values()
        if a.get("enabled", True)
    }

if not DRY:
    mkdir_clean("unpatched", "tools", "patches", "build")

apkeditor = ""
for r in gh("https://api.github.com/repos/REAndroid/APKEditor/releases"):
    if not r["prerelease"]:
        for a in r["assets"]:
            if a["name"].lower().endswith(".jar"):
                apkeditor = a["browser_download_url"]
                break
        break

if apkeditor and not DRY:
    log_sub("APKEditor")
    if download_with_retry(apkeditor, "tools/apkeditor.jar") != 0:
        die("apkeditor download failed")

built = []
used_patch_versions = {}
seen_cli = set()
seen_patch = set()
seen_cli_files = {}
seen_patch_files = {}
seen_unpatched_apps = {}
pj_cache = {}

for table, app in apps.items():

    if app.get("enabled", True) is False:
        continue

    src = app.get("patches-source") or global_patches
    if src not in targets:
        continue

    log_section(table)

    mode = (
        "latest"
        if BUILD_MODE == "stable"
        else "dev" if BUILD_MODE == "pre-release" else BUILD_MODE
    ) or (app.get("patches-version") or global_patch_mode)

    PATCH_VERSION, IS_PRE = resolve(src, mode)
    used_patch_versions[src] = PATCH_VERSION

    log_sub("Resolved")
    log_kv("Patch", src)
    log_kv("Patch Version", PATCH_VERSION)

    cli_src = app.get("cli-source") or global_cli
    cli_mode = (
        "latest"
        if BUILD_MODE == "stable"
        else "dev" if BUILD_MODE == "pre-release" else BUILD_MODE
    ) or (app.get("cli-version") or global_cli_mode)

    CLI_VERSION, _ = resolve(cli_src, cli_mode)

    log_kv("CLI", cli_src)
    log_kv("CLI Version", CLI_VERSION)

    current_cli = (cli_src, CLI_VERSION)

    cli_owner, cli_repo = cli_src.split("/")
    cli_dir = f"tools/{cli_owner}/{cli_repo}"
    os.makedirs(cli_dir, exist_ok=True)

    if current_cli not in seen_cli:

        cli_rel = gh(
            f"https://api.github.com/repos/{cli_src}/releases/tags/{CLI_VERSION}"
        )
        candidates = [
            a for a in cli_rel.get("assets", []) if a["name"].endswith(".jar")
        ]

        if not candidates:
            die(f"cli jar not found for {CLI_VERSION}")

        selected = get_latest_asset(candidates)

        CLI_URL = selected["browser_download_url"]
        CLI_FILENAME = selected["name"]

        log_sub("CLI")
        log_kv("CLI File", CLI_FILENAME)

        cli_file = f"{cli_dir}/{CLI_FILENAME}"

        if not DRY:
            if download_with_retry(CLI_URL, cli_file) != 0:
                die("CLI download failed")

        seen_cli.add(current_cli)
        seen_cli_files[current_cli] = CLI_FILENAME
    else:
        CLI_FILENAME = seen_cli_files[current_cli]
        cli_file = f"{cli_dir}/{CLI_FILENAME}"
        log_cache(f"CLI: {CLI_FILENAME}")

    patch_owner, patch_repo = src.split("/")
    patch_dir = f"patches/{patch_owner}/{patch_repo}"
    os.makedirs(patch_dir, exist_ok=True)

    current_patch = (src, PATCH_VERSION)

    if current_patch not in seen_patch:

        patch_rel = gh(
            f"https://api.github.com/repos/{src}/releases/tags/{PATCH_VERSION}"
        )

        candidates = [
            a for a in patch_rel.get("assets", []) if a["name"].endswith(".mpp")
        ]

        if not candidates:
            die(f"patch file not found for {PATCH_VERSION}")

        selected = get_latest_asset(candidates)

        PATCH_URL = selected["browser_download_url"]
        PATCH_FILENAME = selected["name"]

        log_sub("Patches")
        log_kv("Patch File", PATCH_FILENAME)

        patch_file = f"{patch_dir}/{PATCH_FILENAME}"

        if not DRY:
            if download_with_retry(PATCH_URL, patch_file) != 0:
                die("patch download failed")

        seen_patch.add(current_patch)
        seen_patch_files[current_patch] = PATCH_FILENAME
    else:
        PATCH_FILENAME = seen_patch_files[current_patch]
        patch_file = f"{patch_dir}/{PATCH_FILENAME}"
        log_cache(f"Patch: {PATCH_FILENAME}")

    pkg = app.get("package-name") or die(table)
    repo = app.get("app-source") or die(table)
    brand = app.get("morphe-brand") or global_brand
    name = app.get("app-name") or table
    variant = app.get("variant")
    vm_raw = app.get("version")
    vm = vm_raw or "auto"

    if app.get("patches-list"):
        plist = gh_blob_to_raw(app.get("patches-list"))
    else:
        branch = "dev" if IS_PRE else "main"
        plist = f"https://raw.githubusercontent.com/{src}/{branch}/patches-list.json"

    if vm == "auto":
        if plist in pj_cache:
            pj = pj_cache[plist]
            log_cache(f"Patches-list: {plist}")
        else:
            pj = requests.get(plist, timeout=60).json()
            pj_cache[plist] = pj

        compat = set()

        for p in pj.get("patches", []):
            cp = p.get("compatiblePackages")

            if not isinstance(cp, dict):
                continue

            if pkg in cp:
                versions = cp[pkg]

                if versions:
                    compat |= set(versions)

        rel = gh(f"https://api.github.com/repos/{repo}/releases?per_page=100")

        avail = []

        for x in rel:
            tag = x["tag_name"]

            if not tag.startswith(f"{name}-"):
                continue

            ver = tag[len(name) + 1 :]

            if ver.startswith("v"):
                continue

            try:
                Version(ver)
                avail.append(ver)
            except:
                continue

        if compat:
            cand = sorted(set(compat) & set(avail), key=Version)

            if not cand:
                die(f"{table}: no compatible versions found")
        else:
            cand = sorted(avail, key=Version)

        if not cand:
            die(f"{table}: no versions available")

        APP = cand[-1]
    else:
        APP = vm

    if vm_raw == "🐱":
        rels = gh(f"https://api.github.com/repos/{repo}/releases?per_page=100")
        rel = next((r for r in rels if not r["prerelease"]), None)

        if not rel:
            die(f"No 🐱 found for {repo}")

        APP = rel["tag_name"]

    elif vm == "auto":
        tag = f"{name}-{APP}"
        rel = gh(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")

    else:
        tag = APP
        rel = gh(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")

    parts = [name]

    norm = APP.strip()

    for p in (name, name.replace(" ", "-"), name.replace("-", " ")):
        if norm.startswith(f"{p}-"):
            norm = norm[len(p) + 1 :]
            break

    if norm.startswith("v"):
        norm = norm[1:]

    parts.append(f"v{norm}")

    parts.append(brand)

    if variant:
        parts.append(variant)

    parts.append(f"{PATCH_VERSION}")

    final = "-".join(parts) + ".apk"

    log_space()
    log_info(f"Output: {final}")

    if DRY:
        built.append((name, final, APP, variant))
        continue

    APK = None
    APKM = None

    apk_assets = [a for a in rel.get("assets", []) if a["name"].endswith(".apk")]
    apkm_assets = [a for a in rel.get("assets", []) if a["name"].endswith(".apkm")]

    if apk_assets:
        APK = get_latest_asset(apk_assets)["browser_download_url"]
    elif apkm_assets:
        APKM = get_latest_asset(apkm_assets)["browser_download_url"]

    if not APK and not APKM:
        die(table)

    log_sub("App")
    log_kv("Package", pkg)
    log_kv("App Version", APP)

    app_download_source = APK or APKM
    app_filename = os.path.basename(app_download_source)

    pkg_dir = f"unpatched/{pkg}"
    os.makedirs(pkg_dir, exist_ok=True)

    key = f"{pkg}/{app_filename}"

    if key in seen_unpatched_apps:
        out = seen_unpatched_apps[key]
        log_cache(f"App: {pkg}/{app_filename}")
    else:
        file_path = f"{pkg_dir}/{app_filename}"

        if download_with_retry(app_download_source, file_path) != 0:
            die(table)

        if app_filename.endswith(".apkm"):
            out = f"{pkg_dir}/{app_filename[:-5]}.apk"

            log_sub("Merging")

            run(
                [
                    "java",
                    "-jar",
                    "tools/apkeditor.jar",
                    "m",
                    "-f",
                    "-i",
                    file_path,
                    "-o",
                    out,
                ]
            )

            os.remove(file_path)
        else:
            out = file_path

        seen_unpatched_apps[key] = out

    ensure_apk(out)

    app_args = shlex.split(app.get("patcher-args", ""))
    strip_override = next((t for t in app_args if t.startswith("--striplibs=")), None)

    args_final = (
        [strip_override]
        if strip_override
        else ([global_striplibs] if global_striplibs else [])
    ) + [t for t in app_args if not t.startswith("--striplibs=")]

    log_sub("Patching")

    run(
        [
            "java",
            "-jar",
            cli_file,
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
    log_done("Dry run complete")
    exit(0)

if not built:
    die("Nothing built")

patch_src = list(used_patch_versions.keys())[0]
patch_ver = list(used_patch_versions.values())[0]

rel = gh(f"https://api.github.com/repos/{patch_src}/releases/tags/{patch_ver}")
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

now = datetime.now(ZoneInfo("Asia/Kolkata"))

tag = "peachmeow-" + now.strftime("%Y%m%d-%H%M%S-%f")

release_name = f"🐱 PeachMeow · {now.strftime('%Y-%m-%d')} · {now.strftime('%H:%M')}"

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

log_plain_section("Build Complete")
log_done("Release created")
