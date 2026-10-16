#!/usr/bin/env python3
"""Build selectable Bionic archives and GitHub release manifests.

No package installation scripts are executed. See README.md for provenance,
relocation, updating the lockfiles, and the user archive format.
"""
import argparse
import concurrent.futures
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import catalog

HERE = Path(__file__).resolve().parent
REPOSITORY = "https://packages.termux.dev/apt/termux-main/"
PREFIX = b"/data/data/com.termux/files/usr"
PLACEHOLDER = b"/__agent_toolchain_prefix__".ljust(len(PREFIX), b"_")
assert len(PREFIX) == len(PLACEHOLDER)
ARCHES = {"arm64-v8a": "aarch64", "x86_64": "x86_64"}
PACKS = catalog.read_catalog(HERE / "packs.json")
ROOTS = sorted({name for pack in PACKS for name in pack["roots"]})


def digest(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def build_lock(cache):
    """Serialize IDE/CLI builds without requiring Unix file locks on Windows."""
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "bundle.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        waiting = False
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if not waiting:
                    print("Waiting for another toolchain build to finish...", flush=True)
                    waiting = True
                time.sleep(1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def fetch(url):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                return response.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(attempt + 1)


def update_lock(abi, index_path=None):
    arch = ARCHES[abi]
    raw = Path(index_path).read_bytes() if index_path else fetch(
        REPOSITORY + f"dists/stable/main/binary-{arch}/Packages.gz")
    packages = {}
    for block in gzip.decompress(raw).decode().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines()
                      if ": " in line and not line.startswith(" "))
        if "Package" in fields:
            packages[fields["Package"]] = fields
    selected = {}

    def add(name):
        if name in selected:
            return
        package = packages[name]
        selected[name] = {key: package[key] for key in (
            "Package", "Version", "Architecture", "Filename", "Size", "SHA256")}
        selected[name]["Homepage"] = package.get("Homepage", "")
        selected[name]["Depends"] = package.get("Depends", "")
        selected[name]["Installed-Size"] = package.get("Installed-Size", "0")
        for dependency in package.get("Depends", "").split(","):
            alternatives = [re.split(r"\s|\(", item.strip())[0]
                            for item in dependency.split("|") if item.strip()]
            if alternatives:
                add(next(item for item in alternatives if item in packages))

    for root in ROOTS:
        add(root)
    lock = {"format": 2, "abi": abi, "repository": REPOSITORY, "roots": ROOTS,
            "index_sha256": digest(raw), "packages": [selected[k] for k in sorted(selected)]}
    (HERE / f"packages-{abi}.json").write_text(json.dumps(lock, indent=2) + "\n")
    print(f"Locked {len(selected)} packages for {abi}", flush=True)


def ar_members(data):
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("Not a Debian ar archive")
    offset = 8
    while offset < len(data):
        header = data[offset:offset + 60]
        size = int(header[48:58])
        name = header[:16].decode().strip().rstrip("/")
        yield name, data[offset + 60:offset + 60 + size]
        offset += 60 + size + size % 2


def unpack_deb(path, root, links, modes):
    payload = next(data for name, data in ar_members(path.read_bytes()) if name.startswith("data.tar"))
    entries = set()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        for member in archive:
            name = member.name.lstrip("./")
            prefix = PREFIX.decode().lstrip("/") + "/"
            if not name.startswith(prefix):
                # Termux package payloads may also contain empty parent directories.
                if member.isdir():
                    continue
                raise ValueError(f"Unexpected package path: {member.name}")
            relative = PurePosixPath(name[len(prefix):])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe package path: {relative}")
            target = root.joinpath(*relative.parts)
            entries.add(relative.as_posix())
            modes[relative.as_posix()] = member.mode & 0o777
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            elif member.issym():
                link = member.linkname
                if link.startswith(PREFIX.decode() + "/"):
                    link = os.path.relpath(root / link[len(PREFIX) + 1:], target.parent)
                if link.startswith("/"):
                    raise ValueError(f"External package symlink: {target}: {link}")
                # Keep links in metadata. Creating host symlinks requires extra privileges
                # on Windows; only the Android extractor needs to create real links.
                links[relative.as_posix()] = link.replace("\\", "/")
            elif member.islnk():
                source = member.linkname.lstrip("./")
                if not source.startswith(prefix):
                    raise ValueError(f"External hard link: {source}")
                shutil.copyfile(root / source[len(prefix):], target)
                target.chmod(member.mode & 0o777)
            elif member.isfile():
                if target.is_symlink():
                    target.unlink()
                with archive.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
            else:
                raise ValueError(f"Unsupported package entry: {member.name}")
    return entries


def materialize_links(root, links):
    def resolve(path, visited):
        if path in visited:
            raise ValueError(f"Cyclic package symlink: {path}")
        target = links.get(path)
        if target is None:
            return root / path
        normalized = os.path.normpath(str(PurePosixPath(path).parent / target)).replace("\\", "/")
        if normalized.startswith("../") or normalized.startswith("/"):
            raise ValueError(f"Package link escapes prefix: {path}")
        return resolve(normalized, visited | {path})
    missing = []
    for name in links:
        source = resolve(name, set())
        target = root / name
        if target.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            shutil.copyfile(source, target)
        else:
            missing.append(f"{name} -> {links[name]}")
    if missing:
        raise ValueError("Dangling package symlinks:\n" + "\n".join(missing))


def native_utilities(root, abi):
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    ndk = os.environ.get("ANDROID_NDK_HOME")
    if not ndk and sdk:
        choices = sorted((Path(sdk) / "ndk").glob("*"))
        if choices:
            ndk = str(choices[-1])
    if not ndk:
        raise RuntimeError("Install an Android NDK and set ANDROID_NDK_HOME or ANDROID_SDK_ROOT (Gradle sets the SDK automatically)")
    host = "windows-x86_64" if os.name == "nt" else "darwin-x86_64" if platform.system() == "Darwin" else "linux-x86_64"
    compiler = Path(ndk) / "toolchains/llvm/prebuilt" / host / "bin"
    target = "aarch64-linux-android28" if abi == "arm64-v8a" else "x86_64-linux-android28"
    suffix = ".exe" if os.name == "nt" else ""
    common = ["--target=" + target, "-O2", "-fPIE", "-pie", "-Wl,-z,max-page-size=16384", "-Wl,-rpath,$ORIGIN/../lib"]
    subprocess.run([str(compiler / ("clang" + suffix)), *common,
                    str(HERE / "native/HprofConv.c"), "-o", str(root / "bin/hprof-conv")], check=True)
    subprocess.run([str(compiler / ("clang++" + suffix)), *common, "-static-libstdc++",
                    "-I" + str(HERE / "native"), "-I" + str(root / "include"),
                    str(HERE / "native/etc1tool.cpp"), str(HERE / "native/etc1.cpp"),
                    "-L" + str(root / "lib"), "-lpng", "-lz", "-o", str(root / "bin/etc1tool")], check=True)
    script(root, "make_f2fs_casefold", 'exec "$AGENT_TOOLCHAIN/bin/make_f2fs" -O casefold,project_quota -C utf8 "$@"')


def install_artifacts(root, cache):
    artifacts = json.loads((HERE / "artifacts.json").read_text())
    for artifact in artifacts:
        path = cache / (artifact["sha256"] + ".jar")
        if not path.exists() or digest(path.read_bytes()) != artifact["sha256"]:
            data = fetch(artifact["url"])
            if digest(data) != artifact["sha256"]:
                raise ValueError(f"Artifact checksum mismatch: {artifact['name']}")
            path.write_bytes(data)
        shutil.copyfile(path, root / "share/java" / (artifact["name"] + ".jar"))
        script(root, artifact["name"], 'exec "$JAVA_HOME/bin/java" -jar "$AGENT_TOOLCHAIN/share/java/' + artifact["name"] + '.jar" "$@"')


def update_artifacts():
    # GitHub's redirect works without API credentials or a package manager.
    with urllib.request.urlopen("https://github.com/baksmali/smali/releases/latest", timeout=30) as response:
        version = response.url.rstrip("/").rsplit("/", 1)[-1].removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"Unrecognized smali release: {version}")
    artifacts = []
    for name in ("smali", "baksmali"):
        url = f"https://github.com/baksmali/smali/releases/download/{version}/{name}-{version}-fat-release.jar"
        artifacts.append({"name": name, "version": version, "url": url,
                          "sha256": digest(fetch(url)), "source": f"https://github.com/baksmali/smali/tree/{version}",
                          "license": "BSD-3-Clause"})
    (HERE / "artifacts.json").write_text(json.dumps(artifacts, indent=2) + "\n")
    print(f"Locked smali/baksmali {version}", flush=True)


