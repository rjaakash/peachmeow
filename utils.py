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

CONFIG_FILE = "config.toml"
VERSIONS_FILE = "versions.json"
STATE_BRANCH = "state"
INIT_MSG = "state: initial 🐱 PeachMeow metadata"
GIT_BOT_NAME = "github-actions[bot]"
GIT_BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


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


def _is_community_build():
    return not os.environ.get("SIGNING_KEYSTORE_FILE")


def require_env(n):
    v = os.environ.get(n)
    if v:
        return v

    if _is_community_build():
        if n == "SIGNING_KEYSTORE_FILE":
            return "peachmeow-community.bks"
        if n == "SIGNING_KEYSTORE_PASSWORD":
            return "7777777"
        if n == "SIGNING_KEY_ALIAS":
            return "RJ"
        if n == "SIGNING_KEY_PASSWORD":
            return "7777777"

    die(f"missing env: {n}")


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

    return sorted(
        assets, key=lambda x: x.get("updated_at", "1970-01-01T00:00:00Z"), reverse=True
    )[0]


def resolve(repo, mode, headers=None, strict=False):
    if headers is None:
        headers = {}

    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases",
        headers=headers,
        timeout=60,
    )

    if r.status_code != 200:
        if strict:
            die(f"GitHub API failed: {repo}")
        return None, False

    rel = r.json()

    if not rel:
        if strict:
            die(f"No releases found for {repo}")
        return None, False

    if mode == "latest":
        for x in rel:
            if not x["prerelease"]:
                return x["tag_name"], False
        if strict:
            die(f"No stable release found for {repo}")
        return None, False

    if mode == "dev":
        for x in rel:
            if x["prerelease"]:
                return x["tag_name"], True
        if strict:
            die(f"No prerelease found for {repo}")
        return None, True

    if mode == "all":
        x = rel[0]
        return x["tag_name"], x["prerelease"]

    for x in rel:
        if x["tag_name"] == mode:
            return x["tag_name"], x["prerelease"]

    if strict:
        die(f"Version '{mode}' not found for {repo}")
    return None, False


def resolve_channels(repo, headers=None):
    if headers is None:
        headers = {}

    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases",
        headers=headers,
        timeout=60,
    )
    if r.status_code != 200:
        die(f"Failed to fetch {repo}")

    rel = r.json()
    latest = None
    dev = None

    for x in rel:
        if x["prerelease"]:
            if dev is None:
                dev = x["tag_name"]
        else:
            if latest is None:
                latest = x["tag_name"]

    return latest, dev


def gh(url, headers=None):
    if headers is None:
        headers = {}
    r = requests.get(url, headers=headers, timeout=60)
    if r.status_code != 200:
        die(f"GitHub API failed: {url}")
    return r.json()


def load_config():
    if not Path(CONFIG_FILE).exists():
        die("config.toml missing")
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def load_versions():
    if not Path(VERSIONS_FILE).exists():
        return {}
    txt = Path(VERSIONS_FILE).read_text().strip()
    if not txt:
        return {}
    return json.loads(txt)


def git_bot_config():
    subprocess.run(["git", "config", "user.name", GIT_BOT_NAME], check=True)
    subprocess.run(["git", "config", "user.email", GIT_BOT_EMAIL], check=True)


def git_commit_if_dirty(msg):
    r = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if r.returncode != 0:
        subprocess.run(["git", "commit", "-m", msg], check=True)


def build_init():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source")
    ap.add_argument("--mode")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    build_source = args.source
    build_mode = args.mode
    dry = args.dry_run

    log_plain_section("Build Start")
    log_kv("Mode", build_mode)
    log_kv("Source", build_source or "all")
    log_kv("Dry Run", dry)

    pat = os.environ.get("PEACHMEOW_GITHUB_PAT")

    if not pat:
        log_space()
        pat = getpass.getpass("Enter your GitHub PAT or press Enter to skip: ").strip()
        log_space()

    if pat:
        log_done("PAT accepted")
    else:
        log_info("No PAT entered. Running unauthenticated (may hit rate limits)")

    headers = {"Authorization": f"token {pat}"} if pat else {}

    return build_source, build_mode, dry, headers


