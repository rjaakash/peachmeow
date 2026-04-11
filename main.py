import os
from utils import *

cli_target_source, cli_build_mode, dry_run, auth_headers = build_init()

SIGNING_KEYSTORE_PASSWORD = require_env("SIGNING_KEYSTORE_PASSWORD")
SIGNING_KEY_ALIAS = require_env("SIGNING_KEY_ALIAS")
SIGNING_KEY_PASSWORD = require_env("SIGNING_KEY_PASSWORD")

config = load_config()

default_patches_repo = config.get("patches-source") or "MorpheApp/morphe-patches"
default_cli_repo = config.get("cli-source") or "MorpheApp/morphe-cli"
default_brand = config.get("morphe-brand") or "Morphe"
default_patches_mode = config.get("patches-version") or "latest"
default_cli_mode = config.get("cli-version") or "latest"
default_striplibs_arg = parse_global_striplibs(config)

app_entries = {k: v for k, v in config.items() if isinstance(v, dict)}

patches_source_order = []
for app_entry in app_entries.values():
    patches_repo = app_entry.get("patches-source") or default_patches_repo
    if patches_repo not in patches_source_order:
        patches_source_order.append(patches_repo)

if cli_target_source:
    target_patch_sources = {cli_target_source}
else:
    target_patch_sources = {
        (app_entry.get("patches-source") or default_patches_repo)
        for app_entry in app_entries.values()
        if app_entry.get("enabled", True)
    }

if not dry_run:
    mkdir_clean("unpatched", "tools", "patches", "build")

apkeditor_download_url = get_apkeditor_url(auth_headers)
if apkeditor_download_url and not dry_run:
    log_sub("APKEditor")
    if download_with_retry(apkeditor_download_url, "tools/apkeditor.jar") != 0:
        die("apkeditor download failed")

built_apps = []
used_patches_versions = {}
seen_cli_cache = set()
seen_patches_cache = set()
seen_cli_filenames = {}
seen_patches_filenames = {}
downloaded_apks_cache = {}
patches_url_cache = {}
cli_version = None
cli_repo = None

for app_table_name, app_entry in app_entries.items():

    if app_entry.get("enabled", True) is False:
        continue

    patches_repo = app_entry.get("patches-source") or default_patches_repo
    if patches_repo not in target_patch_sources:
        continue

    log_section(app_table_name)

    patches_mode = resolve_build_mode(
        cli_build_mode, app_entry, "patches-version", default_patches_mode
    )

    patches_version, is_prerelease = resolve(
        patches_repo, patches_mode, headers=auth_headers, strict=True
    )
    used_patches_versions[patches_repo] = patches_version

    log_sub("Resolved")
    log_kv("Patches", patches_repo)
    log_kv("Patches Version", patches_version)

    cli_repo = app_entry.get("cli-source") or default_cli_repo
    cli_resolve_mode = resolve_build_mode(
        cli_build_mode, app_entry, "cli-version", default_cli_mode
    )

    cli_version, _ = resolve(
        cli_repo, cli_resolve_mode, headers=auth_headers, strict=True
    )

    log_kv("CLI", cli_repo)
    log_kv("CLI Version", cli_version)

    cli_owner, cli_repo_name = cli_repo.split("/")
    cli_dir = f"tools/{cli_owner}/{cli_repo_name}"
    os.makedirs(cli_dir, exist_ok=True)

    cli_file, _ = download_tool(
        "CLI",
        cli_repo,
        cli_version,
        ".jar",
        cli_dir,
        seen_cli_cache,
        seen_cli_filenames,
        dry_run,
        auth_headers,
    )

    patches_owner, patches_repo_name = patches_repo.split("/")
    patches_dir = f"patches/{patches_owner}/{patches_repo_name}"
    os.makedirs(patches_dir, exist_ok=True)

    patches_file, _ = download_tool(
        "Patches",
        patches_repo,
        patches_version,
        ".mpp",
        patches_dir,
        seen_patches_cache,
        seen_patches_filenames,
        dry_run,
        auth_headers,
    )

    package_name = app_entry.get("package-name") or die(app_table_name)
    app_repo = app_entry.get("app-source") or die(app_table_name)
    brand = app_entry.get("morphe-brand") or default_brand
    app_display_name = app_entry.get("app-name") or app_table_name
    variant = app_entry.get("variant")
    configured_app_version = app_entry.get("app-version")
    app_version_mode = configured_app_version or "auto"

    if app_version_mode == "auto":
        patches_json, _ = fetch_patches_list(
            patches_repo, is_prerelease, app_entry, patches_url_cache
        )
        resolved_app_version = resolve_app_version(
            app_table_name,
            package_name,
            app_display_name,
            app_repo,
            patches_json,
            auth_headers,
        )
        release_tag = f"{app_display_name}-{resolved_app_version}"
        app_release = gh(
            f"https://api.github.com/repos/{app_repo}/releases/tags/{release_tag}",
            headers=auth_headers,
        )

    elif configured_app_version == "🐱":
        all_app_releases = gh(
            f"https://api.github.com/repos/{app_repo}/releases?per_page=100",
            headers=auth_headers,
        )
        app_release = next(
            (
                release_entry
                for release_entry in all_app_releases
                if not release_entry["prerelease"]
            ),
            None,
        )
        if not app_release:
            die(f"No 🐱 found for {app_repo}")
        resolved_app_version = app_release["tag_name"]

    else:
        resolved_app_version = app_version_mode
        app_release = gh(
            f"https://api.github.com/repos/{app_repo}/releases/tags/{resolved_app_version}",
            headers=auth_headers,
        )

    output_filename = build_output_filename(
        app_display_name, resolved_app_version, brand, variant, patches_version
    )

    log_space()
    log_info(f"Output: {output_filename}")

    if dry_run:
        built_apps.append(
            (
                app_table_name,
                output_filename,
                resolved_app_version,
                variant,
                app_display_name,
            )
        )
        continue

    log_sub("App")
    log_kv("Package", package_name)
    log_kv("App Version", resolved_app_version)

    input_apk = fetch_and_merge_apk(
        package_name, app_release, downloaded_apks_cache, app_table_name
    )

    ensure_apk(input_apk)

    patch_app(
        cli_file,
        patches_file,
        input_apk,
        output_filename,
        app_entry,
        default_striplibs_arg,
    )

    built_apps.append(
        (
            app_table_name,
            output_filename,
            resolved_app_version,
            variant,
            app_display_name,
        )
    )

if dry_run:
    log_done("Dry run complete")
    exit(0)

if not built_apps:
    die("Nothing built")

release_notes, patches_version, is_prerelease = build_release_notes(
    built_apps, used_patches_versions, cli_version, cli_repo, auth_headers
)

publish_release(built_apps, release_notes, is_prerelease)

update_state(used_patches_versions, cli_version, patches_source_order, is_prerelease)

log_plain_section("Build Complete")
log_done("Release created")
