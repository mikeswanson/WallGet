import http.client
import json
import os
import plistlib
import pwd
import shutil
import ssl
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from multiprocessing.pool import ThreadPool
from typing import Dict, Iterable, List, Optional, Set, Tuple

LEGACY_IDLEASSETSD_PATH = "/Library/Application Support/com.apple.idleassetsd"
LEGACY_STRINGS_PATH = f"{LEGACY_IDLEASSETSD_PATH}/Customer/TVIdleScreenStrings.bundle/en.lproj/Localizable.nocache.strings"
LEGACY_ENTRIES_PATH = f"{LEGACY_IDLEASSETSD_PATH}/Customer/entries.json"
LEGACY_VIDEO_PATH = f"{LEGACY_IDLEASSETSD_PATH}/Customer/4KSDR240FPS"
ASSET_URL_KEY = "url-4K-SDR-240FPS"
STORAGE_MODE_USER = "user"
STORAGE_MODE_LEGACY = "legacy"
ACTION_DOWNLOAD = "d"
ACTION_DELETE = "x"


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
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            return os.path.expanduser(f"~{sudo_user}")
    return os.path.expanduser("~")


USER_HOME = resolve_user_home()
USER_AERIALS_BASE = os.path.join(
    USER_HOME, "Library", "Application Support", "com.apple.wallpaper", "aerials"
)
USER_MANIFEST_PATH = os.path.join(USER_AERIALS_BASE, "manifest", "entries.json")
USER_STRINGS_PATH = os.path.join(
    USER_AERIALS_BASE,
    "manifest",
    "TVIdleScreenStrings.bundle",
    "en.lproj",
    "Localizable.nocache.strings",
)
USER_VIDEO_PATH = os.path.join(USER_AERIALS_BASE, "videos")


def detect_storage_mode() -> Optional[str]:
    user_available = os.path.isfile(USER_MANIFEST_PATH) and os.path.isfile(
        USER_STRINGS_PATH
    )
    if user_available:
        return STORAGE_MODE_USER
    legacy_available = os.path.isfile(LEGACY_ENTRIES_PATH) and os.path.isfile(
        LEGACY_STRINGS_PATH
    )
    if legacy_available:
        return STORAGE_MODE_LEGACY
    return None


STORAGE_MODE = detect_storage_mode()
if STORAGE_MODE == STORAGE_MODE_USER:
    ACTIVE_ENTRIES_PATH = USER_MANIFEST_PATH
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


def load_manifest(entries_path: str, strings_path: str) -> Optional[Dict[str, object]]:
    if not (
        entries_path
        and strings_path
        and os.path.isfile(entries_path)
        and os.path.isfile(strings_path)
    ):
        return None
    with open(entries_path) as fp:
        entries = json.load(fp)
    with open(strings_path, "rb") as fp:
        strings = plistlib.load(fp)
    return {"entries": entries, "strings": strings}


def extract_strings(manifest: Dict[str, object]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    merged.update(manifest.get("strings", {}))
    return merged


def extract_categories(manifest: Dict[str, object]) -> List[Dict[str, object]]:
    categories: Dict[str, Dict[str, object]] = {}
    order: List[str] = []
    for category in manifest.get("entries", {}).get("categories", []):
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
        for sub in category.get("subcategories", []):
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
    for asset in manifest.get("entries", {}).get("assets", []):
        asset_id = asset.get("id")
        if not asset_id:
            continue
        if asset_id not in assets:
            order.append(asset_id)
        assets[asset_id] = dict(asset)
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
        label = strings.get(asset.get("localizedNameKey", ""), "")
        url = asset.get(ASSET_URL_KEY, "")
        if not label or not url:
            continue
        parsed = urllib.parse.urlparse(url)
        ext = os.path.splitext(parsed.path)[1]
        if not ext:
            continue

        categories: Set[str] = set()
        subcat_ids: Set[str] = set()
        subcat_orders: List[int] = []

        for cat_id in (asset.get("categories") or []):
            include_category(
                cat_id,
                categories=categories,
                subcat_ids=subcat_ids,
                subcat_orders=subcat_orders,
                track_subcategory=False,
            )
        for sub_id in (asset.get("subcategories") or []):
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
    content_length = get_content_length(url)
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
) -> Dict[str, AssetStatus]:
    """Check remote file sizes to determine which assets need work."""

    print("\nGathering asset status...", end="")
    asset_status: Dict[str, AssetStatus] = {}
    status_tasks: List[Tuple[str, str, List[str]]] = []
    for asset in applicable_assets:
        existing = existing_files(asset.id, asset.ext)
        asset_status[asset.id] = AssetStatus(existing_paths=existing)
        status_tasks.append((asset.id, asset.url, existing))

    pool = ThreadPool()
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


