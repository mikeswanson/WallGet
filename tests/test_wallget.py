"""Regression tests for WallGet catalog discovery and asset selection.

Run: python3 -m unittest discover -s tests -v
"""

import contextlib
import functools
import http.server
import importlib.util
import io
import threading
import json
import os
import plistlib
import pwd
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wallget", REPO / "wallget.py")
assert spec is not None and spec.loader is not None
wallget = importlib.util.module_from_spec(spec)
sys.modules["wallget"] = wallget
spec.loader.exec_module(wallget)


def catalog(asset_count=2, url_key="url-4K-SDR-240FPS", with_ext=True):
    assets = []
    for i in range(asset_count):
        aid = f"ASSET-{i}"
        url = f"https://sylvan.apple.com/a/{aid}{'.mov' if with_ext else ''}"
        assets.append({
            "id": aid,
            "localizedNameKey": f"NAME_{i}",
            "categories": ["CAT"],
            url_key: url,
        })
    return {
        "assets": assets,
        "categories": [{"id": "CAT", "localizedNameKey": "CAT_NAME"}],
    }


def loctable():
    return {"en": {"NAME_0": "First", "NAME_1": "Second", "CAT_NAME": "Category"}}


def write_catalog(base, entries=None, strings=None):
    manifest = base / "manifest"
    manifest.mkdir(parents=True, exist_ok=True)
    (manifest / "entries.json").write_text(
        json.dumps(entries if entries is not None else catalog()), encoding="utf-8"
    )
    bundle = manifest / "TVIdleScreenStrings.bundle" / "Contents" / "Resources"
    bundle.mkdir(parents=True, exist_ok=True)
    with open(bundle / "Localizable.nocache.loctable", "wb") as fp:
        plistlib.dump(strings if strings is not None else loctable(), fp)
    return base


