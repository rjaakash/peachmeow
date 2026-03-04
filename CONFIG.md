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

```toml
[YouTube]
package-name = "com.google.android.youtube"
app-source = "rjaakash/peachmeow-store"
```

---

## App Table

Each app must be declared as a TOML table:

```toml
[YouTube]
```

The table name itself is mandatory.

If the table name does NOT contain spaces, quotes are not required:

```toml
[YouTube]
[Music]
[Reddit]
```

If the table name contains spaces, it must be wrapped in quotes:

```toml
["YouTube Anddea"]
["Music Anddea"]
["YouTube RVX Morphed AFN Blue"]
```

---

## package-name (MANDATORY)

```toml
package-name = "com.google.android.youtube"
```

Android package ID.

Used to match compatible versions from patches-list.

---

## app-source (MANDATORY)

```toml
app-source = "username/repository"
```

GitHub repository containing base APK releases.

Only GitHub is supported.

This repo MUST publish releases like:

```
YouTube-19.05.36
Music-7.16.53
```

Assets MUST be named:

```
YouTube-19.05.36.apk
Music-7.16.53.apk
```

APKM is also supported.

Build logic:

1. APK is attempted first  
2. If APK download fails → APKM is downloaded  
3. APKM is merged automatically using APKEditor  

This naming format is user responsibility.

---


---

# Global Default Values

All global fields are optional.

If a global field is missing or empty, these defaults apply:

```toml
patches-source = "MorpheApp/morphe-patches"
cli-source = "MorpheApp/morphe-cli"
morphe-brand = "Morphe"
patches-version = "latest"
cli-version = "latest"
```

---

## patches-source

GitHub repo providing patch releases (.mpp)

Format:

```
username/repository
```

---

## cli-source

GitHub repo providing Morphe CLI

Format:

```
username/repository
```

---

## morphe-brand

Brand used in:

- Final APK filename
- GitHub release tag
- GitHub release title

Examples:

```
Morphe
Anddea
RVX
Peach
```

---

## patches-version / cli-version

Version selector logic:

```
latest     → newest stable release (non-prerelease)
dev        → newest prerelease ONLY
all        → newest release (stable or prerelease)
<any tag>  → exact release tag (example: 4.0.0 or 4.0.0-dev.3)
```

Both `patches-version` and `cli-version` use the same rules.

---


---

# App Options (Optional / Overrides)

These fields can be defined per app and override global defaults:

```
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
variant
```

If omitted or empty, global values apply.

---

## app-name (optional)

Used for:

- APK output filename
- Release notes section headers

If not set, the table name is used.

---

## enabled

```toml
enabled = true
```

true  → build  
false → skip  

enabled = false skips the app before --source filtering is applied.

---

## variant (optional)

```toml
variant = "AFN-Blue"
```

Adds a variant label to the APK filename and changes release notes into grouped “per-app versions” format.

---

## patcher-args (optional)

```toml
patcher-args = """
-e "Custom branding name for YouTube"
-OappIcon=xisr_yellow
"""
```

Raw Morphe CLI arguments.

Passed directly to CLI.

---

## patches-list

Patch compatibility list.

Used only when `version = auto`.

If defined inside an app table, it overrides the automatic fetch.

If `patches-list` is not defined inside an app table, it is fetched automatically from the patch repository:

```
https://raw.githubusercontent.com/<patches-source>/<branch>/patches-list.json
```

`<branch>` is selected automatically based on the resolved patch release:

- `dev` if the selected patch release is a prerelease
- `main` if the selected patch release is stable

This applies to:

- `patches-version = dev`
- `patches-version = all`
- exact tags (example: `4.0.0-dev.3`)

If `patches-list` is explicitly set to a GitHub blob URL, it is converted to raw automatically and branch logic is skipped.

---

## version

Controls base APK version.

If not set, defaults to `auto`.

`auto`:

1. Reads patches-list  
2. Finds compatible versions for package-name  
3. Reads app-source releases  
4. Picks highest common version  

Manual override:

```toml
version = "19.05.36"
```

If manually set, auto logic is skipped.

---


---

# APK Output Naming

When version = auto:

```
<AppName>-<Brand>-v<PatchVersion>.apk
```

Examples:

```
YouTube-Anddea-v4.0.0-dev.3.apk  
Music-Morphe-v4.0.0.apk
```

When version is manually set:

```
<AppName>-v<AppVersion>-<Brand>-v<PatchVersion>.apk
```

Examples:

```
YouTube-v19.05.36-Anddea-v4.0.0-dev.3.apk  
Music-v7.16.53-Morphe-v4.0.0.apk
```

If variant is set:

```
YouTube-Anddea-AFN-Blue-v4.0.0-dev.3.apk  
Music-v7.16.53-Anddea-AFN-Blue-v4.0.0.apk
```

---

End of config.toml documentation.
