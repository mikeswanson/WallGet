import argparse
import glob
import http.client
import io
import json
import locale
import os
import platform
import plistlib
import pwd
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from multiprocessing.pool import ThreadPool
from typing import Any, Dict, Iterable, List, NoReturn, Optional, Set, Tuple

LEGACY_IDLEASSETSD_PATH = "/Library/Application Support/com.apple.idleassetsd"
LEGACY_STRINGS_PATH = f"{LEGACY_IDLEASSETSD_PATH}/Customer/TVIdleScreenStrings.bundle/en.lproj/Localizable.nocache.strings"
LEGACY_ENTRIES_PATH = f"{LEGACY_IDLEASSETSD_PATH}/Customer/entries.json"
LEGACY_VIDEO_PATH = f"{LEGACY_IDLEASSETSD_PATH}/Customer/4KSDR240FPS"
ASSET_URL_KEY = "url-4K-SDR-240FPS"
ASSET_URL_KEYS = (ASSET_URL_KEY, "url-4K-SDR", "url-4K", "url-1080", "url")
SYSTEM_AERIALS_RESOURCES = os.path.join(
    "/System", "Library", "ExtensionKit", "Extensions",
    "WallpaperAerialsExtension.appex", "Contents", "Resources",
)
CONFIG_FEED_BASE = (
    "https://configuration.apple.com/configurations/"
    "internetservices/aerials/resources-config-{}-0.plist"
)
SYLVAN_BASE = "https://sylvan.apple.com"
STORAGE_MODE_USER = "user"
STORAGE_MODE_LEGACY = "legacy"
ACTION_DOWNLOAD = "d"
ACTION_DELETE = "x"
HTTP_TIMEOUT = 30
MAX_WORKERS = 4
PARTIAL_SUFFIX = ".part"


@dataclass
class PreparedAsset:
    id: str
    label: str
    url: str
    ext: str
    preferred_order: Optional[int]
    manifest_index: int
    subcat_primary_order: Optional[int]
    subcat_group_order: Optional[int]
    categories: Set[str]
    subcategory_ids: Set[str]


@dataclass
class AssetStatus:
    existing_paths: List[str]
    content_length: Optional[int] = None
    up_to_date: bool = False

    def resolved_size(self) -> Optional[int]:
        if isinstance(self.content_length, int) and self.content_length > 0:
            return self.content_length
        if self.existing_paths:
            try:
                return os.path.getsize(self.existing_paths[0])
            except OSError:
                return None
        return None


@dataclass
class AssetDisplayRow:
    index: int
    asset: PreparedAsset
    label: str
    status: str
    size: str


@dataclass
class CategoryMaps:
    """Lookup tables derived from the manifest's category definitions."""

    subcategory_to_parent: Dict[str, str]
    representative_to_parent: Dict[str, str]
    subcat_primary_order_map: Dict[str, int]


def resolve_user_home() -> str:
    """Return the real user's home, including under sudo (issue #13 style false negatives)."""
    try:
        elevated = os.geteuid() == 0
    except AttributeError:
        elevated = False
    sudo_user = os.environ.get("SUDO_USER") if elevated else None
    if sudo_user and sudo_user != "root":
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            home = os.environ.get("HOME", "")
            if home and not home.startswith("/var/root"):
                return home
            return os.path.expanduser(f"~{sudo_user}")
    return os.path.expanduser("~")


USER_HOME = resolve_user_home()
USER_AERIALS_BASE = os.path.join(
    USER_HOME, "Library", "Application Support", "com.apple.wallpaper", "aerials"
)
USER_MANIFEST_PATH = os.path.join(USER_AERIALS_BASE, "manifest", "entries.json")
# macOS 26+ uses .loctable; earlier versions use .strings. Discover both layouts
# rather than freezing one path at import time (issue #13 partial manifests).
USER_STRINGS_PATH = ""
USER_VIDEO_PATH = os.path.join(USER_AERIALS_BASE, "videos")


def find_strings_path(base_dir: str) -> str:
    """Find a localization table under a manifest directory, if present."""
    bundle = os.path.join(base_dir, "TVIdleScreenStrings.bundle")
    roots = [bundle] if os.path.isdir(bundle) else []
    roots.append(base_dir)
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                if name in (
                    "Localizable.nocache.loctable",
                    "Localizable.nocache.strings",
                    "Localizable.loctable",
                    "Localizable.strings",
                ):
                    return os.path.join(dirpath, name)
    return ""


def detect_storage_mode() -> Optional[str]:
    """Pick the live storage location without requiring a complete manifest."""
    if os.path.isdir(USER_AERIALS_BASE):
        return STORAGE_MODE_USER
    if os.path.isfile(LEGACY_ENTRIES_PATH):
        return STORAGE_MODE_LEGACY
    return None


STORAGE_MODE = detect_storage_mode()
if STORAGE_MODE == STORAGE_MODE_USER:
    ACTIVE_ENTRIES_PATH = USER_MANIFEST_PATH
    USER_STRINGS_PATH = find_strings_path(os.path.join(USER_AERIALS_BASE, "manifest"))
    ACTIVE_STRINGS_PATH = USER_STRINGS_PATH
    ACTIVE_VIDEO_PATH = USER_VIDEO_PATH
elif STORAGE_MODE == STORAGE_MODE_LEGACY:
    ACTIVE_ENTRIES_PATH = LEGACY_ENTRIES_PATH
    ACTIVE_STRINGS_PATH = LEGACY_STRINGS_PATH
    ACTIVE_VIDEO_PATH = LEGACY_VIDEO_PATH
