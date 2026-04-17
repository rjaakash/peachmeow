import os
import sys
import json
import time
import shlex
import tomllib
import argparse
import getpass
import subprocess
import requests
from pathlib import Path
from typing import List, Dict
from packaging.version import Version
from datetime import datetime
from zoneinfo import ZoneInfo

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

CONFIG_FILENAME = "config.toml"
VERSIONS_FILENAME = "versions.json"
GIT_STATE_BRANCH = "state"
GIT_STATE_INIT_MESSAGE = "state: initial 🐱 PeachMeow metadata"
GIT_BOT_USERNAME = "github-actions[bot]"
GIT_BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def die(message):
    print(f"{RED}[ERROR]{RESET} {message}", flush=True)
    sys.exit(1)


def log_space():
    print("", flush=True)


def log_download(msg):
    print(f"{CYAN}[DOWNLOAD]{RESET} {msg}", flush=True)


def log_done(msg):
    print(f"{GREEN}[DONE]{RESET} {msg}", flush=True)


def log_retry(msg):
    print(f"{ORANGE}[RETRY]{RESET} {msg}", flush=True)


def log_sub(title):
    log_space()
    print(f"{BLUE}[STEP]{RESET} {title}", flush=True)


def log_source(source_repo):
    log_space()
    print(
        f"{PURPLE}\033[1m[SOURCE]\033[0m{RESET} → {PURPLE}\033[1;4m{source_repo}\033[0m{RESET}",
        flush=True,
    )
    log_space()


def log_source_compact(source_repo):
    log_space()
    print(
        f"{PURPLE}\033[1m[SOURCE]\033[0m{RESET} → {PURPLE}\033[1;4m{source_repo}\033[0m{RESET}",
        flush=True,
    )


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
    centered_title = title.center(width)
    print(f"{AMBER}{centered_title}{RESET}", flush=True)
    print(f"{NEUTRAL}{'=' * width}{RESET}", flush=True)
    log_space()


def log_kv(key, value):
    print(f"{key}: {value}", flush=True)


def log_version_status(section_title, version_lines, update_status):
    log_kv("Mode", section_title)
    log_space()
    for line_label, line_value in version_lines:
        print(
            f"  {line_label}: {line_value if line_value is not None else '—'}",
            flush=True,
        )
    log_space()
    log_kv("Status", update_status)


def log_version_status_channels(latest_lines, prerelease_lines, update_status):
    log_kv("Mode", "pre-release")
    log_space()
    print("  \033[4mlatest\033[0m", flush=True)
    for line_label, line_value in latest_lines:
        print(
            f"  {line_label}: {line_value if line_value is not None else '—'}",
            flush=True,
        )
    log_space()
    print("  \033[4mpre-release\033[0m", flush=True)
    for line_label, line_value in prerelease_lines:
        print(
            f"  {line_label}: {line_value if line_value is not None else '—'}",
            flush=True,
        )
    log_space()
    log_kv("Status", update_status)


def log_cache(msg):
    print(f"{PINK}[CACHE]{RESET} {msg}", flush=True)


def _is_community_build():
    return not os.environ.get("SIGNING_KEYSTORE_FILE")


def is_ci_environment():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def require_env(env_name):
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value

    if _is_community_build():
        if env_name == "SIGNING_KEYSTORE_FILE":
            return "peachmeow-community.bks"
        if env_name == "SIGNING_KEYSTORE_PASSWORD":
            return "7777777"
        if env_name == "SIGNING_KEY_ALIAS":
            return "RJ"
        if env_name == "SIGNING_KEY_PASSWORD":
            return "7777777"

    die(f"missing env: {env_name}")