def download_package(package, cache, repository):
    path = cache / (package["SHA256"] + ".deb")
    if not path.exists() or digest(path.read_bytes()) != package["SHA256"]:
        data = fetch(repository + package["Filename"])
        if digest(data) != package["SHA256"]:
            raise ValueError(f"Checksum mismatch: {package['Package']}")
        temporary = path.with_suffix(".partial")
        temporary.write_bytes(data)
        temporary.replace(path)
    return path


def script(root, name, text):
    target = root / "bin" / name
    target.write_text("#!/system/bin/sh\n" + text + "\n")
    target.chmod(0o755)


def customize(root):
    # The service writes current network defaults unless the user overlay supplies this file.
    (root / "etc" / "resolv.conf").unlink(missing_ok=True)
    # Use our actual Java/Python environment, never Termux's home or Android's ART.
    if (root / "share/java/apktool.jar").exists():
        script(root, "apktool", '\n'.join([
        'case "${1-}" in b|build)',
        '  native_aapt=1',
        '  for arg in "$@"; do case "$arg" in --aapt|--aapt=*) native_aapt=0;; esac; done',
        '  if [ "$native_aapt" = 1 ]; then set -- "$@" --aapt "$AGENT_TOOLCHAIN/bin/aapt2"; fi;;',
        'esac',
        'exec "$JAVA_HOME/bin/java" -Djava.awt.headless=true -jar "$AGENT_TOOLCHAIN/share/java/apktool.jar" "$@"']))
    if (root / "bin/pip").exists():
        script(root, "pip", 'exec "$AGENT_TOOLCHAIN/bin/python3" -m pip "$@"')
        script(root, "pip3", 'exec "$AGENT_TOOLCHAIN/bin/python3" -m pip "$@"')
    if (root / "lib/jvm").exists():
        for name in ("java", "javac", "jar", "javap", "keytool", "jdeps", "jlink", "jshell"):
            script(root, name, 'exec "$JAVA_HOME/bin/' + name + '" "$@"')
    # Package installation does not run postinst. Materialize OpenSSL's CA bundle.
    certificates = sorted((root / "etc" / "tls" / "certs").glob("*.pem"))
    bundle = root / "etc" / "tls" / "cert.pem"
    if not bundle.exists() and certificates:
        bundle.write_bytes(b"\n".join(path.read_bytes() for path in certificates))
    # No GUI launchers, service activation, package-manager hooks or host docs.
    for relative in ("share/man", "share/bash-completion", "share/fish",
                     "share/zsh", "share/applications"):
        shutil.rmtree(root / relative, ignore_errors=True)
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            # JARs/wheels are ZIP containers: byte replacement would invalidate CRCs.
            # Apktool's compressed Java default is handled by its native-aapt wrapper.
            if data.startswith(b"PK\x03\x04"):
                continue
            replaced = data.replace(PREFIX, PLACEHOLDER)
            # Home is writable, retained across re-extraction and passed by the service.
            if not data.startswith(b"\x7fELF") and b"\x00" not in data:
                replaced = replaced.replace(b"/data/data/com.termux/files/home", b"${HOME}")
            if replaced != data:
                path.write_bytes(replaced)


