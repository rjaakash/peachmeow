import os
import re
import sys
import json
import shlex
import shutil
import subprocess
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from packaging.version import Version
from datetime import datetime
from zoneinfo import ZoneInfo
from utils import (
    die,
    log_space,
    log_done,
    log_sub,
    log_source,
    log_info,
    log_section,
    log_plain_section,
    log_kv,
    log_version_status,
    log_version_status_channels,
    log_cache,
    is_ci_environment,
    require_env,
    read_github_pat_from_env,
    build_auth_headers,
    init,
    load_config,
    load_versions,
    extract_app_entries,
    get_config_defaults,
    parse_global_striplibs,
    strip_v,
    ensure_v,
    resolve_build_mode,
    gh,
    resolve,
    resolve_channels,
    get_latest_asset,
    gh_blob_to_raw,
    run,
    ensure_apk,
    mkdir_clean,
    mkdir_ensure,
    download_with_retry,
    git_bot_config,
    git_commit_if_dirty,
    git_commit_versions_and_push,
    VERSIONS_FILENAME,
    GIT_STATE_BRANCH,
    GIT_STATE_INIT_MESSAGE,
)


def select_apps_for_build(
    app_entries,
    target_source,
    target_app,
    build_mode,
    default_patches_repo,
    default_patches_mode,
    patch_sources,
):
    selected = []
    for app_table_name, app_entry in app_entries.items():
        if target_app and app_table_name != target_app:
            continue
        if app_entry.get("enabled", True) is False:
            continue
        patches_repo = app_entry.get("patches-source") or default_patches_repo
        if patches_repo not in patch_sources:
            continue
        app_patches_mode = app_entry.get("patches-version") or default_patches_mode
        if build_mode == "pre-release" and app_patches_mode != "pre-release":
            continue
        if (
            build_mode not in (None, "latest", "pre-release")
            and app_patches_mode != build_mode
        ):
            continue
        selected.append((app_table_name, app_entry, patches_repo))
    return selected


def get_apkeditor_url(auth_headers):
    for release_entry in gh(
        "https://api.github.com/repos/REAndroid/APKEditor/releases",
        headers=auth_headers,
    ):
        if not release_entry["prerelease"]:
            for release_asset in release_entry["assets"]:
                if release_asset["name"].lower().endswith(".jar"):
                    return release_asset["browser_download_url"]
    return ""


def download_tool(
    tool_label,
    tool_repo,
    tool_version,
    file_extension,
    download_dir,
    seen_cache,
    seen_filenames,
    dry_run,
    auth_headers,
):
    cache_key = (tool_repo, tool_version)

    if cache_key in seen_cache:
        filename = seen_filenames[cache_key]
        filepath = f"{download_dir}/{filename}"
        log_cache(f"{tool_label}: {filename}")
        return filepath, filename

    tool_release = gh(
        f"https://api.github.com/repos/{tool_repo}/releases/tags/{tool_version}",
        headers=auth_headers,
    )
    candidates = [
        a for a in tool_release.get("assets", []) if a["name"].endswith(file_extension)
    ]

    if not candidates:
        die(f"{tool_label} {file_extension} not found for {tool_version}")

    selected_asset = get_latest_asset(candidates)
    download_url = selected_asset["browser_download_url"]
    filename = selected_asset["name"]
    filepath = f"{download_dir}/{filename}"

    disk_file = Path(filepath)
    if disk_file.exists() and disk_file.stat().st_size > 10_000:
        log_cache(f"{tool_label}: {filename}")
        seen_cache.add(cache_key)
        seen_filenames[cache_key] = filename
        return filepath, filename

    log_sub(tool_label)
    log_kv(f"{tool_label} File", filename)

    if not dry_run:
        if download_with_retry(download_url, filepath) != 0:
            die(f"{tool_label} download failed")

    seen_cache.add(cache_key)
    seen_filenames[cache_key] = filename

    return filepath, filename


def _patches_list_disk_cache_path(patches_list_url):
    try:
        if "raw.githubusercontent.com" in patches_list_url:
            stripped = patches_list_url.replace(
                "https://raw.githubusercontent.com/", ""
            )
            parts = stripped.split("/")
            return (
                Path("patches") / parts[0] / parts[1] / f"patches-list-{parts[2]}.json"
            )
    except Exception:
        pass
    return None


def fetch_patches_list(patches_repo, is_prerelease, app_config, patches_url_cache):
    if app_config.get("patches-list"):
        patches_list_url = gh_blob_to_raw(app_config.get("patches-list"))
    else:
        branch = "dev" if is_prerelease else "main"
        patches_list_url = f"https://raw.githubusercontent.com/{patches_repo}/{branch}/patches-list.json"

    if patches_list_url in patches_url_cache:
        log_cache(f"Patches-list: {patches_list_url}")
        return patches_url_cache[patches_list_url], patches_list_url

    disk_cache_path = _patches_list_disk_cache_path(patches_list_url)
    if disk_cache_path and disk_cache_path.exists():
        log_cache(f"Patches-list: {patches_list_url}")
        patches_json = json.loads(disk_cache_path.read_text())
        patches_url_cache[patches_list_url] = patches_json
        return patches_json, patches_list_url

    patches_json = requests.get(patches_list_url, timeout=60).json()
    patches_url_cache[patches_list_url] = patches_json

    if disk_cache_path:
        disk_cache_path.parent.mkdir(parents=True, exist_ok=True)
        disk_cache_path.write_text(json.dumps(patches_json))

    return patches_json, patches_list_url


def resolve_app_version(
    app_table_name, package_name, app_display_name, app_repo, patches_json, auth_headers
):
    compatible_versions = set()

    for patch_entry in patches_json.get("patches", []):
        compatible_packages = patch_entry.get("compatiblePackages")
        if not isinstance(compatible_packages, dict):
            continue
        if package_name in compatible_packages:
            pkg_compat_versions = compatible_packages[package_name]
            if pkg_compat_versions:
                compatible_versions |= set(pkg_compat_versions)

    release_list = gh(
        f"https://api.github.com/repos/{app_repo}/releases?per_page=100",
        headers=auth_headers,
    )

    available_versions = []
    for release_entry in release_list:
        release_tag = release_entry["tag_name"]
        if not release_tag.startswith(f"{app_display_name}-"):
            continue
        version_str = release_tag[len(app_display_name) + 1 :]
        if version_str.startswith("v"):
            continue
        try:
            Version(version_str)
            available_versions.append(version_str)
        except Exception:
            continue

    if compatible_versions:
        version_candidates = sorted(
            set(compatible_versions) & set(available_versions), key=Version
        )
        if not version_candidates:
            die(f"{app_table_name}: no compatible versions found")
    else:
        version_candidates = sorted(available_versions, key=Version)

    if not version_candidates:
        die(f"{app_table_name}: no versions available")

    return version_candidates[-1]