def write_tar(base, entries, strings=None):
    path = base / "manifest.tar"
    with tarfile.open(path, "w") as archive:
        data = json.dumps(entries).encode()
        info = tarfile.TarInfo("entries.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        if strings is not None:
            name = "TVIdleScreenStrings.bundle/Contents/Resources/Localizable.nocache.loctable"
            raw = plistlib.dumps(strings)
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return path


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "aerials"
        self.base.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()


class CatalogResolutionTests(TempHome):
    def test_local_manifest_wins(self):
        write_catalog(self.base)
        write_tar(self.base, catalog(asset_count=1))
        manifest, source, notes = wallget.resolve_manifest(str(self.base))
        self.assertEqual(source, "local manifest")
        self.assertEqual(len(manifest["entries"]["assets"]), 2)
        self.assertEqual(manifest["strings"]["NAME_0"], "First")

    def test_empty_manifest_dir_uses_tar(self):
        (self.base / "manifest").mkdir()
        write_tar(self.base, catalog(asset_count=3), loctable())
        manifest, source, _ = wallget.resolve_manifest(str(self.base))
        self.assertEqual(source, "manifest.tar")
        self.assertEqual(len(manifest["entries"]["assets"]), 3)
        self.assertFalse(list((self.base / "manifest").iterdir()))

    def test_corrupt_entries_falls_through_to_tar(self):
        (self.base / "manifest").mkdir()
        (self.base / "manifest" / "entries.json").write_text("{broken", encoding="utf-8")
        write_tar(self.base, catalog(asset_count=1), loctable())
        manifest, source, notes = wallget.resolve_manifest(str(self.base))
        self.assertEqual(source, "manifest.tar")
        self.assertIn("local manifest: unavailable", notes)

    def test_system_bundle_fallback(self):
        (self.base / "manifest").mkdir()
        system = self.base / "system" / "Resources"
        system.mkdir(parents=True)
        (system / "entries.json").write_text(json.dumps(catalog(asset_count=4)), encoding="utf-8")
        bundle = system / "TVIdleScreenStrings.bundle" / "Contents" / "Resources"
        bundle.mkdir(parents=True)
        with open(bundle / "Localizable.nocache.loctable", "wb") as fp:
            plistlib.dump(loctable(), fp)
        with mock.patch.object(wallget, "SYSTEM_AERIALS_RESOURCES", str(system)):
            with mock.patch.object(wallget, "remote_catalog_urls", return_value=[]):
                manifest, source, _ = wallget.resolve_manifest(str(self.base))
        self.assertEqual(source, "system bundle")
        self.assertEqual(len(manifest["entries"]["assets"]), 4)

    def test_remote_urls_are_restricted_to_apple_hosts(self):
        self.assertTrue(wallget._trusted_catalog_url(
            "https://sylvan.apple.com/itunes-assets/a.tar"))
        self.assertFalse(wallget._trusted_catalog_url(
            "https://attacker.example/a.tar"))
        self.assertFalse(wallget._trusted_catalog_url(
            "http://sylvan.apple.com/a.tar"))

    def test_no_sources_gives_actionable_notes(self):
        with mock.patch.object(wallget, "SYSTEM_AERIALS_RESOURCES", str(self.base / "absent")):
            with mock.patch.object(wallget, "system_resource_dirs", return_value=[]):
                with mock.patch.object(wallget, "remote_catalog_urls", return_value=[]):
                    manifest, source, notes = wallget.resolve_manifest(str(self.base))
        self.assertIsNone(manifest)
        self.assertEqual(source, "")
        self.assertTrue(any("local manifest: unavailable" in n for n in notes))

    def test_detect_storage_mode_tolerates_empty_manifest_dir(self):
        manifest_dir = self.base / "manifest"
        manifest_dir.mkdir()
        with mock.patch.object(wallget, "USER_AERIALS_BASE", str(self.base)):
            self.assertEqual(wallget.detect_storage_mode(), wallget.STORAGE_MODE_USER)

    def test_strings_bundle_variants_found(self):
        target = self.base / "manifest" / "TVIdleScreenStrings.bundle" / "en.lproj"
        target.mkdir(parents=True)
        (target / "Localizable.nocache.strings").write_bytes(plistlib.dumps({"A": "B"}))
        found = wallget.find_strings_path(str(self.base / "manifest"))
        self.assertTrue(found.endswith("Localizable.nocache.strings"))


class ParsingTests(unittest.TestCase):
    def test_loctable_metadata_first_does_not_hide_locales(self):
        raw = {"LocProvenance": {"hash": "x"}, "en": {"A": "Name"}, "fr": {"A": "Nom"}}
        with mock.patch.dict(os.environ, {"LANG": "fr_FR.UTF-8"}, clear=False):
            self.assertEqual(wallget._pick_loctable_strings(raw), {"A": "Nom"})

    def test_loctable_flat_plist(self):
        self.assertEqual(wallget._pick_loctable_strings({"A": "Name"}), {"A": "Name"})

    def test_plain_strings_plist(self):
        raw = plistlib.dumps({"A": "Name"})
        manifest = wallget.parse_manifest(
            json.dumps(catalog(asset_count=1)).encode(), raw, "Localizable.nocache.strings"
        )
        self.assertEqual(manifest["strings"]["A"], "Name")

    def test_malformed_inputs_never_raise(self):
        self.assertIsNone(wallget.parse_manifest(b"{bad", None))
        self.assertIsNone(wallget.parse_manifest(b'"not a dict"', None))
        self.assertIsNone(wallget.parse_manifest(b'{"assets": []}', None))

    def test_schema_type_abuse_does_not_crash(self):
        bad = {"assets": [None, 5, {"id": "OK", "categories": None, "subcategories": 7,
                                    "localizedNameKey": "K",
                                    "url-4K-SDR-240FPS": "https://x/a.mov"}],
               "categories": [None, 7, {"id": "CAT"}]}
        manifest = {"entries": bad, "strings": {"K": "Name"}}
        categories = wallget.extract_categories(manifest)
        self.assertEqual([c.get("id") for c in categories], ["CAT"])
        assets_map, order = wallget.extract_assets(manifest)
        self.assertEqual(order, ["OK"])
        prepared = wallget.prepare_assets(
            assets_map, order, {"K": "Name"}, wallget.build_category_maps(categories), {"CAT"}
        )
        self.assertEqual(len(prepared), 1)


class AssetPreparationTests(unittest.TestCase):
    def test_alternate_url_key_and_missing_extension(self):
        entries = catalog(asset_count=1, url_key="url-4K-SDR", with_ext=False)
        manifest = {"entries": entries, "strings": {"NAME_0": "First"}}
        assets_map, order = wallget.extract_assets(manifest)
        cats = wallget.extract_categories(manifest)
        prepared = wallget.prepare_assets(
            assets_map, order, manifest["strings"], wallget.build_category_maps(cats), {"CAT"}
        )
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].ext, ".mov")

    def test_missing_translation_uses_key_instead_of_dropping_asset(self):
        manifest = {"entries": catalog(asset_count=1), "strings": {}}
        assets_map, order = wallget.extract_assets(manifest)
        cats = wallget.extract_categories(manifest)
        prepared = wallget.prepare_assets(
            assets_map, order, {}, wallget.build_category_maps(cats), {"CAT"}
        )
        self.assertEqual(prepared[0].label, "NAME_0")

    def test_all_selection_expands_every_row(self):
        manifest = {"entries": catalog(asset_count=3), "strings": {"NAME_0": "A", "NAME_1": "B", "NAME_2": "C", "CAT_NAME": "Cat"}}
        assets_map, order = wallget.extract_assets(manifest)
        cats = wallget.extract_categories(manifest)
        assets = wallget.prepare_assets(
            assets_map, order, manifest["strings"], wallget.build_category_maps(cats), {"CAT"}
        )
        _grouped, rows = wallget.build_asset_groups(assets, [("CAT", "Cat")], None, "All", {})
        self.assertEqual([r.index for r in rows], [1, 2, 3])
        chosen = wallget.parse_selection("all", len(rows) + 1)
        # main() expands the trailing "All" sentinel into every displayed row.
        self.assertIn(len(rows) + 1, chosen)
        expanded = set(r.index for r in rows) if len(rows) + 1 in chosen else chosen
        self.assertEqual(len(expanded), 3)


class RangeHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args, payload: bytes, **kwargs):
        self.payload = payload
        super().__init__(*args, **kwargs)

    def log_message(self, *_args, **_kwargs):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        range_header = "" if "/ignore" in self.path else self.headers.get("Range", "")
        truncated = "/short" in self.path
        if range_header.startswith("bytes="):
            start = int(range_header.split("=", 1)[1].split("-", 1)[0] or 0)
            if start >= len(self.payload):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(self.payload)}")
                self.end_headers()
                return
            data = self.payload[start:]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}")
        else:
            data = self.payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data[:len(data) // 2] if truncated else data)


class MissingHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args, **_kwargs):
        pass

    def do_HEAD(self):
        self.send_error(404)

    def do_GET(self):
        self.send_error(404)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "aerials"
        self.videos = self.base / "videos"
        self.videos.mkdir(parents=True)
        self.payload = b"wallget-test-video-payload" * 40
        handler = functools.partial(RangeHandler, payload=self.payload)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/asset.mov"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def local_catalog(self):
        entries = {
            "assets": [{
                "id": "LOCAL-1",
                "localizedNameKey": "LOCAL_NAME",
                "categories": ["CAT"],
                "url-4K-SDR-240FPS": self.url,
            }],
            "categories": [{"id": "CAT", "localizedNameKey": "CAT_NAME"}],
        }
        strings = {"en": {"LOCAL_NAME": "Local Asset", "CAT_NAME": "Category"}}
        write_catalog(self.base, entries=entries, strings=strings)
        return entries, strings

    def system_catalog(self, root):
        entries, strings = self.local_catalog()
        system = root / "Resources"
        (system).mkdir(parents=True)
        (system / "entries.json").write_text(json.dumps(entries), encoding="utf-8")
        bundle = system / "TVIdleScreenStrings.bundle" / "Contents" / "Resources"
        bundle.mkdir(parents=True)
        with open(bundle / "Localizable.nocache.loctable", "wb") as fp:
            plistlib.dump(strings, fp)
        shutil.rmtree(self.base / "manifest")
        return system

    @contextlib.contextmanager
    def patched(self, *, system=None, remote=(), storage=None):
        common = dict(
            USER_AERIALS_BASE=str(self.base),
            USER_MANIFEST_PATH=str(self.base / "manifest" / "entries.json"),
            USER_VIDEO_PATH=str(self.videos),
            ACTIVE_VIDEO_PATH=str(self.videos),
            STORAGE_MODE=storage or wallget.STORAGE_MODE_USER,
            SYSTEM_AERIALS_RESOURCES=str(system or self.base / "absent"),
        )
        with mock.patch.dict(wallget.__dict__, common):
            with mock.patch.object(wallget, "remote_catalog_urls", return_value=list(remote)):
                yield

    def run_main(self, argv, inputs=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if inputs is None:
                with mock.patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
                    wallget.main(argv[1:])
            else:
                with mock.patch("builtins.input", side_effect=inputs):
                    wallget.main(argv[1:])
        return out.getvalue(), err.getvalue()

    def test_download_end_to_end(self):
        self.local_catalog()
        with self.patched():
            out, _err = self.run_main(
                ["wallget.py", "--category", "all", "--assets", "1", "--download", "--yes"]
            )
        target = self.videos / "LOCAL-1.mov"
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertFalse((self.videos / "LOCAL-1.mov.part").exists())
        self.assertIn("Downloaded 'Local Asset'", out)

    def test_resume_partial_download(self):
        self.local_catalog()
        (self.videos / "LOCAL-1.mov.part").write_bytes(self.payload[:100])
        with self.patched():
            self.run_main(["wallget.py", "--download", "--yes"])
        self.assertEqual((self.videos / "LOCAL-1.mov").read_bytes(), self.payload)

    def test_completed_part_is_finalized_without_redownload(self):
        self.local_catalog()
        (self.videos / "LOCAL-1.mov.part").write_bytes(self.payload)
        with self.patched():
            with mock.patch.object(wallget, "download_file",
                                   wraps=wallget.download_file) as download:
                self.run_main(["wallget.py", "--download", "--yes"])
        self.assertEqual(download.call_count, 1)
        self.assertEqual((self.videos / "LOCAL-1.mov").read_bytes(), self.payload)

    def test_delete_end_to_end(self):
        self.local_catalog()
        target = self.videos / "LOCAL-1.mov"
        target.write_bytes(self.payload)
        with self.patched():
            out, _err = self.run_main(["wallget.py", "--assets", "all", "--delete", "--yes"])
        self.assertFalse(target.exists())
        self.assertIn("Deleted 'Local Asset'", out)

    def test_fresh_install_uses_system_bundle_in_main(self):
        system = self.system_catalog(Path(self.tmp.name))
        with self.patched(system=system):
            out, _err = self.run_main(["wallget.py", "--assets", "all", "--delete", "--yes"])
        self.assertIn("Catalog source: system bundle", out)
        self.assertIn("Nothing to delete", out)

    def test_tar_fallback_in_main(self):
        entries, strings = self.local_catalog()
        write_tar(self.base, entries, strings)
        shutil.rmtree(self.base / "manifest")
        (self.base / "manifest").mkdir()
        with self.patched():
            out, _err = self.run_main(["wallget.py", "--assets", "all", "--delete", "--yes"])
        self.assertIn("Catalog source: manifest.tar", out)

    def test_no_catalog_exits_nonzero_with_remediation(self):
        (self.base / "manifest").mkdir()
        with self.patched():
            with mock.patch.object(wallget, "system_resource_dirs", return_value=[]):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as ctx:
                        wallget.main(["--assets", "all", "--delete", "--yes"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("System Settings > Wallpaper", err.getvalue())

    def test_interactive_declined_confirmation_does_not_download(self):
        self.local_catalog()
        with self.patched():
            out, _err = self.run_main(["wallget.py"], inputs=["1", "1", "x", "n"])
        self.assertIn("Category", out)
        self.assertFalse((self.videos / "LOCAL-1.mov").exists())

    def test_interactive_download(self):
        self.local_catalog()
        with self.patched():
            self.run_main(["wallget.py"], inputs=["1", "1", "d", "y"])
        self.assertEqual((self.videos / "LOCAL-1.mov").read_bytes(), self.payload)

    def test_delete_without_selector_is_rejected(self):
        self.local_catalog()
        target = self.videos / "LOCAL-1.mov"
        target.write_bytes(self.payload)
        with self.patched():
            with self.assertRaises(SystemExit) as ctx:
                self.run_main(["wallget.py", "--delete", "--yes"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertTrue(target.exists())

    def test_invalid_category_exits_nonzero(self):
        self.local_catalog()
        with self.patched():
            with self.assertRaises(SystemExit) as ctx:
                self.run_main(["wallget.py", "--category", "nope", "--delete", "--yes"])
        self.assertEqual(ctx.exception.code, 2)

    def test_get_content_length_against_local_server(self):
        self.assertEqual(wallget.get_content_length(self.url), len(self.payload))

    def test_failed_download_returns_error(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MissingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/missing.mov"
            target = self.videos / "missing.mov"
            _label, error = wallget.download_file(("Missing", url, str(target), None))
            self.assertIsNotNone(error)
            self.assertIn("404", error)
            self.assertFalse(target.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_remote_urls_resolved_ordered_and_deduped(self):
        source = self.base / "manifest"
        source.mkdir(parents=True, exist_ok=True)
        (source / "manifest.source").write_text(
            "/itunes-assets/Aerials126/v4/resources-27-0-1.tar", encoding="utf-8"
        )
        defaults = mock.Mock(returncode=0, stdout=(
            b"https://sylvan.apple.com/itunes-assets/Aerials126/resources-27-0-1.tar\n"
        ))
        plist_bytes = plistlib.dumps({
            "resources-url": "https://sylvan.apple.com/itunes-assets/Aerials126/resources-27-0-1.tar"
        })
        with mock.patch.object(wallget.subprocess, "run", return_value=defaults):
            with mock.patch.object(wallget, "read_url", return_value=plist_bytes):
                urls = wallget.remote_catalog_urls(str(self.base))
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(wallget._trusted_catalog_url(u) for u in urls))


class CoverageEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_asset(self, aid="A", url="http://127.0.0.1/1.mov"):
        return wallget.PreparedAsset(
            id=aid, label="A", url=url, ext=".mov", preferred_order=None,
            manifest_index=0, subcat_primary_order=None, subcat_group_order=None,
            categories={"CAT"}, subcategory_ids=set(),
        )

    def test_resolved_size_missing_file(self):
        status = wallget.AssetStatus(existing_paths=[str(self.root / "gone")])
        self.assertIsNone(status.resolved_size())

    def test_resolve_user_home_variants(self):
        with mock.patch.dict(os.environ, {"SUDO_USER": "root", "HOME": str(self.root)}):
            self.assertEqual(wallget.resolve_user_home(), str(self.root))
        name = pwd.getpwuid(os.getuid()).pw_name
        with mock.patch.object(wallget.os, "geteuid", return_value=0):
            with mock.patch.dict(os.environ, {"SUDO_USER": name}):
                self.assertEqual(wallget.resolve_user_home(), pwd.getpwnam(name).pw_dir)
        with mock.patch.object(wallget.os, "geteuid", return_value=0):
            with mock.patch.dict(os.environ,
                                 {"SUDO_USER": "no-such-user-xyz", "HOME": str(self.root)}):
                self.assertEqual(wallget.resolve_user_home(), str(self.root))
        with mock.patch.object(wallget.os, "geteuid", side_effect=AttributeError):
            with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
                self.assertEqual(wallget.resolve_user_home(), str(self.root))

    def test_detect_storage_mode_legacy_and_missing(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        entries = legacy / "entries.json"
        entries.write_text("{}", encoding="utf-8")
        with mock.patch.object(wallget, "USER_AERIALS_BASE", str(self.root / "absent")):
            with mock.patch.object(wallget, "LEGACY_ENTRIES_PATH", str(entries)):
                self.assertEqual(wallget.detect_storage_mode(), wallget.STORAGE_MODE_LEGACY)
            with mock.patch.object(wallget, "LEGACY_ENTRIES_PATH", str(legacy / "none")):
                self.assertIsNone(wallget.detect_storage_mode())

    def test_locale_candidates_from_environment(self):
        with mock.patch.dict(os.environ, {
            "LC_ALL": "C", "LANG": "zh-Hans_CN.UTF-8", "LANGUAGE": "fr:en"
        }, clear=False):
            with mock.patch.object(wallget.locale, "getlocale",
                                   side_effect=ValueError("no locale")):
                candidates = wallget._locale_candidates()
        self.assertIn("zh_hans_cn", candidates)
        self.assertIn("zh_hans", candidates)
        self.assertIn("fr", candidates)
        self.assertIn("en", candidates)

    def test_loctable_odd_shapes(self):
        self.assertEqual(wallget._pick_loctable_strings(None), {})
        self.assertEqual(wallget._pick_loctable_strings({}), {})
        self.assertEqual(wallget._pick_loctable_strings({1: 2}), {})
        with mock.patch.dict(os.environ, {"LANG": "zz_ZZ"}, clear=False):
            table = wallget._pick_loctable_strings({"en": {"A": "Name"}})
        self.assertEqual(table, {"A": "Name"})

    def test_parse_strings_non_dict_plist(self):
        self.assertEqual(wallget._parse_strings(plistlib.dumps([1, 2]), "x.strings"), {})

    def test_parse_manifest_invalid_strings_bytes(self):
        manifest = wallget.parse_manifest(
            b'{"assets":[{"id":"A"}]}', b"not a plist", "x.loctable"
        )
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["strings"], {})

    def test_manifest_from_paths_unreadable_entries(self):
        directory = self.root / "entries.json"
        directory.mkdir()
        self.assertIsNone(wallget.manifest_from_paths(str(directory), ""))

    def test_tar_with_dot_prefix_and_no_strings(self):
        path = self.root / "archive.tar"
        with tarfile.open(path, "w") as archive:
            data = json.dumps(catalog(asset_count=1)).encode()
            for name in ("./entries.json", "TVIdleScreenStrings.bundle/README"):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        manifest = wallget.manifest_from_tar(str(path))
        self.assertEqual(len(manifest["entries"]["assets"]), 1)
        self.assertEqual(manifest["strings"], {})

    def test_tar_without_entries_or_corrupt(self):
        empty = self.root / "empty.tar"
        with tarfile.open(empty, "w") as archive:
            info = tarfile.TarInfo("readme.txt")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        self.assertIsNone(wallget.manifest_from_tar(str(empty)))
        broken = self.root / "broken.tar"
        broken.write_bytes(b"not a tar")
        self.assertIsNone(wallget.manifest_from_tar(str(broken)))

    def test_read_url_failure(self):
        self.assertIsNone(wallget.read_url("not-a-url"))

    def test_remote_catalog_urls_ignores_untrusted_feed(self):
        base = self.root / "aerials"
        (base / "manifest").mkdir(parents=True)
        (base / "manifest" / "manifest.source").write_text("relative/path.tar", encoding="utf-8")
        with mock.patch.object(wallget.subprocess, "run",
                               side_effect=OSError("defaults missing")):
            with mock.patch.object(wallget, "read_url",
                                   return_value=plistlib.dumps({"other": "x"})):
                self.assertEqual(wallget.remote_catalog_urls(str(base)), [])

    @mock.patch.object(wallget, "system_resource_dirs", return_value=[])
    def test_resolve_manifest_download_failures_then_success(self, _no_system):
        base = self.root / "aerials"
        (base / "manifest").mkdir(parents=True)
        good = io.BytesIO()
        with tarfile.open(fileobj=good, mode="w") as archive:
            data = json.dumps(catalog(asset_count=1)).encode()
            info = tarfile.TarInfo("entries.json")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        with mock.patch.object(wallget, "SYSTEM_AERIALS_RESOURCES", str(self.root / "absent")):
            with mock.patch.object(wallget, "remote_catalog_urls",
                                   return_value=["https://sylvan.apple.com/a.tar"]):
                with mock.patch.object(wallget, "read_url", return_value=None):
                    manifest, source, notes = wallget.resolve_manifest(str(base))
                self.assertIsNone(manifest)
                self.assertTrue(any("download failed" in n for n in notes))
                with mock.patch.object(wallget, "read_url", return_value=b"broken"):
                    manifest, source, notes = wallget.resolve_manifest(str(base))
                self.assertIsNone(manifest)
                self.assertTrue(any("invalid download" in n for n in notes))
                with mock.patch.object(wallget, "read_url", return_value=good.getvalue()):
                    manifest, source, _notes = wallget.resolve_manifest(str(base))
                self.assertEqual(source, "Apple catalog download")
                self.assertEqual(len(manifest["entries"]["assets"]), 1)

    def test_assess_status_remote_failure_and_existing_file(self):
        path = self.root / "A.mov"
        path.write_bytes(b"12345")
        with mock.patch.object(wallget, "get_content_length", side_effect=OSError("offline")):
            aid, length, up_to_date = wallget.assess_asset_status(("A", "http://x", [str(path)]))
        self.assertEqual((aid, length, up_to_date), ("A", None, True))
        with mock.patch.object(wallget, "get_content_length", return_value=5):
            aid, length, up_to_date = wallget.assess_asset_status(("A", "http://x", [str(path)]))
        self.assertEqual((aid, length, up_to_date), ("A", 5, True))

    def test_existing_files_without_video_path(self):
        with mock.patch.object(wallget, "ACTIVE_VIDEO_PATH", ""):
            self.assertEqual(wallget.existing_files("A", ".mov"), [])

    def test_main_no_store_exits_nonzero(self):
        err = io.StringIO()
        with mock.patch.object(wallget, "STORAGE_MODE", None):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    wallget.main(["--category", "all", "--assets", "all",
                                  "--delete", "--yes"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("System Settings > Wallpaper", err.getvalue())

    def test_main_legacy_requires_root(self):
        with mock.patch.object(wallget, "STORAGE_MODE", wallget.STORAGE_MODE_LEGACY):
            with mock.patch.object(wallget.os, "geteuid", return_value=501):
                with self.assertRaises(SystemExit) as ctx:
                    wallget.main(["--category", "all", "--assets", "all", "--delete", "--yes"])
        self.assertEqual(ctx.exception.code, 1)

    def test_interactive_invalid_action(self):
        base = self.root / "aerials"
        write_catalog(base, entries=catalog(asset_count=1), strings=loctable())
        videos = base / "videos"
        videos.mkdir()
        with mock.patch.dict(wallget.__dict__, {
            "USER_AERIALS_BASE": str(base),
            "USER_MANIFEST_PATH": str(base / "manifest" / "entries.json"),
            "USER_VIDEO_PATH": str(videos),
            "ACTIVE_VIDEO_PATH": str(videos),
            "STORAGE_MODE": wallget.STORAGE_MODE_USER,
            "SYSTEM_AERIALS_RESOURCES": str(self.root / "absent"),
        }):
            with mock.patch.object(wallget, "remote_catalog_urls", return_value=[]):
                with mock.patch("builtins.input", side_effect=["1", "1", "q"]):
                    with self.assertRaises(SystemExit):
                        wallget.main([])

    def test_download_ignores_range_and_retries_416(self):
        payload = b"0123456789abcdef" * 16
        handler = functools.partial(RangeHandler, payload=payload)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/ignore.mov"
            target = self.root / "ignore.mov"
            (self.root / "ignore.mov.part").write_bytes(payload[:8])
            label, error = wallget.download_file(("Ignore", url, str(target), len(payload)))
            self.assertIsNone(error)
            self.assertEqual(target.read_bytes(), payload)

            url = f"http://127.0.0.1:{server.server_port}/asset.mov"
            target = self.root / "resume.mov"
            (self.root / "resume.mov.part").write_bytes(payload + b"extra")
            label, error = wallget.download_file(("Resume", url, str(target), None))
            self.assertIsNone(error)
            self.assertEqual(target.read_bytes(), payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_download_incomplete_body_reports_error(self):
        payload = b"0123456789abcdef" * 16
        handler = functools.partial(RangeHandler, payload=payload)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/short.mov"
            target = self.root / "short.mov"
            with mock.patch.object(wallget.time, "sleep", return_value=None):
                _label, error = wallget.download_file(("Short", url, str(target), len(payload)))
            self.assertIsNotNone(error)
            self.assertIn("Incomplete download", error)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @mock.patch.object(wallget, "system_resource_dirs", return_value=[])
    def test_resolve_manifest_loader_oserror_is_recorded(self, _no_system):
        base = self.root / "aerials"
        (base / "manifest").mkdir(parents=True)
        with mock.patch.object(wallget, "manifest_from_paths",
                               side_effect=OSError("permission denied")):
            with mock.patch.object(wallget, "SYSTEM_AERIALS_RESOURCES", str(self.root / "absent")):
                with mock.patch.object(wallget, "remote_catalog_urls", return_value=[]):
                    manifest, source, notes = wallget.resolve_manifest(str(base))
        self.assertIsNone(manifest)
        self.assertTrue(any("permission denied" in n for n in notes))

    def test_category_maps_and_subcategory_grouping(self):
        categories = [{
            "id": "PARENT",
            "subcategories": [{
                "id": "SUB", "preferredOrder": 2, "representativeAssetID": "A"
            }],
        }]
        maps = wallget.build_category_maps(categories)
        self.assertEqual(maps.subcategory_to_parent["SUB"], "PARENT")
        self.assertEqual(maps.representative_to_parent["A"], "PARENT")
        self.assertEqual(maps.subcat_primary_order_map["SUB"], 2)
        assets_map = {"A": {
            "id": "A", "localizedNameKey": "A", "subcategories": ["SUB"],
            "url-4K-SDR-240FPS": "https://x/a.mov",
        }}
        prepared = wallget.prepare_assets(
            assets_map, ["A"], {"A": "Name"}, maps, {"PARENT"}
        )
        self.assertEqual(prepared[0].categories, {"SUB", "PARENT"})
        self.assertEqual(prepared[0].subcat_primary_order, 2)

    def test_render_asset_groups_prints_rows(self):
        manifest = {"entries": catalog(asset_count=2),
                    "strings": {"NAME_0": "A", "NAME_1": "B", "CAT_NAME": "Cat"}}
        assets_map, order = wallget.extract_assets(manifest)
        cats = wallget.extract_categories(manifest)
        assets = wallget.prepare_assets(
            assets_map, order, manifest["strings"], wallget.build_category_maps(cats), {"CAT"}
        )
        grouped, _rows = wallget.build_asset_groups(assets, [("CAT", "Cat")], None, "All", {})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wallget.render_asset_groups(grouped)
        self.assertIn("1. A", out.getvalue())
        self.assertIn("2. B", out.getvalue())

    def test_interactive_no_assets_selected(self):
        base = self.root / "aerials"
        write_catalog(base, entries=catalog(asset_count=1), strings=loctable())
        videos = base / "videos"
        videos.mkdir()
        with mock.patch.dict(wallget.__dict__, {
            "USER_AERIALS_BASE": str(base),
            "USER_MANIFEST_PATH": str(base / "manifest" / "entries.json"),
            "USER_VIDEO_PATH": str(videos),
            "ACTIVE_VIDEO_PATH": str(videos),
            "STORAGE_MODE": wallget.STORAGE_MODE_USER,
            "SYSTEM_AERIALS_RESOURCES": str(self.root / "absent"),
        }):
            with mock.patch.object(wallget, "remote_catalog_urls", return_value=[]):
                with mock.patch("builtins.input", side_effect=["1", "999"]):
                    with self.assertRaises(SystemExit):
                        wallget.main([])

    def test_download_oversized_part_with_expected_size_is_reset(self):
        payload = b"0123456789abcdef" * 16
        handler = functools.partial(RangeHandler, payload=payload)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/asset.mov"
            target = self.root / "oversized.mov"
            (self.root / "oversized.mov.part").write_bytes(payload + b"extra")
            _label, error = wallget.download_file(
                ("Oversized", url, str(target), len(payload))
            )
            self.assertIsNone(error)
            self.assertEqual(target.read_bytes(), payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_download_aborts_when_disk_space_insufficient(self):
        base = self.root / "aerials"
        write_catalog(base, entries=catalog(asset_count=1), strings=loctable())
        videos = base / "videos"
        videos.mkdir()
        with mock.patch.dict(wallget.__dict__, {
            "USER_AERIALS_BASE": str(base),
            "USER_MANIFEST_PATH": str(base / "manifest" / "entries.json"),
            "USER_VIDEO_PATH": str(videos),
            "ACTIVE_VIDEO_PATH": str(videos),
            "STORAGE_MODE": wallget.STORAGE_MODE_USER,
            "SYSTEM_AERIALS_RESOURCES": str(self.root / "absent"),
        }):
            with mock.patch.object(wallget, "remote_catalog_urls", return_value=[]):
                with mock.patch.object(wallget, "get_content_length", return_value=1000):
                    with mock.patch.object(wallget.shutil, "disk_usage",
                                           return_value=mock.Mock(free=10)):
                        with self.assertRaises(SystemExit) as ctx:
                            wallget.main(["--download", "--yes"])
        self.assertEqual(ctx.exception.code, 1)

    def test_tar_strings_variant_path(self):
        path = self.root / "variant.tar"
        with tarfile.open(path, "w") as archive:
            data = json.dumps(catalog(asset_count=1)).encode()
            info = tarfile.TarInfo("entries.json")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            name = "TVIdleScreenStrings.bundle/Localizable.nocache.loctable"
            raw = plistlib.dumps(loctable())
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
        manifest = wallget.manifest_from_tar(str(path))
        self.assertEqual(manifest["strings"]["NAME_0"], "First")

    def test_system_resource_dirs_deduplicates(self):
        directory = self.root / "Resources"
        directory.mkdir()
        with mock.patch.object(wallget, "SYSTEM_AERIALS_RESOURCES", str(directory)):
            with mock.patch.object(wallget.glob, "glob", return_value=[str(directory)]):
                self.assertEqual(wallget.system_resource_dirs(), [str(directory)])

    def test_macos_major_and_feed_order(self):
        with mock.patch.object(wallget.platform, "mac_ver",
                               return_value=("28.1", ("", "", ""), "")):
            self.assertEqual(wallget.macos_major(), "28")
        seen = []

        def fake_read(url, timeout=10):
            seen.append(url)
            major = url.split("resources-config-", 1)[1].split("-", 1)[0]
            return plistlib.dumps({
                "resources-url": f"https://sylvan.apple.com/{major}.tar"
            })

        base = self.root / "aerials"
        base.mkdir()
        with mock.patch.object(wallget, "macos_major", return_value="28"):
            with mock.patch.object(wallget.subprocess, "run", side_effect=OSError):
                with mock.patch.object(wallget, "read_url", side_effect=fake_read):
                    urls = wallget.remote_catalog_urls(str(base))
        self.assertTrue(seen[0].endswith("resources-config-28-0.plist"))
        self.assertTrue(urls[0].endswith("/28.tar"))

    def test_format_bytes_edges(self):
        self.assertEqual(wallget.format_bytes(1), "1 byte")
        self.assertIn("KB", wallget.format_bytes(2048))


if __name__ == "__main__":
    unittest.main()