def main():
    if STORAGE_MODE == STORAGE_MODE_LEGACY and os.geteuid() != 0:
        print(f'Please run as admin: sudo python3 "{__file__}"')
        exit()

    print("WallGet Live Wallpaper Download/Delete Script")
    print("---------------------------------------------\n")

    if not STORAGE_MODE:
        print(
            "Unable to locate wallpaper manifests in either legacy or user locations."
        )
        exit()

    manifest = load_manifest(ACTIVE_ENTRIES_PATH, ACTIVE_STRINGS_PATH)
    if not manifest:
        print("Unable to load wallpaper manifest for the active storage location.")
        exit()

    strings = extract_strings(manifest)
    categories = extract_categories(manifest)
    if not categories:
        print("No categories found in the available manifests.")
        exit()

    category_maps = build_category_maps(categories)
    top_level_category_ids: Set[str] = {
        cat.get("id") for cat in categories if cat.get("id")
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
        print("No assets available to process.")
        exit()

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

    applicable_assets = [
        asset for asset in assets if not category_id or category_id in asset.categories
    ]
    if not applicable_assets:
        print("\nNo assets available for the selected category.")
        exit()

    selected_category_name = (
        category_menu[category_index - 1][1]
        if category_index <= len(category_menu)
        else "All"
    )

    asset_status = gather_asset_status(applicable_assets)
    grouped_rows, display_rows = build_asset_groups(
        applicable_assets,
        category_menu,
        category_id,
        selected_category_name,
        asset_status,
    )
    render_asset_groups(grouped_rows)
    if display_rows:
        print()
    index_to_asset = {row.index: row.asset for row in display_rows}
    max_index = len(index_to_asset)
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

    action = input("\n(d)Download or (x)delete? (d/x) ").strip().lower()
    if action not in {ACTION_DOWNLOAD, ACTION_DELETE}:
        print("\nNo action selected.")
        exit()
    action_text = "download" if action == ACTION_DOWNLOAD else "delete"

    items: List[Tuple[str, str, str]] = []
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
            items.append((label, url, target_path))
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
        print(f"Nothing to {action_text}.")
        exit()

    free_space = shutil.disk_usage("/").free
    print(f"Available space: {format_bytes(free_space)}")
    print(f"Files to {action_text} ({len(tasks)}): {format_bytes(total_bytes)}")
    if action == ACTION_DOWNLOAD and total_bytes > free_space:
        print("Not enough disk space to download all files.")
        exit()

    proceed = input(f"{action_text.capitalize()} files? (y/n) ").strip().lower()
    if proceed != "y":
        exit()

    if action == ACTION_DOWNLOAD:
        start_time = time.time()
        print("\nDownloading...")
        results = ThreadPool().imap_unordered(download_file, tasks)
        for result in results:
            print(f"  Downloaded '{result}'")
        print(f"\nDownloaded {len(tasks)} files in {time.time() - start_time:.1f}s.")
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

    if STORAGE_MODE == STORAGE_MODE_LEGACY:
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
    context = ssl._create_unverified_context()
    conn = (
        http.client.HTTPSConnection(parsed_url.netloc, context=context)
        if parsed_url.scheme == "https"
        else http.client.HTTPConnection(parsed_url.netloc)
    )
    return conn


def get_content_length(url: str) -> int:
    parsed_url = urllib.parse.urlparse(url)
    conn = connect(parsed_url)
    path = parsed_url.path or "/"
    if parsed_url.query:
        path = f"{path}?{parsed_url.query}"
    conn.request("HEAD", path)
    r = conn.getresponse()
    content_length = int(r.getheader("Content-Length", -1))
    conn.close()
    return content_length


def download_file(download: Tuple[str, str, str]) -> str:
    label, url, file_path = download
    parsed_url = urllib.parse.urlparse(url)

    import time

    def download_with_retries(max_retries=5):
        for attempt in range(1, max_retries + 1):
            conn = connect(parsed_url)
            try:
                path = parsed_url.path or "/"
                if parsed_url.query:
                    path = f"{path}?{parsed_url.query}"

                conn.request("GET", path)
                r = conn.getresponse()

                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}: {r.reason}")

                content_length = r.getheader("Content-Length")
                expected_size = int(content_length) if content_length else None

                os.makedirs(os.path.dirname(file_path), exist_ok=True)

                bytes_written = 0
                CHUNK = 64 * 1024

                with open(file_path, "wb") as f:
                    while True:
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        bytes_written += len(chunk)

                conn.close()

                if expected_size is not None and bytes_written != expected_size:
                    raise RuntimeError(
                        f"Incomplete download: expected {expected_size}, got {bytes_written}"
                    )

                return label

            except Exception as e:
                if os.path.exists(file_path):
                    try: os.remove(file_path)
                    except OSError: pass

                if attempt == max_retries:
                    raise RuntimeError(
                        f"Download failed after {max_retries} attempts: {e}"
                    )

                time.sleep(1 + attempt * 0.5)

    return download_with_retries()



if __name__ == "__main__":
    main()