def build_output_filename(
    app_display_name, app_version, brand, variant, patches_version
):
    filename_parts = [app_display_name]

    normalized_version = strip_v(app_version.strip())
    for name_prefix in (
        app_display_name,
        app_display_name.replace(" ", "-"),
        app_display_name.replace("-", " "),
    ):
        if normalized_version.startswith(f"{name_prefix}-"):
            normalized_version = strip_v(normalized_version[len(name_prefix) + 1 :])
            break

    filename_parts.append(f"v{normalized_version}")
    filename_parts.append(brand)

    if variant:
        filename_parts.append(variant)

    filename_parts.append(ensure_v(patches_version))

    return "-".join(filename_parts) + ".apk"


def ensure_apkeditor(auth_headers):
    apkeditor_path = Path("tools/apkeditor.jar")
    if apkeditor_path.exists() and apkeditor_path.stat().st_size > 10_000:
        log_cache("APKEditor: apkeditor.jar")
        return
    apkeditor_download_url = get_apkeditor_url(auth_headers)
    if not apkeditor_download_url:
        die("APKEditor release not found")
    log_sub("APKEditor")
    if download_with_retry(apkeditor_download_url, "tools/apkeditor.jar") != 0:
        die("apkeditor download failed")


def _merge_apkm(apkm_path, apk_path, auth_headers):
    ensure_apkeditor(auth_headers)
    log_sub("Merging")
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
            apk_path,
        ]
    )
    os.remove(apkm_path)


def fetch_external_apk(app_table_name, app_entry, auth_headers):
    apk_url = app_entry.get("apk-url")
    apkm_url = app_entry.get("apkm-url")

    external_dir = f"unpatched-external/{app_table_name}"
    os.makedirs(external_dir, exist_ok=True)

    if apk_url:
        dest = f"{external_dir}/{app_table_name}.apk"
        if Path(dest).exists() and Path(dest).stat().st_size > 10_000:
            log_cache(f"External APK: {dest}")
            return dest
        if download_with_retry(apk_url, dest) == 0:
            return dest
        if not apkm_url:
            die(f"{app_table_name}: external APK download failed")

    if apkm_url:
        apkm_dest = f"{external_dir}/{app_table_name}.apkm"
        apk_dest = f"{external_dir}/{app_table_name}.apk"
        if Path(apk_dest).exists() and Path(apk_dest).stat().st_size > 10_000:
            log_cache(f"External APK: {apk_dest}")
            return apk_dest
        if download_with_retry(apkm_url, apkm_dest) != 0:
            die(f"{app_table_name}: external APKM download failed")
        _merge_apkm(apkm_dest, apk_dest, auth_headers)
        return apk_dest

    die(f"{app_table_name}: no external URL available")


def fetch_and_merge_apk(
    package_name, app_release, downloaded_apks_cache, app_table_name, auth_headers
):
    apk_assets = [
        a for a in app_release.get("assets", []) if a["name"].endswith(".apk")
    ]
    apkm_assets = [
        a for a in app_release.get("assets", []) if a["name"].endswith(".apkm")
    ]

    if apk_assets:
        download_url = get_latest_asset(apk_assets)["browser_download_url"]
    elif apkm_assets:
        download_url = get_latest_asset(apkm_assets)["browser_download_url"]
    else:
        die(app_table_name)

    apk_filename = os.path.basename(download_url)
    package_dir = f"unpatched/{package_name}"
    os.makedirs(package_dir, exist_ok=True)
    cache_key = f"{package_name}/{apk_filename}"

    if cache_key in downloaded_apks_cache:
        log_cache(f"App: {cache_key}")
        return downloaded_apks_cache[cache_key]

    downloaded_file = f"{package_dir}/{apk_filename}"

    if apk_filename.endswith(".apkm"):
        merged_apk_path = f"{package_dir}/{apk_filename[:-5]}.apk"
    else:
        merged_apk_path = downloaded_file

    disk_file = Path(merged_apk_path)
    if disk_file.exists() and disk_file.stat().st_size > 10_000:
        log_cache(f"App: {cache_key}")
        downloaded_apks_cache[cache_key] = merged_apk_path
        return merged_apk_path

    if download_with_retry(download_url, downloaded_file) != 0:
        die(app_table_name)

    if apk_filename.endswith(".apkm"):
        _merge_apkm(downloaded_file, merged_apk_path, auth_headers)

    downloaded_apks_cache[cache_key] = merged_apk_path
    return merged_apk_path


def patch_app(
    cli_jar, patches_file, input_apk, output_filename, app_config, global_striplibs_arg
):
    raw_patcher_args = shlex.split(app_config.get("patcher-args", ""))
    striplibs_override = next(
        (
            patcher_arg
            for patcher_arg in raw_patcher_args
            if patcher_arg.startswith("--striplibs=")
        ),
        None,
    )

    final_patcher_args = (
        [striplibs_override]
        if striplibs_override
        else ([global_striplibs_arg] if global_striplibs_arg else [])
    ) + [
        patcher_arg
        for patcher_arg in raw_patcher_args
        if not patcher_arg.startswith("--striplibs=")
    ]

    log_sub("Applying patches")

    run(
        [
            "java",
            "-jar",
            cli_jar,
            "patch",
            "--keystore",
            require_env("SIGNING_KEYSTORE_FILE"),
            "--keystore-password",
            require_env("SIGNING_KEYSTORE_PASSWORD"),
            "--keystore-entry-alias",
            require_env("SIGNING_KEY_ALIAS"),
            "--keystore-entry-password",
            require_env("SIGNING_KEY_PASSWORD"),
            "-p",
            patches_file,
            "-o",
            f"build/{output_filename}",
            "--purge",
            input_apk,
        ]
        + final_patcher_args
    )