def parse_global_striplibs(cfg):
    for t in shlex.split(cfg.get("patcher-args", "")):
        if t.startswith("--striplibs="):
            return t
    return ""


def get_apkeditor_url(headers):
    for r in gh(
        "https://api.github.com/repos/REAndroid/APKEditor/releases", headers=headers
    ):
        if not r["prerelease"]:
            for a in r["assets"]:
                if a["name"].lower().endswith(".jar"):
                    return a["browser_download_url"]
    return ""


def fetch_patches_list(src, is_pre, app_cfg, pj_cache):
    if app_cfg.get("patches-list"):
        plist = gh_blob_to_raw(app_cfg.get("patches-list"))
    else:
        branch = "dev" if is_pre else "main"
        plist = f"https://raw.githubusercontent.com/{src}/{branch}/patches-list.json"

    if plist in pj_cache:
        log_cache(f"Patches-list: {plist}")
        return pj_cache[plist], plist

    pj = requests.get(plist, timeout=60).json()
    pj_cache[plist] = pj
    return pj, plist


def resolve_app_version(table, pkg, name, repo, pj, headers):
    compat = set()

    for p in pj.get("patches", []):
        cp = p.get("compatiblePackages")
        if not isinstance(cp, dict):
            continue
        if pkg in cp:
            versions = cp[pkg]
            if versions:
                compat |= set(versions)

    rel = gh(
        f"https://api.github.com/repos/{repo}/releases?per_page=100", headers=headers
    )

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
        except Exception:
            continue

    if compat:
        cand = sorted(set(compat) & set(avail), key=Version)
        if not cand:
            die(f"{table}: no compatible versions found")
    else:
        cand = sorted(avail, key=Version)

    if not cand:
        die(f"{table}: no versions available")

    return cand[-1]


def build_output_filename(name, app_version, brand, variant, patch_version):
    parts = [name]

    norm = app_version.strip()
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

    parts.append(patch_version)

    return "-".join(parts) + ".apk"


def download_tool(label, src, version, ext, tool_dir, seen, seen_files, dry, headers):
    key = (src, version)

    if key in seen:
        filename = seen_files[key]
        filepath = f"{tool_dir}/{filename}"
        log_cache(f"{label}: {filename}")
        return filepath, filename

    rel = gh(
        f"https://api.github.com/repos/{src}/releases/tags/{version}",
        headers=headers,
    )
    candidates = [a for a in rel.get("assets", []) if a["name"].endswith(ext)]

    if not candidates:
        die(f"{label} {ext} not found for {version}")

    selected = get_latest_asset(candidates)
    url = selected["browser_download_url"]
    filename = selected["name"]
    filepath = f"{tool_dir}/{filename}"

    log_sub(label)
    log_kv(f"{label} File", filename)

    if not dry:
        if download_with_retry(url, filepath) != 0:
            die(f"{label} download failed")

    seen.add(key)
    seen_files[key] = filename

    return filepath, filename


def fetch_and_merge_apk(pkg, rel, seen_unpatched_apps, table):
    apk_assets = [a for a in rel.get("assets", []) if a["name"].endswith(".apk")]
    apkm_assets = [a for a in rel.get("assets", []) if a["name"].endswith(".apkm")]

    if apk_assets:
        url = get_latest_asset(apk_assets)["browser_download_url"]
    elif apkm_assets:
        url = get_latest_asset(apkm_assets)["browser_download_url"]
    else:
        die(table)

    app_filename = os.path.basename(url)
    pkg_dir = f"unpatched/{pkg}"
    os.makedirs(pkg_dir, exist_ok=True)
    key = f"{pkg}/{app_filename}"

    if key in seen_unpatched_apps:
        log_cache(f"App: {key}")
        return seen_unpatched_apps[key]

    file_path = f"{pkg_dir}/{app_filename}"

    if download_with_retry(url, file_path) != 0:
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
    return out


def patch_app(cli_file, patch_file, out, final, app_cfg, global_striplibs):
    app_args = shlex.split(app_cfg.get("patcher-args", ""))
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
            require_env("SIGNING_KEYSTORE_PASSWORD"),
            "--keystore-entry-alias",
            require_env("SIGNING_KEY_ALIAS"),
            "--keystore-entry-password",
            require_env("SIGNING_KEY_PASSWORD"),
            "-p",
            patch_file,
            "-o",
            f"build/{final}",
            "--purge",
            out,
        ]
        + args_final
    )