def download_with_retry(url, output, retries=3):
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(retries):
        log_download(output)
        print(f"          ← {url}", flush=True)

        curl_result = subprocess.run(
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

        output_file = Path(output)

        if (
            curl_result.returncode == 0
            and output_file.exists()
            and output_file.stat().st_size > 10_000
        ):
            log_done(output)
            return 0

        if output_file.exists():
            output_file.unlink()

        log_retry(f"{output} (attempt {attempt+1}/{retries})")
        time.sleep(2**attempt)

    print(f"{RED}[ERROR]{RESET} Failed to download {output}", flush=True)
    return 1


def run(cmd):
    print(f"{SKY}[RUN]{RESET} {' '.join(cmd)}", flush=True)
    proc_result = subprocess.run(cmd)
    if proc_result.returncode != 0:
        die(f"command failed ({proc_result.returncode}): {' '.join(cmd)}")


def ensure_apk(apk_file):
    file_check = subprocess.run(["file", apk_file], capture_output=True, text=True)
    if "apk" not in file_check.stdout.lower():
        die("bad apk")


def mkdir_clean(*dirs):
    for directory in dirs:
        dir_path = Path(directory)
        if dir_path.exists():
            subprocess.run(["rm", "-rf", directory])
        dir_path.mkdir(parents=True, exist_ok=True)


def mkdir_ensure(*dirs):
    for directory in dirs:
        Path(directory).mkdir(parents=True, exist_ok=True)


def gh_blob_to_raw(blob_url):
    if "github.com" in blob_url and "/blob/" in blob_url:
        return blob_url.replace(
            "https://github.com/", "https://raw.githubusercontent.com/"
        ).replace("/blob/", "/")
    return blob_url


def get_latest_asset(assets: List[Dict]) -> Dict:
    if not assets:
        raise ValueError("No assets found")

    return sorted(
        assets,
        key=lambda asset_entry: asset_entry.get("updated_at", "1970-01-01T00:00:00Z"),
        reverse=True,
    )[0]


def strip_v(version):
    return version.lstrip("v") if version else version


def ensure_v(version):
    if not version:
        return version
    stripped = version.lstrip("v")
    return f"v{stripped}"


def resolve(repo, mode, headers=None, strict=False):
    if headers is None:
        headers = {}

    api_response = requests.get(
        f"https://api.github.com/repos/{repo}/releases",
        headers=headers,
        timeout=60,
    )

    if api_response.status_code != 200:
        if strict:
            die(f"GitHub API failed: {repo}")
        return None, False

    release_list = api_response.json()

    if not release_list:
        if strict:
            die(f"No releases found for {repo}")
        return None, False

    if mode == "latest":
        for release_entry in release_list:
            if not release_entry["prerelease"]:
                return release_entry["tag_name"], False
        if strict:
            die(f"No stable release found for {repo}")
        return None, False

    if mode == "pre-release":
        release_entry = release_list[0]
        return release_entry["tag_name"], release_entry["prerelease"]

    for release_entry in release_list:
        if release_entry["tag_name"] == mode:
            return release_entry["tag_name"], release_entry["prerelease"]

    if strict:
        die(f"Version '{mode}' not found for {repo}")
    return None, False


def resolve_channels(repo, headers=None):
    if headers is None:
        headers = {}

    api_response = requests.get(
        f"https://api.github.com/repos/{repo}/releases",
        headers=headers,
        timeout=60,
    )
    if api_response.status_code != 200:
        die(f"Failed to fetch {repo}")

    release_list = api_response.json()
    latest_stable = None
    absolutely_latest = release_list[0]["tag_name"] if release_list else None

    for release_entry in release_list:
        if not release_entry["prerelease"]:
            latest_stable = release_entry["tag_name"]
            break

    return latest_stable, absolutely_latest


def gh(url, headers=None):
    if headers is None:
        headers = {}
    api_response = requests.get(url, headers=headers, timeout=60)
    if api_response.status_code != 200:
        die(f"GitHub API failed: {url}")
    return api_response.json()


def load_config():
    if not Path(CONFIG_FILENAME).exists():
        die("config.toml missing")
    with open(CONFIG_FILENAME, "rb") as f:
        return tomllib.load(f)


def load_versions():
    if not Path(VERSIONS_FILENAME).exists():
        return {}
    raw_content = Path(VERSIONS_FILENAME).read_text().strip()
    if not raw_content:
        return {}
    return json.loads(raw_content)


def git_bot_config():
    subprocess.run(["git", "config", "user.name", GIT_BOT_USERNAME], check=True)
    subprocess.run(["git", "config", "user.email", GIT_BOT_EMAIL], check=True)


def git_commit_if_dirty(commit_message):
    diff_result = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff_result.returncode != 0:
        subprocess.run(["git", "commit", "-m", commit_message], check=True)


def git_commit_versions_and_push(saved_versions, commit_message):
    Path(VERSIONS_FILENAME).write_text(json.dumps(saved_versions, indent=2))
    git_bot_config()
    subprocess.run(["git", "add", VERSIONS_FILENAME], check=True)
    subprocess.run(["git", "commit", "-m", commit_message], check=False)
    subprocess.run(["git", "push"], check=True)


def resolve_build_mode(cli_build_mode, app_entry, config_key, default_mode):
    if cli_build_mode and cli_build_mode != "pre-release":
        return cli_build_mode
    return app_entry.get(config_key) or default_mode


def build_init():
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--source")
    arg_parser.add_argument("--mode")
    arg_parser.add_argument("--dry-run", action="store_true")
    parsed_args = arg_parser.parse_args()

    build_source = parsed_args.source
    build_mode = parsed_args.mode
    dry = parsed_args.dry_run

    log_plain_section("Build Start")
    log_kv("Mode", build_mode)
    log_kv("Source", build_source or "all")
    log_kv("Dry Run", dry)
    log_space()

    github_pat = os.environ.get("PEACHMEOW_GITHUB_PAT")

    if not github_pat:
        log_space()
        github_pat = getpass.getpass(
            "Enter your GitHub PAT or press Enter to skip: "
        ).strip()
        log_space()

    if github_pat:
        log_done("PAT accepted")
    else:
        log_info("No PAT entered. Running unauthenticated (may hit rate limits)")

    auth_headers = {"Authorization": f"token {github_pat}"} if github_pat else {}

    return build_source, build_mode, dry, auth_headers


def parse_global_striplibs(config):
    for patcher_arg in shlex.split(config.get("patcher-args", "")):
        if patcher_arg.startswith("--striplibs="):
            return patcher_arg
    return ""


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


def fetch_and_merge_apk(
    package_name, app_release, downloaded_apks_cache, app_table_name
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
        log_sub("Merging")
        run(
            [
                "java",
                "-jar",
                "tools/apkeditor.jar",
                "m",
                "-f",
                "-i",
                downloaded_file,
                "-o",
                merged_apk_path,
            ]
        )
        os.remove(downloaded_file)

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


def build_release_notes(
    built_apps, used_patches_versions, built_cli_version, built_cli_repo, auth_headers
):
    patches_repo = list(used_patches_versions.keys())[0]
    patches_version = list(used_patches_versions.values())[0]

    patches_release = gh(
        f"https://api.github.com/repos/{patches_repo}/releases/tags/{patches_version}",
        headers=auth_headers,
    )
    changelog = patches_release.get("body") or ""
    is_prerelease = patches_release.get("prerelease", False)

    note_lines = []
    apps_grouped = {}
    for app_table_name, _, app_version_str, variant, app_display_name in built_apps:
        apps_grouped.setdefault(app_display_name, []).append((variant, app_version_str))

    has_variants = any(
        len(items) > 1 or (len(items) == 1 and items[0][0] is not None)
        for items in apps_grouped.values()
    )

    total_apps = len(apps_grouped)

    priority = ["youtube", "music"]

    def app_sort_key(app_name):
        if app_name.lower() in priority:
            return (0, priority.index(app_name.lower()))
        return (1, app_name.lower())

    section_heading = "## App Version\n" if total_apps == 1 else "## App Versions\n"
    note_lines.append(section_heading)

    if not has_variants:
        for app_name in sorted(apps_grouped.keys(), key=app_sort_key):
            _, app_version_str = apps_grouped[app_name][0]
            note_lines.append(f"{app_name}: {strip_v(app_version_str)}")
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

    patches_repo_link = f"[{patches_repo}](https://github.com/{patches_repo})"
    cli_repo_link = (
        f"[{built_cli_repo}](https://github.com/{built_cli_repo})"
        if built_cli_repo
        else built_cli_repo
    )

    note_lines.append("## Build Info\n")
    note_lines.append(f"- Patches: {patches_repo_link}")
    note_lines.append(f"- Patches Version: {strip_v(patches_version)}")
    note_lines.append(f"- CLI: {cli_repo_link}")
    note_lines.append(f"- CLI Version: {strip_v(built_cli_version)}")
    note_lines.append("")
    note_lines.append("## Changelog\n")
    note_lines.append(changelog)

    return "\n".join(note_lines), patches_version, is_prerelease


def publish_release(built_apps, release_notes, is_prerelease):
    release_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    release_tag = "peachmeow-" + release_time.strftime("%Y%m%d-%H%M%S-%f")
    release_title = f"🐱 PeachMeow · {release_time.strftime('%Y-%m-%d')} · {release_time.strftime('%H:%M')}"

    Path("release.md").write_text(release_notes)

    existing_tag_check = subprocess.run(
        ["gh", "release", "view", release_tag],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if existing_tag_check.returncode == 0:
        subprocess.run(["gh", "release", "delete", release_tag, "-y"], check=False)
        subprocess.run(
            ["git", "push", "origin", f":refs/tags/{release_tag}"], check=False
        )
        subprocess.run(
            ["git", "tag", "-d", release_tag], check=False, stderr=subprocess.DEVNULL
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
    used_patches_versions, built_cli_version, patches_source_order, is_prerelease
):
    patches_repo = list(used_patches_versions.keys())[0]
    patches_version = list(used_patches_versions.values())[0]

    subprocess.run(["git", "fetch", "origin", GIT_STATE_BRANCH], check=False)

    state_branch_check = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", GIT_STATE_BRANCH],
        capture_output=True,
        text=True,
    )

    if state_branch_check.stdout.strip() == "":
        subprocess.run(["git", "checkout", "--orphan", GIT_STATE_BRANCH], check=True)
        subprocess.run(["git", "rm", "-rf", "."], check=False)
        subprocess.run(["git", "clean", "-fd"], check=False)

        if not Path(VERSIONS_FILENAME).exists():
            Path(VERSIONS_FILENAME).write_text("{}\n")

        git_bot_config()
        subprocess.run(["git", "add", VERSIONS_FILENAME], check=True)
        subprocess.run(["git", "commit", "-m", GIT_STATE_INIT_MESSAGE], check=True)
        subprocess.run(["git", "push", "-u", "origin", GIT_STATE_BRANCH], check=True)
        subprocess.run(["git", "fetch", "origin", GIT_STATE_BRANCH], check=False)

    if state_branch_check.stdout.strip() != "":
        subprocess.run(["git", "reset", "--hard"], check=True)
        subprocess.run(["git", "clean", "-fd"], check=True)
        subprocess.run(
            ["git", "checkout", "-B", GIT_STATE_BRANCH, f"origin/{GIT_STATE_BRANCH}"],
            check=True,
        )

    stored_versions = {}
    if Path(VERSIONS_FILENAME).exists():
        stored_versions = json.loads(Path(VERSIONS_FILENAME).read_text())

    source_version_entry = stored_versions.setdefault(patches_repo, {})

    if is_prerelease:
        source_version_entry["pre-release"] = {
            "patches": patches_version,
            "cli": built_cli_version,
        }
    else:
        source_version_entry["latest"] = {
            "patches": patches_version,
            "cli": built_cli_version,
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
        for key in channels:
            if key not in ordered_channels:
                ordered_channels[key] = channels[key]
        ordered_stored_versions[patches_src] = ordered_channels

    Path(VERSIONS_FILENAME).write_text(json.dumps(ordered_stored_versions, indent=2))

    git_bot_config()
    subprocess.run(["git", "add", VERSIONS_FILENAME], check=True)

    commit_message = f"release: {patches_repo} → {patches_version}"
    git_commit_if_dirty(commit_message)

    for _ in range(5):
        rebase_result = subprocess.run(
            ["git", "pull", "--rebase", "origin", GIT_STATE_BRANCH]
        )

        if rebase_result.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], check=False)
            subprocess.run(["git", "reset", "--hard", f"origin/{GIT_STATE_BRANCH}"])
            subprocess.run(["git", "add", VERSIONS_FILENAME], check=True)
            git_commit_if_dirty(commit_message)

        push_result = subprocess.run(["git", "push", "origin", GIT_STATE_BRANCH])
        if push_result.returncode == 0:
            break


def _extract_patches_source_from_release_body(body):
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


def _extract_patches_version_from_release_body(body):
    if not body:
        return None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- Patches Version:"):
            return stripped[len("- Patches Version:") :].strip() or None
    return None


def _delete_release_and_tag(tag_name):
    subprocess.run(
        ["gh", "release", "delete", tag_name, "--yes"],
        check=False,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "push", "origin", f":refs/tags/{tag_name}"],
        check=False,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "tag", "-d", tag_name],
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

    releases_by_source = {}
    for release in all_releases:
        source = _extract_patches_source_from_release_body(release.get("body") or "")
        if not source:
            continue
        releases_by_source.setdefault(source, []).append(release)

    for source, releases in releases_by_source.items():
        stable_releases = sorted(
            [r for r in releases if not r["prerelease"]],
            key=lambda r: r.get("published_at") or r.get("created_at") or "",
            reverse=True,
        )
        pre_releases = [r for r in releases if r["prerelease"]]

        for old_release in stable_releases[3:]:
            _delete_release_and_tag(old_release["tag_name"])

        stable_patches_versions = set()
        for r in stable_releases[:3]:
            ver = _extract_patches_version_from_release_body(r.get("body") or "")
            if ver:
                stable_patches_versions.add(ensure_v(ver))

        for pre_release in pre_releases:
            pre_ver = _extract_patches_version_from_release_body(
                pre_release.get("body") or ""
            )
            if not pre_ver:
                continue
            pre_ver_full = ensure_v(pre_ver)
            if "-dev" in pre_ver_full:
                base_version = pre_ver_full.split("-dev")[0]
                if base_version in stable_patches_versions:
                    _delete_release_and_tag(pre_release["tag_name"])