def run_plan():
    log_plain_section("Plan Start")

    config = load_config()
    github_pat = read_github_pat_from_env()
    auth_headers = build_auth_headers(github_pat)

    subprocess.run(["git", "fetch", "origin", GIT_STATE_BRANCH], check=False)

    state_branch_check = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", GIT_STATE_BRANCH],
        capture_output=True,
        text=True,
    )

    state_branch_exists = state_branch_check.stdout.strip() != ""

    if not state_branch_exists:
        saved_versions = {}
        versions_file_existed = False
    else:
        subprocess.run(
            ["git", "checkout", "-B", GIT_STATE_BRANCH, f"origin/{GIT_STATE_BRANCH}"],
            check=True,
        )
        versions_file_existed = Path(VERSIONS_FILENAME).exists()
        saved_versions = load_versions()

    defaults = get_config_defaults(config)
    default_patches_repo = defaults["patches_repo"]
    default_patches_mode = defaults["patches_mode"]

    app_entries = extract_app_entries(config)

    patch_sources = {}
    for app_entry in app_entries.values():
        if app_entry.get("enabled", True) is False:
            continue
        patches_repo = app_entry.get("patches-source") or default_patches_repo
        patches_mode = app_entry.get("patches-version") or default_patches_mode
        patch_sources.setdefault(patches_repo, set()).add(patches_mode)

    active_patch_sources = set(patch_sources.keys())

    stale_sources_removed = False
    stale_channels_removed = False
    removed_sources = []
    removed_channels = []
    removed_tags_by_source = {}

    for stale_source in list(saved_versions.keys()):
        if stale_source not in active_patch_sources:
            log_sub("Cleanup")
            log_info(f"Removing stale patches source: {stale_source}")
            saved_versions.pop(stale_source)
            removed_sources.append(stale_source)
            stale_sources_removed = True

    if stale_sources_removed and state_branch_exists and versions_file_existed:
        if len(removed_sources) == 1:
            commit_message = f"delete: stale patches source → {removed_sources[0]}"
        else:
            commit_message = "delete: stale patches sources → " + ", ".join(
                removed_sources
            )
        git_commit_versions_and_push(saved_versions, commit_message)

    for patches_repo, modes in patch_sources.items():
        active_custom_tags = {m for m in modes if m not in ("latest", "pre-release")}
        stored_tags = saved_versions.get(patches_repo, {}).get("tags", [])
        stale_tags = [t for t in stored_tags if t not in active_custom_tags]
        if stale_tags:
            saved_versions.setdefault(patches_repo, {})["tags"] = [
                t for t in stored_tags if t in active_custom_tags
            ]
            removed_tags_by_source[patches_repo] = stale_tags

    if removed_tags_by_source and state_branch_exists and versions_file_existed:
        parts = []
        for src, tags in removed_tags_by_source.items():
            parts.append(f"{src} @ {', '.join(tags)}")
        total_tags = sum(len(t) for t in removed_tags_by_source.values())
        if total_tags == 1 and len(removed_tags_by_source) == 1:
            commit_message = f"delete: unused tag → {parts[0]}"
        else:
            commit_message = "delete: unused tags → " + ", ".join(parts)
        git_commit_versions_and_push(saved_versions, commit_message)

    sources_to_build = []
    log_sub("Check")

    for patches_repo, modes in patch_sources.items():
        stored_source_versions = saved_versions.get(patches_repo, {})

        has_prerelease_mode = "pre-release" in modes
        specific_tags = {m for m in modes if m not in ("latest", "pre-release")}

        if not has_prerelease_mode and "pre-release" in stored_source_versions:
            stored_source_versions.pop("pre-release")
            stale_channels_removed = True
            removed_channels.append(patches_repo)

        log_source(patches_repo)

        needs_channel_check = ("latest" in modes) or has_prerelease_mode

        if needs_channel_check:
            upstream_latest, upstream_absolutely_latest = resolve_channels(
                patches_repo, headers=auth_headers
            )

            stored_latest = stored_source_versions.get("latest", {}).get("patches")
            stored_prerelease = stored_source_versions.get("pre-release", {}).get(
                "patches"
            )

            stable_changed = upstream_latest and (
                stored_latest is None
                or Version(upstream_latest) > Version(stored_latest)
            )

            if has_prerelease_mode:
                prerelease_changed = (
                    upstream_absolutely_latest
                    and upstream_absolutely_latest != stored_prerelease
                )

                if stable_changed:
                    overall_status = "UPDATE AVAILABLE"
                elif prerelease_changed:
                    pre_base = (
                        upstream_absolutely_latest.split("-dev", 1)[0]
                        if "-dev" in upstream_absolutely_latest
                        else upstream_absolutely_latest
                    )
                    if stored_latest and Version(pre_base) <= Version(stored_latest):
                        overall_status = "UP TO DATE"
                    else:
                        overall_status = "UPDATE AVAILABLE"
                else:
                    overall_status = "UP TO DATE"

                log_version_status_channels(
                    [("Upstream", upstream_latest), ("Stored", stored_latest)],
                    [
                        ("Upstream", upstream_absolutely_latest),
                        ("Stored", stored_prerelease),
                    ],
                    overall_status,
                )

                if stable_changed:
                    sources_to_build.append(("latest", patches_repo))
                elif prerelease_changed and overall_status == "UPDATE AVAILABLE":
                    sources_to_build.append(("pre-release", patches_repo))

            else:
                if stable_changed:
                    log_version_status(
                        "latest",
                        [("Upstream", upstream_latest), ("Stored", stored_latest)],
                        "UPDATE AVAILABLE",
                    )
                    sources_to_build.append(("latest", patches_repo))
                else:
                    log_version_status(
                        "latest",
                        [("Upstream", upstream_latest), ("Stored", stored_latest)],
                        "UP TO DATE" if upstream_latest else "SKIPPED",
                    )

        stored_tags = stored_source_versions.get("tags", [])
        for tag in sorted(specific_tags):
            tag_resolved, _ = resolve(patches_repo, tag, headers=auth_headers)
            already_stored = tag in stored_tags

            if tag_resolved and not already_stored:
                log_version_status(
                    tag,
                    [("Requested", tag), ("Stored", None)],
                    "UPDATE AVAILABLE",
                )
                sources_to_build.append((tag, patches_repo))
            else:
                log_version_status(
                    tag,
                    [("Requested", tag), ("Stored", tag if already_stored else None)],
                    "NOT FOUND" if not tag_resolved else "UP TO DATE",
                )

    if not sources_to_build:
        log_space()
        log_info("All patches are up to date")
        Path("build_matrix.json").write_text(json.dumps({"include": []}))
        log_plain_section("Plan Complete")
        log_done("Plan finished successfully")
        log_space()
        return

    log_space()
    update_count = len(sources_to_build)
    log_info(
        f"Changes detected: {update_count} patches source"
        + ("s" if update_count != 1 else "")
    )

    matrix_items = []
    for build_channel, patches_repo in sources_to_build:
        matrix_items.append({"source": patches_repo, "mode": build_channel})
    Path("build_matrix.json").write_text(json.dumps({"include": matrix_items}))

    if (
        stale_channels_removed
        and removed_channels
        and state_branch_exists
        and versions_file_existed
    ):
        if len(removed_channels) == 1:
            commit_message = f"delete: unused version channel → {removed_channels[0]}"
        else:
            commit_message = "delete: unused version channels → " + ", ".join(
                removed_channels
            )
        git_commit_versions_and_push(saved_versions, commit_message)

    log_plain_section("Plan Complete")
    log_done("Plan finished successfully")
    log_space()


