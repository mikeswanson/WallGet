import http.client
import json
import os
import plistlib
import shutil
import ssl
import time
import urllib.parse
from multiprocessing.pool import ThreadPool
from typing import Tuple

IDLEASSETSD_PATH = "/Library/Application Support/com.apple.idleassetsd"
STRINGS_PATH = f"{IDLEASSETSD_PATH}/Customer/TVIdleScreenStrings.bundle/en.lproj/Localizable.nocache.strings"
ENTRIES_PATH = f"{IDLEASSETSD_PATH}/Customer/entries.json"
VIDEO_PATH = f"{IDLEASSETSD_PATH}/Customer/4KSDR240FPS"


def main():
    # Check if running as admin
    if os.geteuid() != 0:
        print(f'Please run as admin: sudo python3 "{__file__}"')
        exit()

    print("WallGet Live Wallpaper Download/Delete Script")
    print("---------------------------------------------\n")

    # Validate environment
    if not os.path.isdir(IDLEASSETSD_PATH):
        print("Unable to find idleassetsd path.")
        exit()
    if not os.path.isfile(STRINGS_PATH):
        print("Unable to find localizable strings file.")
        exit()
    if not os.path.isfile(ENTRIES_PATH):
        print("Unable to find entries.json file.")
        exit()
    if not os.path.isdir(VIDEO_PATH):
        print("Unable to find video path.")
        exit()

    # Read localizable strings
    with open(STRINGS_PATH, "rb") as fp:
        strings = plistlib.load(fp)

    # Read asset entries
    asset_entries = json.load(open(ENTRIES_PATH))

    # Show categories
    item = 0
    categories = asset_entries.get("categories", [])
    for category in categories:
        name = strings.get(category.get("localizedNameKey", ""), "")
        item += 1
        print(f"{item}. {name}")
    print(f"{item + 1}. All")

    # Select category
    category_index = as_int(input("\nCategory number? "))
    if category_index < 1 or category_index > item + 1:
        print("\nNo category selected.")
        exit()
    category_id = (
        categories[int(category_index) - 1]["id"] if category_index <= item else None
    )

    # Determine items (collect all possible wallpapers in the category first)
    possible_items = []
    for asset in asset_entries.get("assets", []):
        if category_id and category_id not in asset.get("categories", []):
            continue

        label = strings.get(asset.get("localizedNameKey", ""), "")
        id = asset.get("id", "")

        # NOTE: May need to update this key logic if other formats are added
        url = asset.get("url-4K-SDR-240FPS", "")

        # Valid asset?
        if not label or not id or not url:
            continue

        path = urllib.parse.urlparse(url).path
        ext = os.path.splitext(path)[1]
        file_path = f"{VIDEO_PATH}/{id}{ext}"
        possible_items.append((label, url, file_path))

    # Show wallpaper list and allow selection
    print("\nWallpapers:")
    for idx, (label, url, file_path) in enumerate(possible_items, 1):
        print(f"{idx}. {label}")
    print("(Press Enter to select all wallpapers in this category)")
    selection = input("Select wallpaper(s) by number (comma-separated), or Enter for all: ").strip()
    if selection:
        try:
            selected_indices = [int(x) - 1 for x in selection.split(",") if x.strip().isdigit()]
            possible_items = [possible_items[i] for i in selected_indices if 0 <= i < len(possible_items)]
        except Exception:
            print("Invalid selection.")
            exit()
    # Now possible_items contains only the selected wallpapers

    # Download or delete?
    action = input("\n(d)Download or (x)delete? (d/x) ").strip().lower()
    if action != "d" and action != "x":
        print("\nNo action selected.")
        exit()
    action_text = "download" if action == "d" else "delete"

    # Ask if user wants to force download (after action is defined)
    force_download = False
    if possible_items and action == "d":
        force = input("Force download even if file exists? (y/n) ").strip().lower()
        force_download = (force == "y")

    # Determine items to process (filter by download/delete logic)
    print(f"\nDetermining {action_text} size...", end="\n")
    items = []
    total_bytes = 0
    for label, url, file_path in possible_items:
        file_exists = os.path.isfile(file_path)
        file_size = os.path.getsize(file_path) if file_exists else 0
        remote_size = -1
        remote_error = None
        if action == "d":
            try:
                remote_size = get_content_length(url)
            except Exception as e:
                remote_error = str(e)
            print(f"  {label}:")
            print(f"    Path: {file_path}")
            print(f"    Exists: {file_exists}")
            print(f"    Local size: {file_size if file_exists else 'N/A'}")
            print(f"    Remote size: {remote_size if remote_error is None else 'ERROR: ' + remote_error}")
            if remote_error is not None:
                print(f"    Skipped: Could not determine remote file size.")
                continue
            if force_download:
                items.append((label, url, file_path))
                total_bytes += remote_size
                print(f"    Will be downloaded (force).")
            elif not file_exists or file_size != remote_size:
                items.append((label, url, file_path))
                total_bytes += remote_size
                print(f"    Will be downloaded.")
            else:
                print(f"    Skipped: Already exists and size matches.")
        elif action == "x":
            print(f"  {label}:")
            print(f"    Path: {file_path}")
            print(f"    Exists: {file_exists}")
            if file_exists:
                items.append((label, url, file_path))
                total_bytes += file_size
                print(f"    Will be deleted.")
            else:
                print(f"    Skipped: File does not exist.")
    print("done.\n")

    # Anything to process?
    if not items:
        print(f"Nothing to {action_text}.")
        exit()

    # Disk space check
    free_space = shutil.disk_usage("/").free
    print(f"Available space: {format_bytes(free_space)}")
    print(f"Files to {action_text} ({len(items)}): {format_bytes(total_bytes)}")
    if action == "d" and total_bytes > free_space:
        print("Not enough disk space to download all files.")
        exit()

    proceed = input(f"{action_text.capitalize()} files? (y/n) ").strip().lower()
    if proceed != "y":
        exit()

    if action == "d":
        start_time = time.time()
        print("\nDownloading...")
        results = ThreadPool().imap_unordered(download_file, items)
        for result in results:
            print(f"  Downloaded '{result}'")
        print(f"\nDownloaded {len(items)} files in {time.time() - start_time:.1f}s.")
    elif action == "x":
        print("\nDeleting...")
        for item in items:
            label, _, file_path = item
            os.remove(file_path)
            print(f"  Deleted '{label}'")
        print(f"\nDeleted {len(items)} files.")

    # Optionally kill idleassetsd to update wallpaper status
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
    conn.request("HEAD", parsed_url.path)
    r = conn.getresponse()
    content_length = int(r.getheader("Content-Length", -1))
    conn.close()
    return content_length


def download_file(download: Tuple[str, str, str]) -> str:
    label, url, file_path = download
    parsed_url = urllib.parse.urlparse(url)
    conn = connect(parsed_url)
    conn.request("GET", parsed_url.path)
    r = conn.getresponse()
    if r.status == 200:
        with open(file_path, "wb") as f:
            shutil.copyfileobj(r, f)
    conn.close()
    return label


if __name__ == "__main__":
    main()