else:
    ACTIVE_ENTRIES_PATH = ""
    ACTIVE_STRINGS_PATH = ""
    ACTIVE_VIDEO_PATH = ""


def _safe_dict(value: object) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: object) -> List[Any]:
    return value if isinstance(value, list) else []


def _locale_candidates() -> List[str]:
    candidates: List[str] = []
    for name in ("LC_ALL", "LANG", "LANGUAGE"):
        value = os.environ.get(name, "")
        if value and value != "C":
            for part in value.replace(":", ",").split(","):
                cleaned = part.split("@", 1)[0].split(".", 1)[0]
                if cleaned:
                    candidates.append(cleaned)
    try:
        value = locale.getlocale()[0]
    except (ValueError, OSError):
        value = None
    if value:
        candidates.append(value)
    normalized: List[str] = []
    for value in candidates:
        value = value.replace("-", "_").lower()
        if value and value not in normalized:
            normalized.append(value)
    # macOS 'zh-Hans' style tags become zh_hans; 'en_US' is already close.
    expanded: List[str] = []
    for value in normalized:
        parts = value.split("_")
        for count in range(1, len(parts) + 1):
            prefix = "_".join(parts[:count])
            if prefix and prefix not in expanded:
                expanded.append(prefix)
    for fallback in ("en", "en_us"):
        if fallback not in expanded:
            expanded.append(fallback)
    return expanded


