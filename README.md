# Agent Android toolchain

Selectable Android/Bionic tool archives for Agent's existing Shizuku/Sui shell
runtime. The app bundles a Core below 50 MiB per ABI; other archives are downloaded from this
repository's GitHub Releases. This does not install Termux or run apt on-device.

## Build

Use Python 3.11+ and Android NDK 28.2.13676358. Set `ANDROID_NDK_HOME`, or provide
an Android SDK through `ANDROID_SDK_ROOT`/`ANDROID_HOME` so the builder can find
its side-by-side NDK. Native archives are built for arm64-v8a and x86_64.

```sh
python3 -m unittest discover -s tests -v
python3 toolchain/build_packs.py --release v0.1.0
python3 toolchain/build_packs.py --release v0.1.0 --merge
```

`--abi arm64-v8a` selects one ABI. `--pack core` builds Core alone; required packs
are included automatically at build time. A full release requires every pack
for every requested ABI. Outputs are in `dist/`; verified upstream downloads
are cached in `.cache/`. No package installation hooks are executed.

## Packs and shared files

`toolchain/packs.json` declares the pack contents and explicit archive
prerequisites. The builder resolves the pinned upstream package dependencies
and includes all required files beyond those supplied by prerequisite packs.
The app only downloads and extracts archives; it does not resolve upstream
packages or maintain a package-manager database.

| Pack | Contents | Required archives |
| --- | --- | --- |
| Core | Bash, GNU tools, Git, DNS, jq/yq, rg, SQLite, file, xxd, curl/OpenSSL, archives | None |
| Python | Python, pip, Pillow and codecs | Core |
| Images | ImageMagick, image/document formats and fonts | Core |
| Media | FFmpeg, ffprobe and codecs | Core |
| Java | OpenJDK 21 | Core |
| APK tools | JADX, Apktool, smali/baksmali, aapt/aapt2, signing/alignment | Core, Java |
| Android platform | adb/fastboot, filesystem/image tools and Python for scripts | Core |
| Native analysis | LLVM, binutils, radare2 | Core |

Optional archives may contain identical shared libraries. They are produced
from one pinned package set and one customized tree per ABI. Install a coherent
release set in manifest order; mixing independently updated releases is not
supported. The per-pack `requires` list describes archive selection, not apt
dependencies. User overlays are applied after official archives by the app.

Each archive preserves file modes, relative symlinks, resources and original
license notices. `share/agent-toolchain/<pack>/` records package versions,
checksums and source homepages; the platform pack includes the AOSP utility
sources. Native payloads use the existing 31-byte relocation marker. JAR/ZIP
contents are not rewritten.

## Versions and updates

`toolchain/packages-<abi>.json` and `artifacts.json` pin exact versions and
SHA-256 checksums. Ordinary builds never select upstream's latest packages.
Refresh these locks explicitly, then review and test the result:

```sh
python3 toolchain/build_packs.py --update-lock
```

Do not commit caches or generated archives. Upstream may remove old packages;
completed GitHub Release assets preserve the runtime independently of that
upstream retention policy.

## Release workflow and manifest

Push a version tag such as `v0.1.0`, or manually run **Build and release
toolchain** for an existing version tag. CI builds both ABIs, then verifies all
archive hashes/sizes and generates `manifest.json` and `SHA256SUMS`. It uploads
everything to a draft and publishes only after the complete asset set exists.
Prerelease tags are marked as prereleases. Published releases are never
overwritten by the workflow; reruns can resume an unpublished draft.

The workflow uses GitHub's built-in `GITHUB_TOKEN` with write access only in the
publishing job. The optional user PAT in Agent is for app-side API requests and
is independent of CI authentication.

`manifest.json` is an asset of the same versioned GitHub Release as the archives:

```text
https://github.com/dingwen07/agent-android-toolchain/releases/download/v0.1.0/manifest.json
```

The format-2 manifest identifies the release, runtime format, repository and
supported ABIs. Each ABI declares the relocation marker, Python version, and
ordered packs. Each pack supplies its ID, display name, description, required
packs, version-pinned URL, SHA-256, compressed/extracted sizes and commands.
Consumers must reject unsupported formats, select their device ABI, and verify
each archive before preparation. An unavailable update must leave an installed
runtime usable.