def generate_build_matrix():
    config = load_config()
    github_pat = read_github_pat_from_env()
    auth_headers = build_auth_headers(github_pat)

    defaults = get_config_defaults(config)
    default_patches_repo = defaults["patches_repo"]
    default_patches_mode = defaults["patches_mode"]

    app_entries = extract_app_entries(config)

    repo_modes = {}
    for app_entry in app_entries.values():
        if app_entry.get("enabled", True) is False:
            continue
        patches_repo = app_entry.get("patches-source") or default_patches_repo
        patches_mode = app_entry.get("patches-version") or default_patches_mode
        repo_modes.setdefault(patches_repo, set()).add(patches_mode)

    matrix_items = []
    for repo, modes in repo_modes.items():
        has_prerelease = "pre-release" in modes
        specific_tags = {m for m in modes if m not in ("latest", "pre-release")}

        if has_prerelease:
            upstream_latest, upstream_absolutely_latest = resolve_channels(
                repo, headers=auth_headers
            )
            emit_prerelease = False
            if upstream_absolutely_latest and upstream_latest:
                pre_base = (
                    upstream_absolutely_latest.split("-dev", 1)[0]
                    if "-dev" in upstream_absolutely_latest
                    else upstream_absolutely_latest
                )
                if Version(pre_base) > Version(upstream_latest):
                    emit_prerelease = True
            elif upstream_absolutely_latest and not upstream_latest:
                emit_prerelease = True
            matrix_items.append({"source": repo, "mode": "latest"})
            if emit_prerelease:
                matrix_items.append({"source": repo, "mode": "pre-release"})
        else:
            matrix_items.append({"source": repo, "mode": "latest"})

        for tag in sorted(specific_tags):
            matrix_items.append({"source": repo, "mode": tag})

    Path("build_matrix.json").write_text(json.dumps({"include": matrix_items}))


def expand_app_matrix():
    config = load_config()
    app_entries = extract_app_entries(config)
    defaults = get_config_defaults(config)
    default_patches_repo = defaults["patches_repo"]
    default_patches_mode = defaults["patches_mode"]
    base = json.loads(os.environ["BASE_MATRIX"])["include"]
    expanded = []

    for item in base:
        source = item["source"]
        mode = item.get("mode")

        for app_table, app_entry in app_entries.items():
            if app_entry.get("enabled", True) is False:
                continue

            app_source = app_entry.get("patches-source") or default_patches_repo
            app_mode = app_entry.get("patches-version") or default_patches_mode

            if app_source != source:
                continue

            if mode == "pre-release" and app_mode != "pre-release":
                continue

            if mode not in (None, "latest", "pre-release") and app_mode != mode:
                continue

            expanded.append({"source": source, "mode": mode, "app": app_table})

    print(json.dumps({"include": expanded}))


def compute_artifact_name():
    name = f"build-{os.environ['MATRIX_SOURCE']}-{os.environ['MATRIX_MODE']}-{os.environ['MATRIX_APP']}"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def artifact_prefix():
    safe = lambda s: re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    source = os.environ["MATRIX_SOURCE"]
    mode = os.environ["MATRIX_MODE"]
    return f"build-{safe(source)}-{safe(mode)}-"


def wait_for_artifacts():
    config = load_config()
    app_entries = extract_app_entries(config)
    defaults = get_config_defaults(config)
    default_patches_repo = defaults["patches_repo"]
    default_patches_mode = defaults["patches_mode"]
    source = os.environ["MATRIX_SOURCE"]
    mode = os.environ["MATRIX_MODE"]
    selected = []

    for app_table, app_entry in app_entries.items():
        if app_entry.get("enabled", True) is False:
            continue
        app_source = app_entry.get("patches-source") or default_patches_repo
        app_mode = app_entry.get("patches-version") or default_patches_mode
        if app_source != source:
            continue
        if mode == "pre-release" and app_mode != "pre-release":
            continue
        if mode not in (None, "latest", "pre-release") and app_mode != mode:
            continue
        selected.append(app_table)

    if not selected:
        print("No apps found for release group", file=sys.stderr)
        sys.exit(1)

    prefix = artifact_prefix()
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    deadline = time.time() + 3600

    while True:
        raw_artifacts = subprocess.check_output(
            ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts"],
            text=True,
        )
        artifact_data = json.loads(raw_artifacts)
        names = [
            item["name"]
            for item in artifact_data.get("artifacts", [])
            if not item.get("expired")
        ]
        matched = [n for n in names if n.startswith(prefix)]

        if len(matched) >= len(selected):
            break

        raw_jobs = subprocess.check_output(
            ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"],
            text=True,
        )
        jobs_data = json.loads(raw_jobs)
        build_jobs = [
            j
            for j in jobs_data.get("jobs", [])
            if j.get("name", "").startswith("build ")
        ]
        all_build_jobs_done = build_jobs and all(
            j.get("status") == "completed" for j in build_jobs
        )

        if all_build_jobs_done:
            if matched:
                break
            print(
                f"All build jobs completed but no artifacts found for {source} {mode}",
                file=sys.stderr,
            )
            sys.exit(1)

        if time.time() > deadline:
            print(
                f"Timed out waiting for artifacts for {source} {mode}", file=sys.stderr
            )
            sys.exit(1)
        time.sleep(15)

    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"prefix={prefix}\n")


