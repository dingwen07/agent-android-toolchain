"""Build-time package composition and the app's versioned release contract."""
import json
from pathlib import Path
import re

FORMAT = 2
RUNTIME_FORMAT = 1
REPOSITORY = "dingwen07/agent-android-toolchain"


def read_catalog(path):
    value = json.loads(Path(path).read_text())
    if value.get("format") != 1:
        raise ValueError("Unsupported pack catalog format")
    packs = value["packs"]
    ids = [p["id"] for p in packs]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[a-z][a-z0-9-]*", p) for p in ids):
        raise ValueError("Invalid or duplicate pack ID")
    if "core" not in ids:
        raise ValueError("The catalog must provide core")
    ordered_packs(packs, ids)
    return packs


def ordered_packs(packs, selected):
    by_id = {p["id"]: p for p in packs}
    result, visiting, visited = [], set(), set()
    def visit(name):
        if name not in by_id:
            raise ValueError(f"Unknown required pack: {name}")
        if name in visiting:
            raise ValueError(f"Pack dependency cycle: {name}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in by_id[name]["requires"]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        result.append(by_id[name])
    for name in selected:
        visit(name)
    return result


def package_closure(packages, roots):
    selected = set()
    def visit(name):
        if name in selected:
            return
        if name not in packages:
            raise ValueError(f"Package missing from lock: {name}")
        selected.add(name)
        for group in packages[name].get("Depends", "").split(","):
            alternatives = [re.split(r"\s|\(", item.strip())[0].split(":")[0]
                            for item in group.split("|") if item.strip()]
            if alternatives:
                dependency = next((p for p in alternatives if p in packages), None)
                if dependency is None:
                    raise ValueError(f"Unresolved dependency of {name}: {group}")
                visit(dependency)
    for name in roots:
        visit(name)
    return selected


def composition(packs, lock, selected=None):
    if lock.get("format") != 2:
        raise ValueError("Refresh the lock: dependency metadata requires lock format 2")
    packages = {p["Package"]: p for p in lock["packages"]}
    ordered = ordered_packs(packs, selected or [p["id"] for p in packs])
    closures = {p["id"]: package_closure(packages, p["roots"]) for p in ordered}
    result = {}
    for pack in ordered:
        provided = set()
        for dependency in ordered_packs(packs, pack["requires"]):
            provided |= closures[dependency["id"]]
        result[pack["id"]] = closures[pack["id"]] - provided
    return ordered, result


def validate_release(release):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", release):
        raise ValueError("Release must be a version tag such as v0.1.0")
    return release


def asset_url(repository, release, filename):
    validate_release(release)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected an owner/repository")
    if not re.fullmatch(r"[a-z0-9_-]+\.tar\.gz", filename):
        raise ValueError("Invalid archive filename")
    return f"https://github.com/{repository}/releases/download/{release}/{filename}"