def generate_build_matrix():
    config = load_config()

    PEACHMEOW_GITHUB_PAT = os.environ.get("PEACHMEOW_GITHUB_PAT")
    auth_headers = {}
    if PEACHMEOW_GITHUB_PAT:
        auth_headers["Authorization"] = f"token {PEACHMEOW_GITHUB_PAT}"

    default_patches_repo = config.get("patches-source") or "MorpheApp/morphe-patches"
    default_patches_mode = config.get("patches-version") or "latest"

    app_entries = {k: v for k, v in config.items() if isinstance(v, dict)}

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


def run_resolver():
    log_plain_section("Resolver Start")

    config = load_config()

    PEACHMEOW_GITHUB_PAT = os.environ.get("PEACHMEOW_GITHUB_PAT")

    auth_headers = {}
    if PEACHMEOW_GITHUB_PAT:
        auth_headers["Authorization"] = f"token {PEACHMEOW_GITHUB_PAT}"

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

    default_patches_repo = config.get("patches-source") or "MorpheApp/morphe-patches"
    default_patches_mode = config.get("patches-version") or "latest"

    app_entries = {k: v for k, v in config.items() if isinstance(v, dict)}

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

        for tag in sorted(specific_tags):
            tag_resolved, tag_is_pre = resolve(patches_repo, tag, headers=auth_headers)
            channel_key = "pre-release" if tag_is_pre else "latest"
            prev_tag_version = stored_source_versions.get(channel_key, {}).get(
                "patches"
            )

            if tag_resolved and tag_resolved != prev_tag_version:
                log_version_status(
                    tag,
                    [("Requested", tag), ("Stored", prev_tag_version)],
                    "UPDATE AVAILABLE",
                )
                sources_to_build.append((tag, patches_repo))
            else:
                log_version_status(
                    tag,
                    [("Requested", tag), ("Stored", prev_tag_version)],
                    "NOT FOUND" if not tag_resolved else "UP TO DATE",
                )

    if not sources_to_build:
        log_space()
        log_info("All patches are up to date")
        log_space()
        Path("build_matrix.json").write_text(json.dumps({"include": []}))
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

    log_plain_section("Resolver Complete")
    log_done("Resolver finished successfully")
    log_space()