def _build_single_app(
    app_table_name,
    app_entry,
    patches_repo,
    build_mode,
    auth_headers,
    default_brand,
    default_patches_mode,
    default_cli_repo,
    default_cli_mode,
    default_striplibs_arg,
    seen_cli_cache,
    seen_cli_filenames,
    seen_patches_cache,
    seen_patches_filenames,
    downloaded_apks_cache,
    patches_url_cache,
    dry_run,
):
    log_section(app_table_name)

    patches_mode = resolve_build_mode(
        build_mode, app_entry, "patches-version", default_patches_mode
    )

    patches_version, is_prerelease = resolve(
        patches_repo, patches_mode, headers=auth_headers, strict=True
    )

    log_sub("Resolved")
    log_kv("Patches", patches_repo)
    log_kv("Patches Version", strip_v(patches_version))

    cli_repo = app_entry.get("cli-source") or default_cli_repo
    cli_resolve_mode = resolve_build_mode(
        build_mode, app_entry, "cli-version", default_cli_mode
    )

    cli_version, _ = resolve(
        cli_repo, cli_resolve_mode, headers=auth_headers, strict=True
    )

    log_kv("CLI", cli_repo)
    log_kv("CLI Version", strip_v(cli_version))

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
    brand = app_entry.get("morphe-brand") or default_brand
    app_name = app_entry.get("app-name")
    app_display_name = app_name or app_table_name
    variant = app_entry.get("variant")

    external_apk_url = app_entry.get("apk-url")
    external_apkm_url = app_entry.get("apkm-url")
    is_external = bool(external_apk_url or external_apkm_url)

    if is_external:
        configured_app_version = app_entry.get("app-version")
        resolved_app_version = (
            configured_app_version if configured_app_version else "Unknown"
        )
    else:
        app_repo = app_entry.get("app-source") or die(app_table_name)
        configured_app_version = app_entry.get("app-version")
        app_version_mode = configured_app_version or "auto"

        if app_version_mode == "auto":
            patches_json, _ = fetch_patches_list(
                patches_repo,
                is_prerelease,
                app_entry,
                patches_url_cache,
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
        return {
            "app_table_name": app_table_name,
            "output_filename": output_filename,
            "resolved_app_version": resolved_app_version,
            "variant": variant,
            "app_display_name": app_display_name,
            "patches_repo": patches_repo,
            "patches_version": patches_version,
            "is_prerelease": is_prerelease,
            "cli_repo": cli_repo,
            "cli_version": cli_version,
        }

    log_sub("App")
    log_kv("Package", package_name)
    log_kv("App Version", strip_v(resolved_app_version))

    if is_external:
        input_apk = fetch_external_apk(app_table_name, app_entry, auth_headers)
    else:
        input_apk = fetch_and_merge_apk(
            package_name,
            app_release,
            downloaded_apks_cache,
            app_table_name,
            auth_headers,
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

    metadata = {
        "source": patches_repo,
        "mode": patches_mode,
        "app_table_name": app_table_name,
        "package_name": package_name,
        "app_name": app_name if app_name else None,
        "app_version": resolved_app_version,
        "morphe_brand": brand,
        "variant": variant if variant else None,
        "output": output_filename,
        "cli_repo": cli_repo,
        "cli_version": cli_version,
    }

    Path("build").mkdir(exist_ok=True)
    Path(f"build/{app_table_name}.json").write_text(json.dumps(metadata, indent=2))

    return {
        "app_table_name": app_table_name,
        "output_filename": output_filename,
        "resolved_app_version": resolved_app_version,
        "variant": variant,
        "app_display_name": app_display_name,
        "patches_repo": patches_repo,
        "patches_version": patches_version,
        "is_prerelease": is_prerelease,
        "cli_repo": cli_repo,
        "cli_version": cli_version,
    }


def run_build():
    target_source, build_mode, dry_run, auth_headers = init()
    target_app = os.environ.get("PEACHMEOW_APP")

    log_plain_section("Build Start")
    log_kv("Mode", build_mode)
    log_kv("Source", target_source or "all")
    log_kv("Dry Run", dry_run)

    require_env("SIGNING_KEYSTORE_PASSWORD")
    require_env("SIGNING_KEY_ALIAS")
    require_env("SIGNING_KEY_PASSWORD")

    config = load_config()
    defaults = get_config_defaults(config)

    default_patches_repo = defaults["patches_repo"]
    default_cli_repo = defaults["cli_repo"]
    default_brand = defaults["brand"]
    default_patches_mode = defaults["patches_mode"]
    default_cli_mode = defaults["cli_mode"]
    default_striplibs_arg = parse_global_striplibs(config)

    app_entries = extract_app_entries(config)

    seen_cli_cache = set()
    seen_patches_cache = set()
    seen_cli_filenames = {}
    seen_patches_filenames = {}
    downloaded_apks_cache = {}
    patches_url_cache = {}

    if target_source:
        patch_sources = [target_source]
    else:
        patch_sources = [
            app_entry.get("patches-source") or default_patches_repo
            for app_entry in app_entries.values()
            if app_entry.get("enabled", True)
        ]

    if not dry_run:
        if is_ci_environment():
            mkdir_clean("unpatched", "unpatched-external", "tools", "patches", "build")
        else:
            mkdir_clean("build")
            mkdir_ensure("unpatched", "unpatched-external", "tools", "patches")

    selected_apps = select_apps_for_build(
        app_entries,
        target_source,
        target_app,
        build_mode,
        default_patches_repo,
        default_patches_mode,
        patch_sources,
    )

    if not selected_apps:
        die("Nothing built")

    def build_single_app(app_table_name, app_entry, patches_repo):
        return _build_single_app(
            app_table_name,
            app_entry,
            patches_repo,
            build_mode,
            auth_headers,
            default_brand,
            default_patches_mode,
            default_cli_repo,
            default_cli_mode,
            default_striplibs_arg,
            set() if is_ci_environment() else seen_cli_cache,
            {} if is_ci_environment() else seen_cli_filenames,
            set() if is_ci_environment() else seen_patches_cache,
            {} if is_ci_environment() else seen_patches_filenames,
            {} if is_ci_environment() else downloaded_apks_cache,
            {} if is_ci_environment() else patches_url_cache,
            dry_run,
        )

    results = []
    if dry_run:
        for app_table_name, app_entry, patches_repo in selected_apps:
            results.append(build_single_app(app_table_name, app_entry, patches_repo))
        log_done("Dry run complete")
        sys.exit(0)

    if is_ci_environment():
        with ThreadPoolExecutor(
            max_workers=min(len(selected_apps), 8) or 1
        ) as executor:
            future_map = {
                executor.submit(
                    build_single_app, app_table_name, app_entry, patches_repo
                ): idx
                for idx, (app_table_name, app_entry, patches_repo) in enumerate(
                    selected_apps
                )
            }
            ordered = {}
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    ordered[idx] = future.result()
                except SystemExit:
                    app_table_name = selected_apps[idx][0]
                    log_info(f"Skipping {app_table_name} due to build failure")
            for idx in sorted(ordered):
                results.append(ordered[idx])
    else:
        for app_table_name, app_entry, patches_repo in selected_apps:
            try:
                results.append(
                    build_single_app(app_table_name, app_entry, patches_repo)
                )
            except SystemExit:
                log_info(f"Skipping {app_table_name} due to build failure")

    built_apps = [
        (
            item["app_table_name"],
            item["output_filename"],
            item["resolved_app_version"],
            item["variant"],
            item["app_display_name"],
        )
        for item in results
    ]

    if not built_apps:
        die("Nothing built")

    if not is_ci_environment():
        local_output_dir = "/sdcard/Download/🐱 PeachMeow"
        os.makedirs(local_output_dir, exist_ok=True)
        for _, output_filename, *_ in built_apps:
            shutil.copy2(
                f"build/{output_filename}", f"{local_output_dir}/{output_filename}"
            )
            log_done(f"Saved: {local_output_dir}/{output_filename}")
        log_plain_section("Build Complete")
        sys.exit(0)

    log_plain_section("Build Complete")
    log_done("Build finished")


def parse_test_apps(raw_input, app_entries):
    parts = [p.strip() for p in raw_input.split(",")]
    apps = []
    seen = set()
    for part in parts:
        if not part:
            continue
        if part not in app_entries:
            die(f"Unknown app: '{part}'. Valid apps: {', '.join(app_entries.keys())}")
        if part not in seen:
            seen.add(part)
            apps.append(part)
    if not apps:
        die("No app names provided. Use --apps 'AppName1, AppName2'")
    return apps


def expand_test_matrix():
    raw = os.environ.get("TEST_APPS", "").strip()
    if not raw:
        die("TEST_APPS env var is required")
    config = load_config()
    app_entries = extract_app_entries(config)
    apps = parse_test_apps(raw, app_entries)
    print(json.dumps({"include": [{"app": a} for a in apps]}))


def run_test_build():
    import argparse
    import getpass

    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--apps", required=True)
    arg_parser.add_argument("--dry-run", action="store_true")
    parsed_args = arg_parser.parse_args()

    raw_apps = parsed_args.apps.strip()
    dry_run = parsed_args.dry_run

    github_pat = read_github_pat_from_env()
    if not github_pat and not is_ci_environment():
        log_space()
        github_pat = getpass.getpass(
            "Enter your GitHub PAT or press Enter to skip: "
        ).strip()
        log_space()

    if github_pat:
        log_done("PAT accepted")
    else:
        log_info("No PAT entered. Running unauthenticated (may hit rate limits)")

    auth_headers = build_auth_headers(github_pat)

    require_env("SIGNING_KEYSTORE_PASSWORD")
    require_env("SIGNING_KEY_ALIAS")
    require_env("SIGNING_KEY_PASSWORD")

    config = load_config()
    defaults = get_config_defaults(config)

    default_patches_repo = defaults["patches_repo"]
    default_cli_repo = defaults["cli_repo"]
    default_brand = defaults["brand"]
    default_patches_mode = defaults["patches_mode"]
    default_cli_mode = defaults["cli_mode"]
    default_striplibs_arg = parse_global_striplibs(config)

    app_entries = extract_app_entries(config)
    apps = parse_test_apps(raw_apps, app_entries)

    log_plain_section("Test Build Start")
    log_kv("Apps", ", ".join(apps))

    if not dry_run:
        if is_ci_environment():
            mkdir_clean("unpatched", "unpatched-external", "tools", "patches", "build")
        else:
            mkdir_clean("build")
            mkdir_ensure("unpatched", "unpatched-external", "tools", "patches")

    for app_table_name in apps:
        app_entry = app_entries[app_table_name]
        patches_repo = app_entry.get("patches-source") or default_patches_repo
        effective_patches_mode = (
            app_entry.get("patches-version") or default_patches_mode
        )
        effective_cli_mode = app_entry.get("cli-version") or default_cli_mode
        try:
            result = _build_single_app(
                app_table_name,
                app_entry,
                patches_repo,
                effective_patches_mode,
                auth_headers,
                default_brand,
                effective_patches_mode,
                default_cli_repo,
                effective_cli_mode,
                default_striplibs_arg,
                set(),
                {},
                set(),
                {},
                {},
                {},
                dry_run,
            )
            if result and not dry_run and not is_ci_environment():
                output_filename = result["output_filename"]
                local_output_dir = "/sdcard/Download/🐱 PeachMeow"
                os.makedirs(local_output_dir, exist_ok=True)
                shutil.copy2(
                    f"build/{output_filename}",
                    f"{local_output_dir}/{output_filename}",
                )
                log_done(f"Saved: {local_output_dir}/{output_filename}")
        except SystemExit:
            log_info(f"Skipping {app_table_name} due to build failure")

    log_plain_section("Test Build Complete")
    log_done("Test build finished")


def build_release_notes(built_apps, used_patches_versions, cli_entries, auth_headers):
    patches_repo = list(used_patches_versions.keys())[0]
    patches_version = list(used_patches_versions.values())[0]

    patches_release = gh(
        f"https://api.github.com/repos/{patches_repo}/releases/tags/{patches_version}",
        headers=auth_headers,
    )
    changelog = patches_release.get("body") or ""
    is_prerelease = patches_release.get("prerelease", False)

    note_lines = []

    brand_apps = {}
    for (
        app_table_name,
        _,
        app_version_str,
        variant,
        app_display_name,
        morphe_brand,
    ) in built_apps:
        brand_apps.setdefault(morphe_brand, {}).setdefault(app_display_name, []).append(
            (variant, app_version_str)
        )

    priority = ["youtube", "music"]

    def app_sort_key(app_name):
        if app_name.lower() in priority:
            return (0, priority.index(app_name.lower()))
        return (1, app_name.lower())

    total_apps = sum(len(apps) for apps in brand_apps.values())
    app_header = "App Version 📦" if total_apps == 1 else "App Versions 📦"

    for brand in sorted(brand_apps.keys()):
        apps_grouped = brand_apps[brand]
        has_variants = any(
            len(items) > 1 or (len(items) == 1 and items[0][0] is not None)
            for items in apps_grouped.values()
        )

        note_lines.append(f"## {app_header}\n")

        if not has_variants:
            for app_name in sorted(apps_grouped.keys(), key=app_sort_key):
                _, app_version_str = apps_grouped[app_name][0]
                note_lines.append(f"- {app_name}: {strip_v(app_version_str)}")
            note_lines.append("")
        else:
            for app_name in sorted(apps_grouped.keys(), key=app_sort_key):
                note_lines.append(f"### {app_name}")
                items = apps_grouped[app_name]

                def variant_sort_key(item):
                    variant, _ = item
                    return (0, "") if variant is None else (1, variant.lower())

                for variant, app_version_str in sorted(items, key=variant_sort_key):
                    if len(items) == 1 and variant is None:
                        note_lines.append(f"- {strip_v(app_version_str)}")
                    elif variant is None:
                        note_lines.append(f"- Base: {strip_v(app_version_str)}")
                    else:
                        note_lines.append(f"- {variant}: {strip_v(app_version_str)}")

                note_lines.append("")

    seen_cli_keys = set()
    deduped_cli_entries = []
    for cli_repo, cli_version in cli_entries:
        key = (cli_repo, strip_v(cli_version))
        if key not in seen_cli_keys:
            seen_cli_keys.add(key)
            deduped_cli_entries.append((cli_repo, cli_version))

    patches_version_link = f"[{ensure_v(patches_version)}](https://github.com/{patches_repo}/releases/tag/{patches_version})"

    note_lines.append("## Build Info ℹ️\n")
    note_lines.append(f"- Patches: {patches_version_link}")
    if len(deduped_cli_entries) == 1:
        cli_repo, cli_version = deduped_cli_entries[0]
        cli_version_link = f"[{ensure_v(cli_version)}](https://github.com/{cli_repo}/releases/tag/{cli_version})"
        note_lines.append(f"- CLI: {cli_version_link}")
    else:
        note_lines.append("- CLI: 🚫⚠️❌")
    note_lines.append("")
    note_lines.append("## Changelog 📝\n")
    note_lines.append(changelog)

    release_brand = sorted(brand_apps.keys())[0]

    return "\n".join(note_lines), patches_version, is_prerelease, release_brand


def publish_release(built_apps, release_notes, is_prerelease, brand, patches_version):
    release_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    release_tag = release_time.strftime("%Y%m%d-%H%M%S-%f")
    release_title = f"{brand} 🐱 PeachMeow {ensure_v(patches_version)}"

    Path("release.md").write_text(release_notes)

    existing_tag_check = subprocess.run(
        ["gh", "release", "view", release_tag],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if existing_tag_check.returncode == 0:
        subprocess.run(
            ["gh", "release", "delete", release_tag, "-y", "--cleanup-tag"], check=False
        )

    gh_release_cmd = [
        "gh",
        "release",
        "create",
        release_tag,
        "-t",
        release_title,
        "-F",
        "release.md",
    ] + [f"build/{output_filename}" for _, output_filename, *_ in built_apps]

    if is_prerelease:
        gh_release_cmd.append("--prerelease")

    subprocess.run(gh_release_cmd, check=True)


def update_state(
    used_patches_versions, patches_source_order, build_mode, is_prerelease
):
    patches_repo = list(used_patches_versions.keys())[0]
    patches_version = list(used_patches_versions.values())[0]
    commit_message = f"release: {patches_repo} → {patches_version}"

    for attempt in range(8):
        subprocess.run(["git", "fetch", "origin", GIT_STATE_BRANCH], check=False)

        state_branch_check = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", GIT_STATE_BRANCH],
            capture_output=True,
            text=True,
        )

        branch_exists = state_branch_check.stdout.strip() != ""

        if not branch_exists:
            subprocess.run(
                ["git", "checkout", "--orphan", GIT_STATE_BRANCH], check=True
            )
            subprocess.run(["git", "rm", "-rf", "."], check=False)
            subprocess.run(["git", "clean", "-fd"], check=False)

            if not Path(VERSIONS_FILENAME).exists():
                Path(VERSIONS_FILENAME).write_text("{}\n")

            git_bot_config()
            subprocess.run(["git", "add", VERSIONS_FILENAME], check=True)
            subprocess.run(["git", "commit", "-m", GIT_STATE_INIT_MESSAGE], check=True)
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", GIT_STATE_BRANCH]
            )
            if push_result.returncode != 0:
                time.sleep(2**attempt)
                continue

            subprocess.run(["git", "fetch", "origin", GIT_STATE_BRANCH], check=False)

        subprocess.run(
            ["git", "checkout", "-B", GIT_STATE_BRANCH, f"origin/{GIT_STATE_BRANCH}"],
            check=True,
        )
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{GIT_STATE_BRANCH}"], check=True
        )
        subprocess.run(["git", "clean", "-fd"], check=True)

        stored_versions = load_versions()

        source_version_entry = stored_versions.setdefault(patches_repo, {})

        is_custom_tag = build_mode not in (None, "latest", "pre-release")

        if is_custom_tag:
            existing_tags = source_version_entry.get("tags", [])
            if patches_version not in existing_tags:
                existing_tags.append(patches_version)
            source_version_entry["tags"] = existing_tags
        elif is_prerelease:
            source_version_entry["pre-release"] = {
                "patches": patches_version,
            }
        else:
            source_version_entry["latest"] = {
                "patches": patches_version,
            }

        ordered_stored_versions = {}
        for patches_src in patches_source_order:
            if patches_src in stored_versions:
                ordered_stored_versions[patches_src] = stored_versions[patches_src]
        for patches_src in stored_versions:
            if patches_src not in ordered_stored_versions:
                ordered_stored_versions[patches_src] = stored_versions[patches_src]

        for patches_src, channels in ordered_stored_versions.items():
            ordered_channels = {}
            for key in ["latest", "pre-release"]:
                if key in channels:
                    ordered_channels[key] = channels[key]
            if "tags" in channels:
                try:
                    ordered_channels["tags"] = sorted(
                        channels["tags"],
                        key=lambda t: Version(strip_v(t)),
                        reverse=True,
                    )
                except Exception:
                    ordered_channels["tags"] = channels["tags"]
            ordered_stored_versions[patches_src] = ordered_channels

        Path(VERSIONS_FILENAME).write_text(
            json.dumps(ordered_stored_versions, indent=2)
        )

        git_bot_config()
        subprocess.run(["git", "add", VERSIONS_FILENAME], check=True)
        git_commit_if_dirty(commit_message)

        push_result = subprocess.run(["git", "push", "origin", GIT_STATE_BRANCH])
        if push_result.returncode == 0:
            return

        subprocess.run(
            ["git", "reset", "--hard", f"origin/{GIT_STATE_BRANCH}"], check=False
        )
        subprocess.run(["git", "clean", "-fd"], check=False)
        time.sleep(2**attempt)

    die("Failed to update state")


