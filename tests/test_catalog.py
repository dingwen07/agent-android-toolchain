import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolchain"))
import build_packs as builder
import catalog


class CatalogTest(unittest.TestCase):
    def test_dependency_closure_uses_locked_alternatives_and_subtracts_prerequisites(self):
        packs = [{"id": "core", "requires": [], "roots": ["base"]},
                 {"id": "java", "requires": ["core"], "roots": ["jvm"]},
                 {"id": "apk", "requires": ["core", "java"], "roots": ["tool"]}]
        lock = {"format": 2, "packages": [
            {"Package": "base"}, {"Package": "lib"},
            {"Package": "jvm", "Depends": "base, absent | lib (>= 1)"},
            {"Package": "tool", "Depends": "jvm, base"}]}
        ordered, result = catalog.composition(packs, lock, ["apk"])
        self.assertEqual([p["id"] for p in ordered], ["core", "java", "apk"])
        self.assertEqual(result, {"core": {"base"}, "java": {"jvm", "lib"}, "apk": {"tool"}})

    def test_incomplete_and_cyclic_dependencies_fail_before_building(self):
        with self.assertRaises(ValueError):
            catalog.ordered_packs([{"id": "one", "requires": ["two"]},
                                   {"id": "two", "requires": ["one"]}], ["one"])
        with self.assertRaises(ValueError):
            catalog.package_closure({"one": {"Depends": "missing"}}, ["one"])

    def test_pinned_indexes_cover_every_pack_on_both_abis(self):
        for abi in builder.ARCHES:
            self.assertIn(f"core-{abi}.tar.gz", catalog.asset_url(catalog.REPOSITORY, "v1.0.0", f"core-{abi}.tar.gz"))
            lock = json.loads((builder.HERE / f"packages-{abi}.json").read_text())
            ordered, compositions = catalog.composition(builder.PACKS, lock)
            self.assertEqual(len(ordered), len(builder.PACKS))
            self.assertTrue(all(compositions.values()))
            self.assertNotIn("openjdk-21", compositions["apk"])
            self.assertIn("python", compositions["platform"])
            self.assertNotIn("python", compositions["core"])

    def test_archive_roundtrip_is_reproducible_and_preserves_links_and_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "root"
            (root / "bin").mkdir(parents=True)
            (root / "bin/tool").write_bytes(b"hello")
            (root / "bin/alias").write_bytes(b"hello")
            selected = {"bin", "bin/tool", "bin/alias"}
            links, modes = {"bin/alias": "tool"}, {"bin/tool": 0o755}
            a = builder.write_archive(root, selected, directory / "first.tar.gz", links, modes)
            b = builder.write_archive(root, selected, directory / "second.tar.gz", links, modes)
            self.assertEqual(a, b)
            self.assertEqual(a["unpacked_bytes"], 5)
            with tarfile.open(directory / "first.tar.gz") as archive:
                self.assertTrue(archive.getmember("bin/alias").issym())
                self.assertEqual(archive.getmember("bin/tool").mode, 0o755)
                self.assertEqual(archive.extractfile("bin/tool").read(), b"hello")

    def test_release_manifest_rejects_corrupt_or_incomplete_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            packs = []
            for pack in catalog.ordered_packs(builder.PACKS, [p["id"] for p in builder.PACKS]):
                archive = pack["id"] + "-arm64-v8a.tar.gz"
                (directory / archive).write_bytes(b"payload")
                packs.append({"id": pack["id"], "archive": archive, "bytes": 7,
                              "sha256": builder.digest(b"payload"),
                              "url": catalog.asset_url(catalog.REPOSITORY, "v1.0.0", archive)})
            metadata = {"format": catalog.FORMAT, "runtime_format": catalog.RUNTIME_FORMAT,
                        "release": "v1.0.0", "repository": catalog.REPOSITORY, "abi": "arm64-v8a",
                        "relocation_prefix": builder.PLACEHOLDER.decode(), "python_version": "3.14", "packs": packs}
            path = directory / "arm64-v8a.json"
            path.write_text(json.dumps(metadata))
            result = builder.merge_manifests(directory, ["arm64-v8a"], "v1.0.0")
            self.assertEqual(result["release"], "v1.0.0")
            self.assertIn("manifest.json", (directory / "SHA256SUMS").read_text())
            (directory / packs[0]["archive"]).write_bytes(b"corrupt")
            with self.assertRaises(ValueError):
                builder.merge_manifests(directory, ["arm64-v8a"], "v1.0.0")
            metadata["packs"] = []
            path.write_text(json.dumps(metadata))
            with self.assertRaises(ValueError):
                builder.merge_manifests(directory, ["arm64-v8a"], "v1.0.0")


if __name__ == "__main__":
    unittest.main()