def build_release_notes(built, used_patch_versions, cli_version, headers):
    patch_src = list(used_patch_versions.keys())[0]
    patch_ver = list(used_patch_versions.values())[0]

    rel = gh(
        f"https://api.github.com/repos/{patch_src}/releases/tags/{patch_ver}",
        headers=headers,
    )
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

    lines.append("## App Versions\n")

    if not has_variants:
        for app in sorted(grouped.keys(), key=app_sort_key):
            _, appv = grouped[app][0]
            lines.append(f"{app.replace('-', ' ')}: {appv}")
        lines.append("")
    else:
        for app in sorted(grouped.keys(), key=app_sort_key):
            lines.append(f"### {app.replace('-', ' ')}")
            items = grouped[app]

            def variant_sort_key(item):
                v, _ = item
                return (0, "") if v is None else (1, v.lower())

            for variant, appv in sorted(items, key=variant_sort_key):
                if len(items) == 1 and variant is None:
                    lines.append(f"- {appv}")
                elif variant is None:
                    lines.append(f"- Base: {appv}")
                else:
                    lines.append(f"- {variant.replace('-', ' ')}: {appv}")

            lines.append("")

    lines.append("## Build Info\n")
    lines.append(f"- Patch: {patch_ver}")
    lines.append(f"- CLI: {cli_version}")
    lines.append("")
    lines.append("## Patch Changelog\n")
    lines.append(changelog)

    return "\n".join(lines), patch_ver, is_prerelease


def publish_release(built, release_notes, is_prerelease):
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    tag = "peachmeow-" + now.strftime("%Y%m%d-%H%M%S-%f")
    release_name = (
        f"🐱 PeachMeow · {now.strftime('%Y-%m-%d')} · {now.strftime('%H:%M')}"
    )

    Path("release.md").write_text(release_notes)

    check = subprocess.run(
        ["gh", "release", "view", tag],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if check.returncode == 0:
        subprocess.run(["gh", "release", "delete", tag, "-y"], check=False)
        subprocess.run(["git", "push", "origin", f":refs/tags/{tag}"], check=False)
        subprocess.run(
            ["git", "tag", "-d", tag], check=False, stderr=subprocess.DEVNULL
        )

    cmd = ["gh", "release", "create", tag, "-t", release_name, "-F", "release.md"] + [
        f"build/{x}" for _, x, _, _ in built
    ]

    if is_prerelease:
        cmd.append("--prerelease")

    subprocess.run(cmd, check=True)


def update_state(used_patch_versions, cli_version, source_order, is_prerelease):
    patch_src = list(used_patch_versions.keys())[0]
    patch_ver = list(used_patch_versions.values())[0]

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

        git_bot_config()
        subprocess.run(["git", "add", VERSIONS_FILE], check=True)
        subprocess.run(["git", "commit", "-m", INIT_MSG], check=True)
        subprocess.run(["git", "push", "-u", "origin", STATE_BRANCH], check=True)
        subprocess.run(["git", "fetch", "origin", STATE_BRANCH], check=False)

    if remote_check.stdout.strip() != "":
        subprocess.run(["git", "reset", "--hard"], check=True)
        subprocess.run(["git", "clean", "-fd"], check=True)
        subprocess.run(
            ["git", "checkout", "-B", STATE_BRANCH, f"origin/{STATE_BRANCH}"],
            check=True,
        )

    versions = {}
    if Path(VERSIONS_FILE).exists():
        versions = json.loads(Path(VERSIONS_FILE).read_text())

    entry = versions.setdefault(patch_src, {})

    if is_prerelease:
        entry["dev"] = {"patch": patch_ver, "cli": cli_version}
    else:
        entry["latest"] = {"patch": patch_ver, "cli": cli_version}

    ordered_versions = {}
    for src in source_order:
        if src in versions:
            ordered_versions[src] = versions[src]
    for src in versions:
        if src not in ordered_versions:
            ordered_versions[src] = versions[src]

    Path(VERSIONS_FILE).write_text(json.dumps(ordered_versions, indent=2))

    git_bot_config()
    subprocess.run(["git", "add", VERSIONS_FILE], check=True)

    msg = f"release: {patch_src} → {patch_ver}"
    git_commit_if_dirty(msg)

    for _ in range(5):
        r = subprocess.run(["git", "pull", "--rebase", "origin", STATE_BRANCH])

        if r.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], check=False)
            subprocess.run(["git", "reset", "--hard", f"origin/{STATE_BRANCH}"])
            subprocess.run(["git", "add", VERSIONS_FILE], check=True)
            git_commit_if_dirty(msg)

        push = subprocess.run(["git", "push", "origin", STATE_BRANCH])
        if push.returncode == 0:
            break