def _extract_patches_version_from_release_body(body):
    if not body:
        return None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- Patches:"):
            content = stripped[len("- Patches:") :].strip()
            if content.startswith("[") and "](" in content:
                return content[1 : content.index("](")]
            return content or None
    return None


def _extract_brand_from_release_title(title):
    if not title:
        return None
    marker = " 🐱 PeachMeow "
    idx = title.find(marker)
    if idx == -1:
        return None
    return title[:idx].strip() or None


def _delete_release_and_tag(tag_name):
    subprocess.run(
        ["gh", "release", "delete", tag_name, "--yes", "--cleanup-tag"],
        check=False,
        stderr=subprocess.DEVNULL,
    )


def cleanup_releases(config, auth_headers):
    if not config.get("release-cleanup", False):
        return

    repo_result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    current_repo = repo_result.stdout.strip()
    if not current_repo:
        return

    defaults = get_config_defaults(config)
    default_brand = defaults["brand"]
    default_patches_repo = defaults["patches_repo"]
    default_patches_mode = defaults["patches_mode"]
    app_entries = extract_app_entries(config)

    present_brands = set()
    for app_entry in app_entries.values():
        brand = app_entry.get("morphe-brand") or default_brand
        present_brands.add(brand)

    active_brands = set()
    for app_entry in app_entries.values():
        if app_entry.get("enabled", True) is False:
            continue
        brand = app_entry.get("morphe-brand") or default_brand
        active_brands.add(brand)

    active_custom_tags_by_source = {}
    for app_entry in app_entries.values():
        if app_entry.get("enabled", True) is False:
            continue
        patches_repo = app_entry.get("patches-source") or default_patches_repo
        patches_mode = app_entry.get("patches-version") or default_patches_mode
        if patches_mode not in ("latest", "pre-release"):
            active_custom_tags_by_source.setdefault(patches_repo, set()).add(
                patches_mode
            )

    brand_to_source = {}
    for app_entry in app_entries.values():
        brand = app_entry.get("morphe-brand") or default_brand
        patches_repo = app_entry.get("patches-source") or default_patches_repo
        brand_to_source[brand] = patches_repo

    all_releases = []
    page = 1
    while True:
        page_releases = gh(
            f"https://api.github.com/repos/{current_repo}/releases?per_page=100&page={page}",
            headers=auth_headers,
        )
        if not page_releases:
            break
        all_releases.extend(page_releases)
        if len(page_releases) < 100:
            break
        page += 1

    releases_by_brand = {}
    for release in all_releases:
        title = release.get("name") or ""
        brand = _extract_brand_from_release_title(title)
        if not brand:
            continue
        releases_by_brand.setdefault(brand, []).append(release)

    for brand, releases in releases_by_brand.items():
        if brand not in present_brands:
            for release in releases:
                _delete_release_and_tag(release["tag_name"])
            continue

        if brand not in active_brands:
            continue

        source_for_brand = brand_to_source.get(brand)
        active_tags_for_brand = active_custom_tags_by_source.get(
            source_for_brand, set()
        )
        active_tags_normalized = {ensure_v(t) for t in active_tags_for_brand}

        custom_releases = []
        non_custom_releases = []
        for r in releases:
            ver = _extract_patches_version_from_release_body(r.get("body") or "")
            if ver and ensure_v(ver) in active_tags_normalized:
                custom_releases.append(r)
            else:
                non_custom_releases.append(r)

        seen_custom_versions = {}
        for r in sorted(
            custom_releases,
            key=lambda r: r.get("published_at") or r.get("created_at") or "",
            reverse=True,
        ):
            ver = _extract_patches_version_from_release_body(r.get("body") or "")
            if not ver:
                continue
            ver_normalized = ensure_v(ver)
            if ver_normalized in seen_custom_versions:
                _delete_release_and_tag(r["tag_name"])
            else:
                seen_custom_versions[ver_normalized] = r

        stable_releases = sorted(
            [r for r in non_custom_releases if not r["prerelease"]],
            key=lambda r: r.get("published_at") or r.get("created_at") or "",
            reverse=True,
        )
        pre_releases = [r for r in non_custom_releases if r["prerelease"]]

        seen_stable_versions = {}
        for r in stable_releases:
            ver = _extract_patches_version_from_release_body(r.get("body") or "")
            if not ver:
                continue
            ver_normalized = ensure_v(ver)
            if ver_normalized in seen_stable_versions:
                _delete_release_and_tag(r["tag_name"])
            else:
                seen_stable_versions[ver_normalized] = r

        deduplicated_stable = sorted(
            seen_stable_versions.values(),
            key=lambda r: r.get("published_at") or r.get("created_at") or "",
            reverse=True,
        )

        for old_release in deduplicated_stable[3:]:
            _delete_release_and_tag(old_release["tag_name"])

        kept_stable_versions = set()
        for r in deduplicated_stable[:3]:
            ver = _extract_patches_version_from_release_body(r.get("body") or "")
            if ver:
                kept_stable_versions.add(ensure_v(ver))

        seen_pre_versions = {}
        for pre_release in pre_releases:
            pre_ver = _extract_patches_version_from_release_body(
                pre_release.get("body") or ""
            )
            if not pre_ver:
                continue
            pre_ver_full = ensure_v(pre_ver)
            if pre_ver_full in seen_pre_versions:
                _delete_release_and_tag(pre_release["tag_name"])
            else:
                seen_pre_versions[pre_ver_full] = pre_release

        newest_kept_stable = (
            max(kept_stable_versions, key=lambda v: Version(strip_v(v)))
            if kept_stable_versions
            else None
        )

        for pre_ver_full, pre_release in seen_pre_versions.items():
            if "-dev" in pre_ver_full:
                base_version = pre_ver_full.split("-dev")[0]
                if newest_kept_stable and Version(strip_v(base_version)) <= Version(
                    strip_v(newest_kept_stable)
                ):
                    _delete_release_and_tag(pre_release["tag_name"])


