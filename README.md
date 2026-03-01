# 🐱 PeachMeow — Config‑driven Morphe Patch Builder

PeachMeow is a GitHub‑Actions based patch builder.

You describe everything in `config.toml` — PeachMeow reads it, fetches upstream patches + CLI, patches your apps, and publishes releases automatically.

Cat does the work. You just feed the config. 😼

---

## 📄 Configuration

All configuration lives in `config.toml`.

Full documentation is here:

👉 **[CONFIG.md](https://github.com/rjaakash/peachmeow/blob/main/CONFIG.md)**

Just set up your `config.toml` by following CONFIG.md.

---

## 🚀 Builds

All APKs published in GitHub Releases are **Official 🐱 PeachMeow Builds**.

These are 🐱 PeachMeow releases — they are NOT builds made by upstream projects.

They are built and released by 🌚 [me](https://github.com/rjaakash) via **[GitHub Actions](https://github.com/apps/github-actions)** using upstream sources and tooling.

If you’re just looking for builds:

👉 **[Releases](https://github.com/rjaakash/peachmeow/releases)**  
👉 **[MicroG RE Releases](https://github.com/MorpheApp/MicroG-RE/releases)**

---

## 🔐 Required GitHub Secrets

When you fork the repo, add these secrets:

- `SIGNING_KEYSTORE_B64`  
- `SIGNING_KEYSTORE_PASSWORD`  
- `SIGNING_KEY_ALIAS`  
- `SIGNING_KEY_PASSWORD`  
- `PEACHMEOW_GITHUB_PAT`  

### PAT requirements

`PEACHMEOW_GITHUB_PAT` must be **Fine‑grained**.

Permissions:

- Contents: Read + Write  
- Actions: Read + Write  

---

## 🔑 Keystore

Upload your signing keystore as base64:

```
base64 morphe-release.bks
```

Save output into:

```
SIGNING_KEYSTORE_B64
```

Keystore filename must be:

```
morphe-release.bks
```

---

## ❤️ Credits

- **[Morphe ecosystem](https://github.com/MorpheApp)** — patch framework and CLI tooling  
- **[APKEditor](https://github.com/REAndroid/APKEditor)** — APKM → APK merge utility  
- **[revanced-magisk-module](https://github.com/j-hc/revanced-magisk-module)** — architectural inspiration  

---

🐾 Meow.