def resolver_cleanup_stale(old, active, state_exists, versions_file_existed):
    removed_sources = []

    for k in list(old.keys()):
        if k not in active:
            log_sub("Cleanup")
            log_info(f"Removing stale patch source: {k}")
            old.pop(k)
            removed_sources.append(k)

    if removed_sources and state_exists and versions_file_existed:
        Path(VERSIONS_FILE).write_text(json.dumps(old, indent=2))
        git_bot_config()
        subprocess.run(["git", "add", VERSIONS_FILE], check=True)

        if len(removed_sources) == 1:
            msg = f"delete: stale patch source → {removed_sources[0]}"
        else:
            msg = "delete: stale patch sources → " + ", ".join(removed_sources)

        subprocess.run(["git", "commit", "-m", msg], check=False)
        subprocess.run(["git", "push"], check=True)

    return bool(removed_sources)


def resolver_commit_channel_cleanup(
    old, removed_channels, state_exists, versions_file_existed
):
    if not (removed_channels and state_exists and versions_file_existed):
        return

    Path(VERSIONS_FILE).write_text(json.dumps(old, indent=2))
    git_bot_config()
    subprocess.run(["git", "add", VERSIONS_FILE], check=True)

    if len(removed_channels) == 1:
        msg = f"delete: unused version channel → {removed_channels[0]}"
    else:
        msg = "delete: unused version channels → " + ", ".join(removed_channels)

    subprocess.run(["git", "commit", "-m", msg], check=False)
    subprocess.run(["git", "push"], check=True)


def trigger(src, mode=None):
    log_sub("Trigger Build")
    log_source(src)

    display_mode = mode if mode else "None"
    log_kv("Mode", display_mode)

    cmd = ["gh", "workflow", "run", "build.yml", "-f", f"source={src}"]
    if mode:
        cmd += ["-f", f"mode={mode}"]

    subprocess.run(cmd, check=True)


