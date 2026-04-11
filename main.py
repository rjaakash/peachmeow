import os
from utils import *

BUILD_SOURCE, BUILD_MODE, DRY, HEAD = build_init()

SIGNING_KEYSTORE_PASSWORD = require_env("SIGNING_KEYSTORE_PASSWORD")
SIGNING_KEY_ALIAS = require_env("SIGNING_KEY_ALIAS")
SIGNING_KEY_PASSWORD = require_env("SIGNING_KEY_PASSWORD")

cfg = load_config()

global_patches = cfg.get("patches-source") or "MorpheApp/morphe-patches"
global_cli = cfg.get("cli-source") or "MorpheApp/morphe-cli"
global_brand = cfg.get("morphe-brand") or "Morphe"
global_patch_mode = cfg.get("patches-version") or "latest"
global_cli_mode = cfg.get("cli-version") or "latest"
global_striplibs = parse_global_striplibs(cfg)

apps = {k: v for k, v in cfg.items() if isinstance(v, dict)}

source_order = []
for app in apps.values():
    src = app.get("patches-source") or global_patches
    if src not in source_order:
        source_order.append(src)

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

apkeditor_url = get_apkeditor_url(HEAD)
if apkeditor_url and not DRY:
    log_sub("APKEditor")
    if download_with_retry(apkeditor_url, "tools/apkeditor.jar") != 0:
        die("apkeditor download failed")

built = []
used_patch_versions = {}
seen_cli = set()
seen_patch = set()
seen_cli_files = {}
seen_patch_files = {}
seen_unpatched_apps = {}
pj_cache = {}
CLI_VERSION = None

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

    PATCH_VERSION, IS_PRE = resolve(src, mode, headers=HEAD, strict=True)
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

    CLI_VERSION, _ = resolve(cli_src, cli_mode, headers=HEAD, strict=True)

    log_kv("CLI", cli_src)
    log_kv("CLI Version", CLI_VERSION)

    cli_owner, cli_repo_name = cli_src.split("/")
    cli_dir = f"tools/{cli_owner}/{cli_repo_name}"
    os.makedirs(cli_dir, exist_ok=True)

    cli_file, _ = download_tool(
        "CLI",
        cli_src,
        CLI_VERSION,
        ".jar",
        cli_dir,
        seen_cli,
        seen_cli_files,
        DRY,
        HEAD,
    )

    patch_owner, patch_repo_name = src.split("/")
    patch_dir = f"patches/{patch_owner}/{patch_repo_name}"
    os.makedirs(patch_dir, exist_ok=True)

    patch_file, _ = download_tool(
        "Patches",
        src,
        PATCH_VERSION,
        ".mpp",
        patch_dir,
        seen_patch,
        seen_patch_files,
        DRY,
        HEAD,
    )

    pkg = app.get("package-name") or die(table)
    repo = app.get("app-source") or die(table)
    brand = app.get("morphe-brand") or global_brand
    name = app.get("app-name") or table
    variant = app.get("variant")
    vm_raw = app.get("version")
    vm = vm_raw or "auto"

    if vm == "auto":
        pj, _ = fetch_patches_list(src, IS_PRE, app, pj_cache)
        APP = resolve_app_version(table, pkg, name, repo, pj, HEAD)
        tag = f"{name}-{APP}"
        rel = gh(
            f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=HEAD
        )

    elif vm_raw == "🐱":
        rels = gh(
            f"https://api.github.com/repos/{repo}/releases?per_page=100", headers=HEAD
        )
        rel = next((r for r in rels if not r["prerelease"]), None)
        if not rel:
            die(f"No 🐱 found for {repo}")
        APP = rel["tag_name"]

    else:
        APP = vm
        rel = gh(
            f"https://api.github.com/repos/{repo}/releases/tags/{APP}", headers=HEAD
        )

    final = build_output_filename(name, APP, brand, variant, PATCH_VERSION)

    log_space()
    log_info(f"Output: {final}")

    if DRY:
        built.append((name, final, APP, variant))
        continue

    log_sub("App")
    log_kv("Package", pkg)
    log_kv("App Version", APP)

    out = fetch_and_merge_apk(pkg, rel, seen_unpatched_apps, table)

    ensure_apk(out)

    patch_app(cli_file, patch_file, out, final, app, global_striplibs)

    built.append((name, final, APP, variant))

if DRY:
    log_done("Dry run complete")
    exit(0)

if not built:
    die("Nothing built")

release_notes, patch_ver, is_prerelease = build_release_notes(
    built, used_patch_versions, CLI_VERSION, HEAD
)

publish_release(built, release_notes, is_prerelease)

update_state(used_patch_versions, CLI_VERSION, source_order, is_prerelease)

log_plain_section("Build Complete")
log_done("Release created")