def write_archive(root, selected, archive_path, links, modes):
    """Write deterministic tar+gzip; preserve links without host symlinks."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    partial = archive_path.with_suffix(".partial")
    unpacked_bytes, commands = 0, []
    with partial.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=6) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for relative in sorted(selected):
                if any(parent.as_posix() in links for parent in PurePosixPath(relative).parents):
                    continue
                path = root / relative
                if not path.exists():
                    continue  # Documentation and resolv.conf removed by customize().
                member = archive.gettarinfo(str(path), relative)
                if relative in links:
                    member.type = tarfile.SYMTYPE
                    member.linkname = links[relative]
                    member.size = 0
                member.mode = modes.get(relative, 0o755 if member.isdir() or "bin" in path.relative_to(root).parts else 0o644)
                member.uid = member.gid = member.mtime = 0
                member.uname = member.gname = ""
                if member.isfile():
                    unpacked_bytes += member.size
                    with path.open("rb") as source:
                        archive.addfile(member, source)
                else:
                    archive.addfile(member)
                if PurePosixPath(relative).parent == PurePosixPath("bin") and not member.isdir():
                    commands.append(path.name)
    partial.replace(archive_path)
    return {"sha256": digest(archive_path.read_bytes()), "bytes": archive_path.stat().st_size,
            "unpacked_bytes": unpacked_bytes, "commands": sorted(commands)}


def build(abi, output, cache, release, repository=catalog.REPOSITORY, selected=None):
    catalog.validate_release(release)
    lock = json.loads((HERE / f"packages-{abi}.json").read_text())
    ordered, compositions = catalog.composition(PACKS, lock, selected)
    packages = {p["Package"]: p for p in lock["packages"]}
    needed = sorted(set().union(*compositions.values()))
    inputs = [p for p in HERE.rglob("*") if p.is_file() and ".cache" not in p.parts and "__pycache__" not in p.parts]
    key = digest(b"".join(p.relative_to(HERE).as_posix().encode() + p.read_bytes() for p in sorted(inputs)) +
                 json.dumps([abi, release, repository, [p["id"] for p in ordered]]).encode())
    output.mkdir(parents=True, exist_ok=True)
    expected = {f"{p['id']}-{abi}.tar.gz" for p in ordered}
    for obsolete in output.glob(f"*-{abi}.tar.gz"):
        if obsolete.name not in expected:
            obsolete.unlink()
    metadata_path = output / f"{abi}.json"
    if metadata_path.exists():
        old = json.loads(metadata_path.read_text())
        if old.get("build_key") == key and all(
            (output / p["archive"]).is_file() and digest((output / p["archive"]).read_bytes()) == p["sha256"]
            for p in old["packs"]):
            print(f"{abi}: archives up to date", flush=True)
            return old
    print(f"{abi}: verifying/downloading {len(needed)} pinned packages", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(lambda name: download_package(packages[name], cache, lock["repository"]), needed))
    with tempfile.TemporaryDirectory(prefix=f"agent-{abi}-", dir=cache) as temporary:
        root = Path(temporary) / "root"
        root.mkdir()
        links, modes, files = {}, {}, {}
        for name, path in zip(needed, paths):
            files[name] = unpack_deb(path, root, links, modes)
        materialize_links(root, links)
        extras = {}
        if "platform" in compositions:
            native_utilities(root, abi)
            extras["platform"] = {"bin/hprof-conv", "bin/etc1tool", "bin/make_f2fs_casefold"}
        if "apk" in compositions:
            install_artifacts(root, cache)
            extras["apk"] = {"share/java/smali.jar", "share/java/baksmali.jar", "bin/smali", "bin/baksmali"}
        customize(root)
        if "java" in compositions:
            extras["java"] = {"bin/" + name for name in ("java", "javac", "jar", "javap", "keytool", "jdeps", "jlink", "jshell")}
        results = []
        for pack in ordered:
            name = pack["id"]
            selected_files = set().union(*(files[p] for p in compositions[name])) | extras.get(name, set())
            provenance = root / "share/agent-toolchain" / name
            provenance.mkdir(parents=True)
            (provenance / "packages.json").write_text(json.dumps(
                [packages[p] for p in sorted(compositions[name])], indent=2) + "\n")
            (provenance / "NOTICE").write_text(
                "Android/Bionic packages built by the Termux project.\n"
                "Versions, checksums and source homepages: packages.json\n"
                "Recipes: https://github.com/termux/termux-packages\n"
                "Original package license notices are retained in share/.\n")
            if name == "platform":
                shutil.copytree(HERE / "native", provenance / "native")
            if name == "apk":
                shutil.copyfile(HERE / "artifacts.json", provenance / "artifacts.json")
            selected_files |= {p.relative_to(root).as_posix() for p in provenance.rglob("*")}
            archive = f"{name}-{abi}.tar.gz"
            value = {k: pack[k] for k in ("id", "name", "description", "requires")}
            value.update({"archive": archive, "url": catalog.asset_url(repository, release, archive)})
            value.update(write_archive(root, selected_files, output / archive, links, modes))
            results.append(value)
            print(f"{abi}/{name}: {value['bytes'] / 2**20:.2f} MiB compressed, "
                  f"{value['unpacked_bytes'] / 2**20:.2f} MiB extracted", flush=True)
        metadata = {"format": catalog.FORMAT, "runtime_format": catalog.RUNTIME_FORMAT,
                    "release": release, "repository": repository, "abi": abi, "build_key": key,
                    "relocation_prefix": PLACEHOLDER.decode(),
                    "python_version": packages["python"]["Version"].split("-")[0], "packs": results}
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
        return metadata


def merge_manifests(output, abis, release, repository=catalog.REPOSITORY):
    """Publish metadata only after every expected ABI/archive is complete."""
    catalog.validate_release(release)
    manifest = {"format": catalog.FORMAT, "runtime_format": catalog.RUNTIME_FORMAT,
                "release": release, "repository": repository, "abis": {}}
    archives = []
    for abi in abis:
        metadata = json.loads((output / f"{abi}.json").read_text())
        if (metadata["format"], metadata["runtime_format"], metadata["release"], metadata["repository"], metadata["abi"]) != (
                catalog.FORMAT, catalog.RUNTIME_FORMAT, release, repository, abi):
            raise ValueError(f"Inconsistent ABI metadata: {abi}")
        if [p["id"] for p in metadata["packs"]] != [p["id"] for p in catalog.ordered_packs(PACKS, [p["id"] for p in PACKS])]:
            raise ValueError(f"Release is missing packs: {abi}")
        for pack in metadata["packs"]:
            path = output / pack["archive"]
            if pack["url"] != catalog.asset_url(repository, release, pack["archive"]):
                raise ValueError("Archive URL does not match the release")
            if path.stat().st_size != pack["bytes"] or digest(path.read_bytes()) != pack["sha256"]:
                raise ValueError(f"Archive checksum/size mismatch: {path.name}")
            archives.append(path)
        manifest["abis"][abi] = {k: metadata[k] for k in ("relocation_prefix", "python_version", "packs")}
    target = output / "manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "SHA256SUMS").write_text("".join(
        f"{digest(p.read_bytes())}  {p.name}\n" for p in sorted(archives + [target])))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--abi", choices=ARCHES, action="append")
    parser.add_argument("--pack", choices=[p["id"] for p in PACKS], action="append")
    parser.add_argument("--release", default="v0.1.0")
    parser.add_argument("--repository", default=catalog.REPOSITORY)
    parser.add_argument("--output", type=Path, default=HERE.parent / "dist")
    parser.add_argument("--cache", type=Path, default=HERE.parent / ".cache")
    parser.add_argument("--update-lock", action="store_true")
    parser.add_argument("--index", type=Path)
    parser.add_argument("--merge", action="store_true", help="Verify all archives and generate manifest.json and SHA256SUMS")
    args = parser.parse_args()
    abis = args.abi or list(ARCHES)
    if args.index and (not args.update_lock or len(abis) != 1):
        parser.error("--index requires --update-lock and exactly one --abi")
    if args.merge:
        merge_manifests(args.output.resolve(), abis, args.release, args.repository)
        return
    with build_lock(args.cache.resolve()):
        for abi in abis:
            if args.update_lock:
                update_lock(abi, args.index)
            else:
                build(abi, args.output.resolve(), args.cache.resolve(), args.release, args.repository, args.pack)
        if args.update_lock and not args.index:
            update_artifacts()


if __name__ == "__main__":
    main()


