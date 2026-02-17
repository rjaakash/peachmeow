# CONFIG.md

This document describes ONLY `config.toml`.

It defines mandatory fields, global defaults, and optional per-app overrides.

---

# Mandatory Fields

Every app MUST:

- Be declared as a TOML table
- Define `package-name`
- Define `app-source`

Minimal example:

[YouTube]
package-name = "com.google.android.youtube"
app-source = "rjaakash/peachmeow-store"

---

## App Table

Each app must be declared as a TOML table:

[YouTube]

The table name is mandatory.

If `app-name` is not set, this table name is used for output filenames.

---

## package-name (MANDATORY)

package-name = "com.google.android.youtube"

Android package ID.

Used to match compatible versions from patches-list.

---

## app-source (MANDATORY)

app-source = "username/repository"

GitHub repository containing base APK releases.

Only GitHub is supported.

This repo MUST publish releases like:

YouTube-19.05.36
Music-7.16.53

Assets MUST be named:

YouTube-19.05.36.apk
Music-7.16.53.apk

APKM is also supported.

If APK exists → used directly  
If APKM exists → merged automatically via APKEditor  

This naming format is user responsibility.

---

# Global Default Values

All global fields are optional.

If missing or empty ("") these defaults apply:

enabled = true
patches-source = "MorpheApp/morphe-patches"
cli-source = "MorpheApp/morphe-cli"
morphe-brand = "Morphe"
patches-version = "latest"
cli-version = "latest"
patches-list = "https://github.com/MorpheApp/morphe-patches/blob/main/patches-list.json"
version = "auto"

---

## enabled

true  → build  
false → skip  

---

## patches-source

GitHub repo providing patch releases (.mpp)

Format:

username/repository

---

## cli-source

GitHub repo providing Morphe CLI

Format:

username/repository

---

## morphe-brand

Brand used in final APK filename.

Can be anything:

Morphe  
Anddea  
RVX  
Peach  

---

## patches-version

Which patch version to use:

latest  → newest stable  
dev     → newest prerelease  
all     → newest regardless  
X.Y.Z   → exact version (example: 4.0.0)

---

## cli-version

Same options as patches-version, but for CLI.

---

## patches-list

Patch compatibility list.

Used only when version = auto.

Blob URLs are converted to raw automatically.

---

## version

Controls base APK version.

auto:

1. Reads patches-list  
2. Finds compatible versions for package-name  
3. Reads app-source releases  
4. Picks highest common version  

Manual override:

version = "19.05.36"

If manually set, auto logic is skipped.

---

# App Options (Optional / Overrides)

These fields may be set per app and override global defaults:

app-name  
enabled  
patches-source  
cli-source  
morphe-brand  
patches-version  
cli-version  
patches-list  
version  
patcher-args  

If omitted or empty, global values apply.

---

## app-name (optional)

Used only for:

- APK filename
- Release notes

If not set, table name is used.

---

## patcher-args (optional)

patcher-args = """
-e "Custom branding name for YouTube"
-OappIcon=xisr_yellow
"""

Raw Morphe CLI arguments.

Passed directly to CLI.

---

# APK Output Naming

Final APK filename:

<AppName>-v<AppVersion>-<Brand>-v<PatchVersion>.apk

Examples:

YouTube-v19.05.36-Anddea-v4.0.0-dev.3.apk  
Music-v7.16.53-Morphe-v4.0.0.apk  

If app-name is not set, table name is used.

---

End of config.toml documentation.