def run_resolver():
    log_plain_section("Resolver Start")

    cfg = load_config()

    PEACHMEOW_GITHUB_PAT = os.environ.get("PEACHMEOW_GITHUB_PAT")

    HEADERS = {}
    if PEACHMEOW_GITHUB_PAT:
        HEADERS["Authorization"] = f"token {PEACHMEOW_GITHUB_PAT}"

    subprocess.run(["git", "fetch", "origin", STATE_BRANCH], check=False)

    remote_check = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", STATE_BRANCH],
        capture_output=True,
        text=True,
    )

    state_exists = remote_check.stdout.strip() != ""

    if not state_exists:
        old = {}
        versions_file_existed = False
    else:
        subprocess.run(
            ["git", "checkout", "-B", STATE_BRANCH, f"origin/{STATE_BRANCH}"],
            check=True,
        )
        versions_file_existed = Path(VERSIONS_FILE).exists()
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

    active = set(sources.keys())

    source_dirty = False
    channel_dirty = False
    removed_sources = []
    removed_channels = []

    for k in list(old.keys()):
        if k not in active:
            log_sub("Cleanup")
            log_info(f"Removing stale patch source: {k}")
            old.pop(k)
            removed_sources.append(k)
            source_dirty = True

    if source_dirty and state_exists and versions_file_existed:
        Path(VERSIONS_FILE).write_text(json.dumps(old, indent=2))
        git_bot_config()
        subprocess.run(["git", "add", VERSIONS_FILE], check=True)

        if len(removed_sources) == 1:
            msg = f"delete: stale patch source → {removed_sources[0]}"
        else:
            msg = "delete: stale patch sources → " + ", ".join(removed_sources)

        subprocess.run(["git", "commit", "-m", msg], check=False)
        subprocess.run(["git", "push"], check=True)

    changed = []
    log_sub("Check")

    for src, mode in sources.items():

        stored = old.get(src, {})

        if mode == "latest":
            if "dev" in stored:
                stored.pop("dev")
                channel_dirty = True
                removed_channels.append(src)
        elif mode == "dev":
            if "latest" in stored:
                stored.pop("latest")
                channel_dirty = True
                removed_channels.append(src)
        elif mode != "all":
            if "latest" in stored and "dev" in stored:
                stored.pop("dev")
                channel_dirty = True
                removed_channels.append(src)

        if mode != "all":

            latest, is_pre = resolve(src, mode, headers=HEADERS)

            if is_pre:
                prev_version = stored.get("dev", {}).get("patch")
            else:
                prev_version = stored.get("latest", {}).get("patch")

            log_source(src)

            if mode in ["latest", "dev"]:
                status = (
                    "SKIPPED"
                    if latest is None
                    else (
                        "UPDATE AVAILABLE" if latest != prev_version else "UP TO DATE"
                    )
                )
            else:
                status = (
                    "NOT FOUND"
                    if latest is None
                    else (
                        "UPDATE AVAILABLE" if latest != prev_version else "UP TO DATE"
                    )
                )

            if mode in ["latest", "dev"]:
                log_version_status(
                    mode,
                    [
                        ("Upstream", latest),
                        ("Stored", prev_version),
                    ],
                    status,
                )
            else:
                log_version_status(
                    mode,
                    [
                        ("Requested", mode),
                        ("Stored", prev_version),
                    ],
                    status,
                )

            if latest and latest != prev_version:
                changed.append(src)

            continue

        latest_stable, latest_dev = resolve_channels(src, headers=HEADERS)

        stored_latest = stored.get("latest", {}).get("patch")
        stored_dev = stored.get("dev", {}).get("patch")

        log_source(src)

        if latest_stable is None and latest_dev is None:
            status = "SKIPPED"
        else:
            stable_changed = latest_stable and (
                stored_latest is None or Version(latest_stable) > Version(stored_latest)
            )

            dev_changed = latest_dev and latest_dev != stored_dev

            if stable_changed:
                status = "UPDATE AVAILABLE"
            elif dev_changed:
                dev_base = latest_dev.split("-dev", 1)[0]
                if stored_latest and Version(dev_base) <= Version(stored_latest):
                    status = "UP TO DATE"
                else:
                    status = "UPDATE AVAILABLE"
            else:
                status = "UP TO DATE"

        log_version_status(
            "all",
            [
                ("Stable Upstream", latest_stable),
                ("Stable Stored", stored_latest),
                ("Dev Upstream", latest_dev),
                ("Dev Stored", stored_dev),
            ],
            status,
        )

        stable_changed = latest_stable and (
            stored_latest is None or Version(latest_stable) > Version(stored_latest)
        )

        if stable_changed:
            changed.append(("stable", src))
            continue

        dev_changed = latest_dev and latest_dev != stored_dev

        if dev_changed:
            dev_base = latest_dev.split("-dev", 1)[0]
            if stored_latest and Version(dev_base) <= Version(stored_latest):
                continue
            changed.append(("dev", src))

    if not changed:
        log_space()
        log_info("No patch updates")
        log_space()
        return

    log_space()
    count = len(changed)
    log_info(f"Changes detected: {count} patch source" + ("s" if count != 1 else ""))

    for item in changed:
        if isinstance(item, tuple):
            channel, src = item
            if channel == "stable":
                trigger(src, "latest")
            else:
                trigger(src)
        else:
            trigger(item)

    if channel_dirty and removed_channels and state_exists and versions_file_existed:
        Path(VERSIONS_FILE).write_text(json.dumps(old, indent=2))
        git_bot_config()
        subprocess.run(["git", "add", VERSIONS_FILE], check=True)

        if len(removed_channels) == 1:
            msg = f"delete: unused version channel → {removed_channels[0]}"
        else:
            msg = "delete: unused version channels → " + ", ".join(removed_channels)

        subprocess.run(["git", "commit", "-m", msg], check=False)
        subprocess.run(["git", "push"], check=True)

    log_plain_section("Resolver Complete")
    log_done("Resolver finished successfully")
    log_space()