def run_release():
    target_source, build_mode, dry_run, auth_headers = init()

    require_env("SIGNING_KEYSTORE_PASSWORD")
    require_env("SIGNING_KEY_ALIAS")
    require_env("SIGNING_KEY_PASSWORD")

    config = load_config()
    log_plain_section("Release Start")

    defaults = get_config_defaults(config)
    default_patches_repo = defaults["patches_repo"]
    default_cli_repo = defaults["cli_repo"]

    app_entries = extract_app_entries(config)

    metadata_files = sorted(
        p
        for p in Path("build").rglob("*.json")
        if p.is_file() and p.name != "build_matrix.json"
    )
    release_items = []
    for file in metadata_files:
        data = json.loads(file.read_text())
        if not isinstance(data, dict):
            continue
        if not all(
            k in data
            for k in (
                "source",
                "mode",
                "app_table_name",
                "output",
                "app_version",
                "morphe_brand",
            )
        ):
            continue
        if data["source"] != target_source:
            continue
        if data["mode"] != build_mode:
            continue
        release_items.append(data)

    if not release_items:
        die("No metadata found")

    patches_version, is_prerelease = resolve(
        target_source, build_mode, headers=auth_headers, strict=True
    )
    used_patches_versions = {target_source: patches_version}

    built_apps = []
    cli_entries = []
    seen_cli_keys = set()
    for item in release_items:
        built_apps.append(
            (
                item["app_table_name"],
                item["output"],
                item["app_version"],
                item.get("variant"),
                item["app_name"] or item["app_table_name"],
                item.get("morphe_brand") or "",
            )
        )
        item_cli_repo = item.get("cli_repo") or (
            app_entries.get(item["app_table_name"], {}).get("cli-source")
            or default_cli_repo
        )
        item_cli_version = item.get("cli_version") or ""
        cli_key = (item_cli_repo, item_cli_version)
        if cli_key not in seen_cli_keys:
            seen_cli_keys.add(cli_key)
            cli_entries.append((item_cli_repo, item_cli_version))

    brands_in_release = {item.get("morphe_brand") or "" for item in release_items}
    if len(brands_in_release) > 1:
        die(
            f"Misconfiguration: multiple morphe-brands detected in the same patches source: {', '.join(sorted(brands_in_release))}. All apps under one patches source must use the same morphe-brand."
        )

    release_notes, patches_version, is_prerelease, release_brand = build_release_notes(
        built_apps,
        used_patches_versions,
        cli_entries,
        auth_headers,
    )

    publish_release(
        built_apps, release_notes, is_prerelease, release_brand, patches_version
    )

    toml_order_sources = [
        app_entry.get("patches-source") or default_patches_repo
        for app_table_name, app_entry in app_entries.items()
        if isinstance(app_entry, dict) and app_entry.get("enabled", True)
    ]
    update_state(used_patches_versions, toml_order_sources, build_mode, is_prerelease)

    cleanup_releases(config, auth_headers)
    log_plain_section("Release Complete")
    log_done("Release created")
    sys.exit(0)
