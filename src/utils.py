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

DEFAULT_PATCHES_REPO = "MorpheApp/morphe-patches"
DEFAULT_CLI_REPO = "MorpheApp/morphe-cli"
DEFAULT_BRAND = "Morphe"
DEFAULT_VERSION_MODE = "latest"


def die(message):
    print(f"{RED}[ERROR]{RESET} {message}", flush=True)
    sys.exit(1)


def log_space():
    print("", flush=True)


def log_plain_section(title):
    width = 32
    title = title.strip()
    log_space()
    print(f"{NEUTRAL}{'=' * width}{RESET}", flush=True)
    print(f"{AMBER}{title.center(width)}{RESET}", flush=True)
    print(f"{NEUTRAL}{'=' * width}{RESET}", flush=True)
    log_space()


def log_section(title):
    log_space()
    print(f"{AMBER}[BUILD]{RESET} → {AMBER}{title}{RESET}", flush=True)


def log_source(source_repo):
    log_space()
    print(
        f"{PURPLE}\033[1m[SOURCE]\033[0m{RESET} → {PURPLE}\033[1;4m{source_repo}\033[0m{RESET}",
        flush=True,
    )
    log_space()


def log_sub(title):
    log_space()
    print(f"{BLUE}[STEP]{RESET} {title}", flush=True)


def log_kv(key, value):
    print(f"{key}: {value}", flush=True)


def log_info(msg):
    print(f"{YELLOW}[INFO]{RESET} {msg}", flush=True)


def log_done(msg):
    print(f"{GREEN}[DONE]{RESET} {msg}", flush=True)


def log_download(msg):
    print(f"{CYAN}[DOWNLOAD]{RESET} {msg}", flush=True)


def log_retry(msg):
    print(f"{ORANGE}[RETRY]{RESET} {msg}", flush=True)


def log_cache(msg):
    print(f"{PINK}[CACHE]{RESET} {msg}", flush=True)


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


def is_ci_environment():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _is_community_build():
    return not os.environ.get("SIGNING_KEYSTORE_FILE")


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


def read_github_pat_from_env():
    return os.environ.get("PEACHMEOW_GITHUB_PAT")


def build_auth_headers(github_pat):
    return {"Authorization": f"token {github_pat}"} if github_pat else {}


def init():
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--source")
    arg_parser.add_argument("--mode")
    arg_parser.add_argument("--dry-run", action="store_true")
    parsed_args = arg_parser.parse_args()

    target_source = parsed_args.source
    build_mode = parsed_args.mode
    dry_run = parsed_args.dry_run

    github_pat = read_github_pat_from_env()

    if not github_pat:
        log_space()
        github_pat = getpass.getpass(
            "Enter your GitHub PAT or press Enter to skip: "
        ).strip()
        log_space()

    if github_pat:
        if is_ci_environment():
            log_space()
        log_done("PAT accepted")
    else:
        log_info("No PAT entered. Running unauthenticated (may hit rate limits)")

    auth_headers = build_auth_headers(github_pat)

    return target_source, build_mode, dry_run, auth_headers


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


def extract_app_entries(config):
    return {k: v for k, v in config.items() if isinstance(v, dict)}


def get_config_defaults(config):
    return {
        "patches_repo": config.get("patches-source") or DEFAULT_PATCHES_REPO,
        "cli_repo": config.get("cli-source") or DEFAULT_CLI_REPO,
        "brand": config.get("morphe-brand") or DEFAULT_BRAND,
        "patches_mode": config.get("patches-version") or DEFAULT_VERSION_MODE,
        "cli_mode": config.get("cli-version") or DEFAULT_VERSION_MODE,
    }


def parse_global_striplibs(config):
    for patcher_arg in shlex.split(config.get("patcher-args", "")):
        if patcher_arg.startswith("--striplibs="):
            return patcher_arg
    return ""


def strip_v(version):
    return version.lstrip("v") if version else version


def ensure_v(version):
    if not version:
        return version
    return f"v{version.lstrip('v')}"


def resolve_build_mode(build_mode, app_entry, config_key, default_mode):
    effective = app_entry.get(config_key) or default_mode
    if effective not in ("latest", "pre-release"):
        return effective
    if not is_ci_environment():
        return effective
    if build_mode not in (None, "latest", "pre-release"):
        return effective
    if build_mode != "pre-release":
        return "latest"
    return effective


def gh(url, headers=None):
    if headers is None:
        headers = {}
    api_response = requests.get(url, headers=headers, timeout=60)
    if api_response.status_code != 200:
        die(f"GitHub API failed: {url}")
    return api_response.json()


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


def get_latest_asset(assets: List[Dict]) -> Dict:
    if not assets:
        raise ValueError("No assets found")
    return sorted(
        assets,
        key=lambda asset_entry: asset_entry.get("updated_at", "1970-01-01T00:00:00Z"),
        reverse=True,
    )[0]


def gh_blob_to_raw(blob_url):
    if "github.com" in blob_url and "/blob/" in blob_url:
        return blob_url.replace(
            "https://github.com/", "https://raw.githubusercontent.com/"
        ).replace("/blob/", "/")
    return blob_url


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

        log_retry(f"{output} (attempt {attempt + 1}/{retries})")
        time.sleep(2**attempt)

    print(f"{RED}[ERROR]{RESET} Failed to download {output}", flush=True)
    return 1


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