def _pick_loctable_strings(raw: object) -> Dict[str, str]:
    """Select one locale dict from a .loctable plist.

    Locale keys are dicts; metadata keys (e.g. LocProvenance) are not. Deciding
    from a majority rather than the first key avoids dropping every asset when
    a metadata key sorts before the locales.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    locale_keys = [k for k, v in raw.items() if isinstance(v, dict)]
    if not locale_keys:
        # Already a flat key -> string table.
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    normalized = {str(k).replace("-", "_").lower(): v for k, v in raw.items()
                  if isinstance(v, dict)}
    for candidate in _locale_candidates():
        table = normalized.get(candidate)
        if isinstance(table, dict):
            return {str(k): str(v) for k, v in table.items() if isinstance(v, str)}
    first = raw.get(locale_keys[0])
    return ({str(k): str(v) for k, v in first.items() if isinstance(v, str)}
            if isinstance(first, dict) else {})


def _parse_strings(data: bytes, name: str) -> Dict[str, str]:
    raw = plistlib.loads(data)
    if name.endswith(".loctable") or (
        isinstance(raw, dict)
        and any(isinstance(v, dict) for v in raw.values())
    ):
        return _pick_loctable_strings(raw)
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    return {}


def parse_manifest(
    entries_data: bytes,
    strings_data: Optional[bytes],
    strings_name: str = "",
) -> Optional[Dict[str, object]]:
    """Parse catalog bytes from disk, a tar, or a download. Never raises."""
    try:
        entries = json.loads(entries_data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    entries = _safe_dict(entries)
    if not _safe_list(entries.get("assets")):
        return None
    strings: Dict[str, str] = {}
    if strings_data:
        try:
            strings = _parse_strings(strings_data, strings_name)
        except (ValueError, TypeError, plistlib.InvalidFileException):
            strings = {}
    return {"entries": entries, "strings": strings}


def manifest_from_paths(entries_path: str, strings_path: str) -> Optional[Dict[str, object]]:
    if not (entries_path and os.path.isfile(entries_path)):
        return None
    try:
        with open(entries_path, "rb") as fp:
            entries_data = fp.read()
        strings_data = None
        name = strings_path
        if strings_path and os.path.isfile(strings_path):
            with open(strings_path, "rb") as fp:
                strings_data = fp.read()
    except OSError:
        return None
    return parse_manifest(entries_data, strings_data, name)


def load_manifest(entries_path: str, strings_path: str) -> Optional[Dict[str, object]]:
    """Compatibility wrapper used by callers and tests."""
    return manifest_from_paths(entries_path, strings_path)


def _tar_members(archive: tarfile.TarFile) -> Dict[str, Any]:
    wanted_entries = ("entries.json", "./entries.json")
    strings_names = ("Localizable.nocache.loctable", "Localizable.nocache.strings")
    entries_data = strings_data = None
    strings_name = ""
    for member in archive.getmembers():
        if not member.isfile():
            continue
        name = member.name[2:] if member.name.startswith("./") else member.name
        if name in wanted_entries and entries_data is None:
            handle = archive.extractfile(member)
            entries_data = handle.read() if handle else None
        elif (name.startswith("TVIdleScreenStrings.bundle/")
              and os.path.basename(name) in strings_names
              and strings_data is None):
            handle = archive.extractfile(member)
            strings_data = handle.read() if handle else None
            strings_name = name
    if entries_data is None:
        return {}
    result: Dict[str, Any] = {"entries": entries_data}
    if strings_data is not None:
        result["strings"] = strings_data
        result["strings_name"] = strings_name
    return result


def manifest_from_tar(path: str) -> Optional[Dict[str, object]]:
    if not os.path.isfile(path):
        return None
    try:
        with tarfile.open(path, "r:*") as archive:
            members = _tar_members(archive)
    except (OSError, tarfile.TarError):
        return None
    if not members:
        return None
    return parse_manifest(
        members["entries"], members.get("strings"), members.get("strings_name", "")
    )


def read_url(url: str, timeout: int = HTTP_TIMEOUT) -> Optional[bytes]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "WallGet/1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (OSError, http.client.HTTPException, ValueError):
        return None


def _trusted_catalog_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in (
        "sylvan.apple.com", "configuration.apple.com"
    )


def system_resource_dirs() -> List[str]:
    """All plausible locations of the OS-bundled aerial catalog."""
    pattern = os.path.join(
        "/System", "Library", "ExtensionKit", "Extensions",
        "WallpaperAerialsExtension*.appex", "Contents", "Resources",
    )
    candidates = [SYSTEM_AERIALS_RESOURCES] + sorted(glob.glob(pattern))
    result: List[str] = []
    seen: Set[str] = set()
    for directory in candidates:
        if directory and directory not in seen and os.path.isdir(directory):
            seen.add(directory)
            result.append(directory)
    return result


def macos_major() -> Optional[str]:
    version = platform.mac_ver()[0] or ""
    major = version.split(".", 1)[0]
    return major if major.isdigit() else None


def remote_catalog_urls(user_base: str) -> List[str]:
    """Resolve Apple's current catalog URL; never hardcode a tar filename."""
    urls: List[str] = []
    source_path = os.path.join(user_base, "manifest", "manifest.source")
    try:
        with open(source_path, encoding="utf-8") as fp:
            source = fp.read().strip()
        candidate = SYLVAN_BASE + source if source.startswith("/") else ""
        if candidate and _trusted_catalog_url(candidate):
            urls.append(candidate)
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["defaults", "read", "com.apple.wallpaper.aerial", "remoteResourceURL"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        value = result.stdout.decode("utf-8", "ignore").strip()
        if value.startswith("https://") and _trusted_catalog_url(value):
            urls.append(value)
    except (OSError, subprocess.SubprocessError):
        pass
    majors: List[str] = []
    for major in (macos_major(), "27", "26"):
        if major and major not in majors:
            majors.append(major)
    for major in majors:
        plist_data = read_url(CONFIG_FEED_BASE.format(major), timeout=10)
        if not plist_data:
            continue
        try:
            feed = plistlib.loads(plist_data)
        except plistlib.InvalidFileException:
            continue
        value = _safe_dict(feed).get("resources-url")
        if (isinstance(value, str) and value.startswith("https://")
                and _trusted_catalog_url(value)):
            urls.append(value)
    seen: Set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def resolve_manifest(user_base: str) -> Tuple[Optional[Dict[str, object]], str, List[str]]:
    """Find a usable catalog from local, bundled or remote sources.

    Returns (manifest, source description, diagnostics). All sources are
    read-only: macOS remains the owner of its manifest files.
    """
    notes: List[str] = []
    manifest_dir = os.path.join(user_base, "manifest")
    entries_path = os.path.join(manifest_dir, "entries.json")
    candidates = [
        ("local manifest", lambda: manifest_from_paths(
            entries_path, find_strings_path(manifest_dir))),
        ("manifest.tar", lambda: manifest_from_tar(
            os.path.join(user_base, "manifest.tar"))),
    ]
    for number, directory in enumerate(system_resource_dirs(), start=1):
        label = "system bundle" if number == 1 else f"system bundle #{number}"
        candidates.append((label, lambda d=directory: manifest_from_paths(
            os.path.join(d, "entries.json"), find_strings_path(d))))
    for label, loader in candidates:
        try:
            manifest = loader()
        except OSError as exc:
            notes.append(f"{label}: {exc}")
            continue
        notes.append(f"{label}: {'ok' if manifest else 'unavailable'}")
        if manifest:
            return manifest, label, notes
    for url in remote_catalog_urls(user_base):
        data = read_url(url)
        if not data:
            notes.append(f"download failed: {url}")
            continue
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
                members = _tar_members(archive)
        except tarfile.TarError as exc:
            notes.append(f"invalid download: {url} ({exc})")
            continue
        manifest = parse_manifest(
            members.get("entries", b""), members.get("strings"),
            members.get("strings_name", ""),
        ) if members else None
        notes.append(f"download {url}: {'ok' if manifest else 'unusable'}")
        if manifest:
            return manifest, "Apple catalog download", notes
    return None, "", notes


def extract_strings(manifest: Dict[str, object]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    merged.update(_safe_dict(manifest.get("strings")))
    return merged


def extract_categories(manifest: Dict[str, object]) -> List[Dict[str, object]]:
    categories: Dict[str, Dict[str, object]] = {}
    order: List[str] = []
    for category in _safe_list(_safe_dict(manifest.get("entries")).get("categories")):
        category = _safe_dict(category)
        cat_id = category.get("id")
        if not cat_id:
            continue
        if cat_id not in categories:
            categories[cat_id] = dict(category)
            order.append(cat_id)
        else:
            categories[cat_id].update(category)
    return [categories[cat_id] for cat_id in order]


def build_category_maps(categories: Iterable[Dict[str, object]]) -> CategoryMaps:
    """Normalize category metadata into lookup tables used during preparation."""

    subcategory_to_parent: Dict[str, str] = {}
    subcat_primary_order_map: Dict[str, int] = {}
    representative_to_parent: Dict[str, str] = {}
    for category in categories:
        parent_id = category.get("id")
        for sub in _safe_list(category.get("subcategories")):
            sub = _safe_dict(sub)
            sub_id = sub.get("id")
            if sub_id:
                subcategory_to_parent[sub_id] = parent_id
                preferred_order = sub.get("preferredOrder")
                if isinstance(preferred_order, int):
                    subcat_primary_order_map[sub_id] = preferred_order
            rep_id = sub.get("representativeAssetID")
            if rep_id:
                representative_to_parent[rep_id] = parent_id
    return CategoryMaps(
        subcategory_to_parent=subcategory_to_parent,
        representative_to_parent=representative_to_parent,
        subcat_primary_order_map=subcat_primary_order_map,
    )


def extract_assets(
    manifest: Dict[str, object],
) -> Tuple[Dict[str, Dict[str, object]], List[str]]:
    assets: Dict[str, Dict[str, object]] = {}
    order: List[str] = []
    for asset in _safe_list(_safe_dict(manifest.get("entries")).get("assets")):
        asset = _safe_dict(asset)
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not asset_id:
            continue
        if asset_id not in assets:
            order.append(asset_id)
        assets[asset_id] = asset
    return assets, order


def prepare_assets(
    assets: Dict[str, Dict[str, object]],
    order: List[str],
    strings: Dict[str, str],
    category_maps: CategoryMaps,
    top_level_categories: Set[str],
) -> List[PreparedAsset]:
    """Flatten manifest entries into PreparedAsset records with derived metadata."""

    prepared: List[PreparedAsset] = []
    subcategory_asset_min: Dict[str, int] = {}
    subcategory_assets: Dict[str, List[PreparedAsset]] = defaultdict(list)

    def include_category(
        cat_id: Optional[str],
        *,
        categories: Set[str],
        subcat_ids: Set[str],
        subcat_orders: List[int],
        track_subcategory: bool,
    ) -> None:
        """Add a category (and its parent/order metadata) to the accumulating asset."""

        if not cat_id:
            return
        categories.add(cat_id)
        parent = category_maps.subcategory_to_parent.get(cat_id)
        if parent:
            categories.add(parent)
        preferred_order = category_maps.subcat_primary_order_map.get(cat_id)
        if preferred_order is not None:
            subcat_orders.append(preferred_order)
        if track_subcategory:
            subcat_ids.add(cat_id)

    def recompute_group_order(target: PreparedAsset) -> None:
        """Refresh an asset's group order using the lowest known subcategory order."""

        group_orders = [
            subcategory_asset_min[sub_id]
            for sub_id in target.subcategory_ids
            if sub_id in subcategory_asset_min
        ]
        target.subcat_group_order = min(group_orders) if group_orders else None

    for manifest_index, asset_id in enumerate(order):
        asset = assets.get(asset_id, {})
        name_key = asset.get("localizedNameKey")
        label = strings.get(name_key, "") if isinstance(name_key, str) else ""
        if not label:
            label = name_key if isinstance(name_key, str) and name_key else asset_id
        url = ""
        for key in ASSET_URL_KEYS:
            candidate = asset.get(key)
            if isinstance(candidate, str) and candidate:
                url = candidate
                break
        if not url:
            continue
        parsed = urllib.parse.urlparse(url)
        ext = os.path.splitext(parsed.path)[1] or ".mov"

        categories: Set[str] = set()
        subcat_ids: Set[str] = set()
        subcat_orders: List[int] = []

        for cat_id in _safe_list(asset.get("categories")):
            include_category(
                cat_id,
                categories=categories,
                subcat_ids=subcat_ids,
                subcat_orders=subcat_orders,
                track_subcategory=False,
            )
        for sub_id in _safe_list(asset.get("subcategories")):
            include_category(
                sub_id,
                categories=categories,
                subcat_ids=subcat_ids,
                subcat_orders=subcat_orders,
                track_subcategory=True,
            )

        if not categories & top_level_categories:
            implied_parent = category_maps.representative_to_parent.get(asset_id)
            if implied_parent:
                categories.add(implied_parent)
            elif asset.get("showInTopLevel") and top_level_categories:
                categories.add(next(iter(top_level_categories)))

        primary_subcat_order = min(subcat_orders) if subcat_orders else None
        preferred_order = (
            asset.get("preferredOrder")
            if isinstance(asset.get("preferredOrder"), int)
            else None
        )

        prepared_asset = PreparedAsset(
            id=asset_id,
            label=label,
            url=url,
            ext=ext,
            preferred_order=preferred_order,
            manifest_index=manifest_index,
            subcat_primary_order=primary_subcat_order,
            subcat_group_order=None,
            categories=categories,
            subcategory_ids=subcat_ids,
        )
        prepared.append(prepared_asset)

        for sub_id in subcat_ids:
            subcategory_assets[sub_id].append(prepared_asset)

        if isinstance(preferred_order, int):
            for sub_id in subcat_ids:
                current = subcategory_asset_min.get(sub_id)
                if current is None or preferred_order < current:
                    subcategory_asset_min[sub_id] = preferred_order
                    for linked_asset in subcategory_assets[sub_id]:
                        recompute_group_order(linked_asset)

        recompute_group_order(prepared_asset)

    return prepared


def existing_files(asset_id: str, ext: str) -> List[str]:
    if not ACTIVE_VIDEO_PATH:
        return []
    path = os.path.join(ACTIVE_VIDEO_PATH, f"{asset_id}{ext}")
    return [path] if os.path.isfile(path) else []


def assess_asset_status(
    task: Tuple[str, str, List[str]],
) -> Tuple[str, Optional[int], bool]:
    asset_id, url, existing = task
    try:
        content_length = get_content_length(url)
    except (OSError, http.client.HTTPException):
        # Treat an unreachable CDN as "size unknown" instead of aborting
        # the whole status pass.
        content_length = -1
    if content_length <= 0:
        content_length = None
    up_to_date = False
    if content_length is not None:
        for path in existing:
            if os.path.getsize(path) == content_length:
                up_to_date = True
                break
    elif existing:
        up_to_date = True
    return asset_id, content_length, up_to_date


def gather_asset_status(
    applicable_assets: List[PreparedAsset],
    check_remote: bool = True,
) -> Dict[str, AssetStatus]:
    """Check remote file sizes to determine which assets need work.

    Deleting never needs remote sizes; skipping them keeps delete usable
    offline and avoids hundreds of CDN requests.
    """
    print("\nGathering asset status...", end="")
    asset_status: Dict[str, AssetStatus] = {}
    status_tasks: List[Tuple[str, str, List[str]]] = []
    for asset in applicable_assets:
        existing = existing_files(asset.id, asset.ext)
        asset_status[asset.id] = AssetStatus(existing_paths=existing)
        status_tasks.append((asset.id, asset.url, existing))

    if not check_remote:
        for asset_id, _url, existing in status_tasks:
            asset_status[asset_id].up_to_date = bool(existing)
        print("done.\n")
        return asset_status

    pool = ThreadPool(processes=MAX_WORKERS)
    completed_status_checks = 0
    try:
        for asset_id, content_length, up_to_date in pool.imap_unordered(
            assess_asset_status, status_tasks
        ):
            info = asset_status.get(asset_id)
            if not info:
                continue
            info.content_length = content_length
            info.up_to_date = up_to_date
            completed_status_checks += 1
            if completed_status_checks % 5 == 0:
                print(".", end="", flush=True)
    finally:
        pool.close()
        pool.join()
    print("done.\n")
    return asset_status


def summarize_asset_status(
    asset: PreparedAsset, status: AssetStatus
) -> Tuple[str, str]:
    status_text = "downloaded" if status.up_to_date else ""
    size_bytes = status.resolved_size()
    size_text = format_bytes(size_bytes) if size_bytes is not None else "-"
    return status_text, size_text


def asset_sort_key(asset: PreparedAsset) -> Tuple[int, int, int, int]:
    sentinel = 1_000_000
    manifest_index = asset.manifest_index
    group_key = (
        asset.subcat_group_order
        if isinstance(asset.subcat_group_order, int)
        else sentinel
    )
    sub_key = (
        asset.subcat_primary_order
        if isinstance(asset.subcat_primary_order, int)
        else sentinel
    )
    preferred_key = (
        asset.preferred_order
        if isinstance(asset.preferred_order, int)
        else sentinel + manifest_index
    )
    return group_key, sub_key, preferred_key, manifest_index


def sort_assets_for_display(
    assets_list: Iterable[PreparedAsset],
) -> List[PreparedAsset]:
    return sorted(assets_list, key=asset_sort_key)


def build_asset_groups(
    applicable_assets: List[PreparedAsset],
    category_menu: List[Tuple[str, str]],
    category_id: Optional[str],
    selected_category_name: str,
    asset_status: Dict[str, AssetStatus],
) -> Tuple[List[Tuple[str, List[AssetDisplayRow]]], List[AssetDisplayRow]]:
    """Group assets for display and selection, returning grouped and flat views."""

    grouped_rows: List[Tuple[str, List[AssetDisplayRow]]] = []
    all_rows: List[AssetDisplayRow] = []
    index_counter = 1

    def make_row(prepared_asset: PreparedAsset) -> AssetDisplayRow:
        nonlocal index_counter
        status = asset_status.get(prepared_asset.id, AssetStatus(existing_paths=[]))
        status_text, size_text = summarize_asset_status(prepared_asset, status)
        row = AssetDisplayRow(
            index=index_counter,
            asset=prepared_asset,
            label=prepared_asset.label,
            status=status_text,
            size=size_text,
        )
        all_rows.append(row)
        index_counter += 1
        return row

    if category_id:
        group_assets = sort_assets_for_display(applicable_assets)
        group_rows = [make_row(asset) for asset in group_assets]
        if group_rows:
            grouped_rows.append((selected_category_name, group_rows))
        return grouped_rows, all_rows

    seen_assets: Set[str] = set()
    for cat_id, cat_name in category_menu:
        cat_assets = [
            asset
            for asset in applicable_assets
            if cat_id in asset.categories and asset.id not in seen_assets
        ]
        if not cat_assets:
            continue
        group_rows = []
        for asset in sort_assets_for_display(cat_assets):
            group_rows.append(make_row(asset))
            seen_assets.add(asset.id)
        grouped_rows.append((cat_name, group_rows))

    remaining_assets = [
        asset for asset in applicable_assets if asset.id not in seen_assets
    ]
    if remaining_assets:
        group_rows = [
            make_row(asset) for asset in sort_assets_for_display(remaining_assets)
        ]
        grouped_rows.append(("Other", group_rows))

    return grouped_rows, all_rows


def render_asset_groups(grouped_rows: List[Tuple[str, List[AssetDisplayRow]]]) -> None:
    if not grouped_rows:
        return

    all_rows = [row for _, rows in grouped_rows for row in rows]
    if not all_rows:
        return

    index_width = len(str(all_rows[-1].index))
    label_width = max(len(row.label) for row in all_rows)
    status_width = len("downloaded")
    size_width = max(len(row.size) for row in all_rows)

    first_group = True
    for heading, rows in grouped_rows:
        if not rows:
            continue
        if not first_group:
            print()
        first_group = False
        print(heading)
        for row in rows:
            index_part = str(row.index).rjust(index_width)
            label_part = row.label.ljust(label_width)
            status_part = row.status.ljust(status_width)
            size_part = row.size.rjust(size_width)
            print(f"{index_part}. {label_part}  {status_part}  {size_part}")


def parse_selection(selection: str, max_choice: int) -> Set[int]:
    chosen: Set[int] = set()
    parts = selection.split(",")
    for part in parts:
        token = part.strip()
        if not token:
            continue
        if token.lower() == "all":
            # max_choice is the trailing "All" entry, which main() expands.
            chosen.add(max_choice)
            continue
        if "-" in token:
            start_str, end_str = token.split("-", 1)
            start = as_int(start_str.strip())
            end = as_int(end_str.strip())
            if start <= 0 or end <= 0 or start > end:
                continue
            for value in range(start, end + 1):
                if 1 <= value <= max_choice:
                    chosen.add(value)
        else:
            value = as_int(token)
            if 1 <= value <= max_choice:
                chosen.add(value)
    return chosen


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Download or delete macOS aerial wallpaper videos. "
        "Run without arguments for the interactive menu."
    )
    parser.add_argument(
        "--category",
        metavar="CATEGORY",
        help="category id or name (case-insensitive), or 'all'",
    )
    parser.add_argument(
        "--assets",
        metavar="SELECTION",
        help="asset numbers, e.g. '1-4,8', or 'all' (defaults to all)",
    )
    action_group = parser.add_mutually_exclusive_group(required=False)
    action_group.add_argument(
        "--download", action="store_true", help="download missing files"
    )
    action_group.add_argument(
        "--delete", action="store_true", help="delete selected files"
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip confirmation prompts (non-interactive)",
    )
    args = parser.parse_args(argv)

    if args.delete and not (args.category or args.assets):
        parser.error(
            "--delete requires --category or --assets; "
            "use --assets all to delete every downloaded wallpaper"
        )

    if STORAGE_MODE == STORAGE_MODE_LEGACY:
        try:
            elevated = os.geteuid() == 0
        except AttributeError:
            elevated = True
        if not elevated:
            fail(f'Please run as admin: sudo python3 "{__file__}"')

    non_interactive = bool(
        args.category or args.assets or args.download or args.delete
    )
    if non_interactive and not (args.download or args.delete):
        parser.error("--download or --delete is required outside interactive mode")

    print("WallGet Live Wallpaper Download/Delete Script")
    print("---------------------------------------------\n")

    notes: List[str] = []
    if STORAGE_MODE == STORAGE_MODE_USER:
        manifest, source, notes = resolve_manifest(USER_AERIALS_BASE)
        if manifest and source and source != "local manifest":
            print(f"Catalog source: {source} (local manifest unavailable).\n")
    elif STORAGE_MODE == STORAGE_MODE_LEGACY:
        manifest = load_manifest(ACTIVE_ENTRIES_PATH, ACTIVE_STRINGS_PATH)
        source = "legacy manifest"
    else:
        manifest, source = None, ""
        fail(
            "Unable to locate a wallpaper store.\n"
            f"  Checked: {USER_AERIALS_BASE}\n"
            f"  Checked: {LEGACY_ENTRIES_PATH}\n"
            "Open System Settings > Wallpaper once so macOS creates its aerials store."
        )

    if not manifest:
        details = "\n".join(f"  - {note}" for note in notes) if notes else ""
        fail(
            "Unable to load a usable wallpaper catalog.\n"
            + (details + "\n" if details else "")
            + f"  Local manifest: {USER_MANIFEST_PATH}\n"
            + f"  Local archive:  {os.path.join(USER_AERIALS_BASE, 'manifest.tar')}\n"
            + "If the manifest folder is empty, open System Settings > Wallpaper\n"
            + "and view the aerial wallpapers once, then run WallGet again."
        )

    strings = extract_strings(manifest)
    categories = extract_categories(manifest)
    if not categories:
        fail("No categories found in the available manifests.")

    category_maps = build_category_maps(categories)
    top_level_category_ids: Set[str] = {
        cat.get("id") for cat in categories if isinstance(cat.get("id"), str)
    }

    assets_map, asset_order = extract_assets(manifest)
    assets = prepare_assets(
        assets_map,
        asset_order,
        strings,
        category_maps,
        top_level_category_ids,
    )

    if not assets:
        fail(
            "No assets available to process. The catalog is readable but its "
            "assets lack usable names or URLs."
        )

    category_menu: List[Tuple[str, str]] = []
    category_asset_counts: Dict[str, int] = defaultdict(int)
    for asset in assets:
        for category_id in asset.categories & top_level_category_ids:
            category_asset_counts[category_id] += 1
    for category in categories:
        category_id = category.get("id")
        if not category_id:
            continue
        localized_key = category.get("localizedNameKey", "")
        name = strings.get(localized_key, category_id)
        category_menu.append((category_id, name))

    if not non_interactive:
        number_width = len(str(len(category_menu) + 1))
        name_width = max(
            [len(name) for _, name in category_menu] + [len("All")]
        )

        for idx, (cat_id, name) in enumerate(category_menu, start=1):
            count = category_asset_counts.get(cat_id, 0)
            print(
                f"{str(idx).rjust(number_width)}. {name.ljust(name_width)}  ({count})"
            )
        print(
            f"{str(len(category_menu) + 1).rjust(number_width)}. "
            f"{'All'.ljust(name_width)}  ({len(assets)})"
        )

        category_index = as_int(input("\nCategory number? "))
        if category_index < 1 or category_index > len(category_menu) + 1:
            print("\nNo category selected.")
            exit()
        category_id = (
            category_menu[category_index - 1][0]
            if category_index <= len(category_menu)
            else None
        )
    else:
        category_id = None
        if args.category:
            needle = args.category.strip().lower()
            if needle != "all":
                matches = [
                    cat_id
                    for cat_id, name in category_menu
                    if needle in (cat_id.lower(), name.lower())
                ]
                if len(matches) != 1:
                    listing = ", ".join(name for _, name in category_menu)
                    parser.error(
                        f"category '{args.category}' matched {len(matches)} "
                        f"categories (expected 1). Available: {listing}, all"
                    )
                category_id = matches[0]

    applicable_assets = [
        asset for asset in assets if not category_id or category_id in asset.categories
    ]
    if not applicable_assets:
        print("\nNo assets available for the selected category.")
        exit()

    selected_category_name = (
        next(name for cat_id, name in category_menu if cat_id == category_id)
        if category_id
        else "All"
    )

    asset_status = gather_asset_status(applicable_assets, check_remote=not args.delete)
    grouped_rows, display_rows = build_asset_groups(
        applicable_assets,
        category_menu,
        category_id,
        selected_category_name,
        asset_status,
    )
    if not non_interactive:
        render_asset_groups(grouped_rows)
        if display_rows:
            print()
    index_to_asset = {row.index: row.asset for row in display_rows}
    max_index = len(index_to_asset)

    if non_interactive:
        selection_raw = (args.assets or "all").strip()
    else:
        print(f"{max_index + 1}. All")
        selection_raw = input(
            "\nAsset numbers? (ranges/comma-separated, e.g. 1-4,8) "
        ).strip()
    max_choice = max_index + 1
    if not selection_raw:
        print("\nNo assets selected.")
        exit()
    selected_indices = parse_selection(selection_raw, max_choice)
    if not selected_indices:
        print("\nNo assets selected.")
        exit()
    if max_choice in selected_indices:
        selected_indices = set(index_to_asset.keys())
    else:
        selected_indices = {idx for idx in selected_indices if 1 <= idx <= max_index}

    if not selected_indices:
        print("\nNo assets selected.")
        exit()

    selected_assets = [
        index_to_asset[idx] for idx in sorted(selected_indices) if idx in index_to_asset
    ]

    if non_interactive:
        action = ACTION_DOWNLOAD if args.download else ACTION_DELETE
    else:
        action = input("\n(d)Download or (x)delete? (d/x) ").strip().lower()
        if action not in {ACTION_DOWNLOAD, ACTION_DELETE}:
            print("\nNo action selected.")
            exit()
    action_text = "download" if action == ACTION_DOWNLOAD else "delete"

    items: List[Tuple[str, str, str, Optional[int]]] = []
    total_bytes = 0
    delete_targets: List[Tuple[str, str]] = []
    queued_delete_paths: Set[str] = set()
    for asset in selected_assets:
        label = asset.label
        url = asset.url
        ext = asset.ext
        asset_id = asset.id

        status_info = asset_status.get(asset_id)
        existing = (
            list(status_info.existing_paths)
            if status_info and status_info.existing_paths
            else existing_files(asset_id, ext)
        )
        content_length = status_info.content_length if status_info else None
        up_to_date = status_info.up_to_date if status_info else False

        if action == ACTION_DOWNLOAD:
            if up_to_date:
                continue
            target_path = os.path.join(ACTIVE_VIDEO_PATH, f"{asset_id}{ext}")
            items.append(
                (
                    label,
                    url,
                    target_path,
                    content_length
                    if isinstance(content_length, int) and content_length > 0
                    else None,
                )
            )
            if isinstance(content_length, int) and content_length > 0:
                total_bytes += content_length
        else:
            for path in existing:
                if path in queued_delete_paths:
                    continue
                delete_targets.append((label, path))
                queued_delete_paths.add(path)
                total_bytes += os.path.getsize(path)

    print()

    if action == ACTION_DOWNLOAD:
        tasks = items
    else:
        tasks = delete_targets

    if not tasks:
        print(f"Nothing to {action_text}.\n\nDone.")
        return

    free_space = shutil.disk_usage("/").free
    print(f"Available space: {format_bytes(free_space)}")
    print(f"Files to {action_text} ({len(tasks)}): {format_bytes(total_bytes)}")
    if action == ACTION_DOWNLOAD and total_bytes > free_space:
        fail("Not enough disk space to download all files.")

    if not (args.yes or non_interactive):
        proceed = input(f"{action_text.capitalize()} files? (y/n) ").strip().lower()
        if proceed != "y":
            exit()

    if action == ACTION_DOWNLOAD:
        start_time = time.time()
        print("\nDownloading...")
        pool = ThreadPool(processes=MAX_WORKERS)
        downloaded = 0
        failures = 0
        try:
            for label, error in pool.imap_unordered(download_file, tasks):
                if error:
                    failures += 1
                    print(f"  FAILED '{label}': {error}")
                else:
                    downloaded += 1
                    print(f"  Downloaded '{label}'")
        except KeyboardInterrupt:
            failures += 1
            print(
                "\nInterrupted; partial transfers were kept as .part files "
                "and can be resumed by re-running this command."
            )
        finally:
            pool.terminate()
            pool.join()
        summary = (
            f"\nDownloaded {downloaded} of {len(tasks)} files "
            f"in {time.time() - start_time:.1f}s."
        )
        if failures:
            summary += f" ({failures} failed or interrupted; re-run to resume.)"
        print(summary)
    else:
        print("\nDeleting...")
        seen_paths: Set[str] = set()
        for label, path in tasks:
            if path in seen_paths or not os.path.isfile(path):
                continue
            os.remove(path)
            seen_paths.add(path)
            print(f"  Deleted '{label}'")
        print(f"\nDeleted {len(seen_paths)} files.")

    if STORAGE_MODE == STORAGE_MODE_LEGACY and not args.yes:
        should_kill = (
            input("\nKill idleassetsd to update download status in Settings? (y/n) ")
            .strip()
            .lower()
        )
        if should_kill == "y":
            os.system("killall idleassetsd")
            print("Killed idleassetsd.")

    print("\nDone.")


def as_int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return -1


def format_bytes(bytes: int) -> str:
    units = (
        (1 << 50, "PB"),
        (1 << 40, "TB"),
        (1 << 30, "GB"),
        (1 << 20, "MB"),
        (1 << 10, "KB"),
        (1, "bytes"),
    )
    if bytes == 1:
        return "1 byte"
    for factor, suffix in units:
        if bytes >= factor:
            break
    return f"{bytes / factor:.2f} {suffix}"


def connect(parsed_url: urllib.parse.ParseResult) -> http.client.HTTPConnection:
    # Default certificate verification is deliberate: these are multi-GB
    # downloads from Apple's CDN, and skipping verification would invite
    # silent tampering.
    context = ssl.create_default_context()
    conn = (
        http.client.HTTPSConnection(
            parsed_url.netloc, context=context, timeout=HTTP_TIMEOUT
        )
        if parsed_url.scheme == "https"
        else http.client.HTTPConnection(parsed_url.netloc, timeout=HTTP_TIMEOUT)
    )
    return conn


def request_path(parsed_url: urllib.parse.ParseResult) -> str:
    path = parsed_url.path or "/"
    if parsed_url.query:
        path = f"{path}?{parsed_url.query}"
    return path


def get_content_length(url: str) -> int:
    parsed_url = urllib.parse.urlparse(url)
    conn = connect(parsed_url)
    conn.request("HEAD", request_path(parsed_url))
    r = conn.getresponse()
    try:
        content_length = int(r.getheader("Content-Length", -1))
    except (TypeError, ValueError):
        content_length = -1
    conn.close()
    return content_length


def download_file(download: Tuple[str, str, str, Optional[int]]) -> Tuple[str, Optional[str]]:
    """Download one asset and atomically place it at its final path.

    Writes to a ``.part`` sibling file and renames on success, so a crash
    never leaves a partial video where macOS looks for real assets. When a
    previous ``.part`` exists and the server honours Range requests, the
    transfer resumes from where it stopped.

    ``expected_size`` (when known from the manifest HEAD check) lets an
    already-complete ``.part`` be finalized without re-downloading, and
    flags oversized leftovers for removal.

    Returns ``(label, None)`` on success or ``(label, error)`` on failure so
    one bad asset cannot abort the whole batch.
    """
    label, url, file_path, expected_size = download
    parsed_url = urllib.parse.urlparse(url)
    part_path = file_path + PARTIAL_SUFFIX

    def parse_total(content_range: Optional[str]) -> Optional[int]:
        if not content_range or "/" not in content_range:
            return None
        try:
            return int(content_range.rsplit("/", 1)[-1])
        except ValueError:
            return None

    def download_with_retries(max_retries: int = 5) -> None:
        for attempt_no in range(1, max_retries + 1):
            offset = os.path.getsize(part_path) if os.path.isfile(part_path) else 0
            if expected_size is not None and offset == expected_size:
                # A previous run finished the transfer but was interrupted
                # before the rename.
                os.replace(part_path, file_path)
                return
            if expected_size is not None and offset > expected_size:
                try:
                    os.remove(part_path)
                except OSError:
                    pass
                offset = 0
            conn = connect(parsed_url)
            try:
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                conn.request("GET", request_path(parsed_url), headers=headers)
                r = conn.getresponse()

                if r.status == 416 and offset:
                    # Server rejected the resume position: drop the partial
                    # and start clean on the next attempt.
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass
                    raise RuntimeError(f"HTTP {r.status}: {r.reason}")
                if offset and r.status == 200:
                    # Server ignored the Range header; start from scratch.
                    offset = 0
                elif r.status not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status}: {r.reason}")

                if offset and r.status == 206:
                    total = parse_total(r.getheader("Content-Range")) or expected_size
                else:
                    header_length = r.getheader("Content-Length")
                    total = int(header_length) if header_length else expected_size

                os.makedirs(os.path.dirname(file_path), exist_ok=True)

                bytes_written = offset
                CHUNK = 64 * 1024

                with open(part_path, "ab" if offset else "wb") as f:
                    while True:
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        bytes_written += len(chunk)

                if total is not None and bytes_written != total:
                    raise RuntimeError(
                        f"Incomplete download: expected {total}, got {bytes_written}"
                    )

                os.replace(part_path, file_path)
                return

            except Exception:
                # Keep the .part file so a later run can resume.
                if attempt_no == max_retries:
                    raise
                time.sleep(1 + attempt_no * 0.5)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    try:
        download_with_retries()
        return label, None
    except Exception as e:
        return label, str(e)



if __name__ == "__main__":
    main()
