import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolchain"))

import build_packs as bundle


def deb_fixture():
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:xz") as archive:
        entry = tarfile.TarInfo("./data/data/com.termux/files/usr/bin/tool")
        entry.mode = 0o755
        entry.size = 4
        archive.addfile(entry, io.BytesIO(b"ELF!"))
        link = tarfile.TarInfo("./data/data/com.termux/files/usr/bin/alias")
        link.type = tarfile.SYMTYPE
        link.linkname = "tool"
        archive.addfile(link)
    data = payload.getvalue()
    header = f"{'data.tar.xz/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
    return b"!<arch>\n" + header + data + (b"\n" if len(data) % 2 else b"")


class PackagerTest(unittest.TestCase):
    def test_packages_do_not_require_host_symlink_privileges_and_keep_posix_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            package = Path(temporary) / "test.deb"
            package.write_bytes(deb_fixture())
            links, modes = {}, {}
            bundle.unpack_deb(package, root, links, modes)
            self.assertFalse((root / "bin/alias").exists())
            self.assertEqual(links, {"bin/alias": "tool"})
            self.assertEqual(modes["bin/tool"], 0o755)
            bundle.materialize_links(root, links)
            self.assertFalse((root / "bin/alias").is_symlink())
            self.assertEqual((root / "bin/alias").read_bytes(), b"ELF!")

    def test_link_escape_and_cycles_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for links in ({"bad": "../outside"}, {"one": "two", "two": "one"}):
                with self.assertRaises(ValueError):
                    bundle.materialize_links(root, links)

    def test_relocation_preserves_zip_containers_and_their_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bin").mkdir()
            (root / "etc/tls").mkdir(parents=True)
            jar = root / "example.jar"
            with zipfile.ZipFile(jar, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("sample", bundle.PREFIX)
            original = jar.read_bytes()
            (root / "bin/native").write_bytes(b"\x7fELF" + bundle.PREFIX)
            bundle.customize(root)
            self.assertEqual(jar.read_bytes(), original)
            with zipfile.ZipFile(jar) as archive:
                self.assertIsNone(archive.testzip())
            self.assertIn(bundle.PLACEHOLDER, (root / "bin/native").read_bytes())

    def test_indexes_pin_both_abis_and_include_essential_licenses_and_alias_targets(self):
        for abi in bundle.ARCHES:
            lock = json.loads((bundle.HERE / f"packages-{abi}.json").read_text())
            packages = {package["Package"]: package for package in lock["packages"]}
            self.assertTrue(set(bundle.ROOTS) <= packages.keys())
            self.assertIn("termux-licenses", packages)
            self.assertIn("llvm", packages)
            for package in packages.values():
                self.assertRegex(package["SHA256"], "^[a-f0-9]{64}$")
            self.assertEqual(len(bundle.PREFIX), len(bundle.PLACEHOLDER))


if __name__ == "__main__":
    unittest.main()
