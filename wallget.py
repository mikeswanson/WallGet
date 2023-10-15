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

    print("WallGet Live Wallpaper Download Script")
    print("--------------------------------------\n")

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
    category_index = as_int(input("\nCategory number to download? "))
    if category_index < 1 or category_index > item + 1:
        print("\nNo category selected.")
        exit()
    category_id = (
        categories[int(category_index) - 1]["id"] if category_index <= item else None
    )

    # Determine downloads
    print("\nDetermining download size...", end="")
    downloads = []
    bytes_required = 0
    for asset in asset_entries.get("assets", []):
        if category_id and category_id not in asset.get("categories", []):
            continue

        print(".", end="", flush=True)

        label = strings.get(asset.get("localizedNameKey", ""), "")
        id = asset.get("id", "")

        # NOTE: May need to update this key logic if other formats are added
        url = asset.get("url-4K-SDR-240FPS", "")

        # Valid asset?
        if not label or not id or not url:
            continue

        content_length = get_content_length(url)
        path = urllib.parse.urlparse(url).path
        ext = os.path.splitext(path)[1]
        file_path = f"{VIDEO_PATH}/{id}{ext}"

        # Download if file doesn't exist or is the wrong size
        if (
            not os.path.isfile(file_path)
            or os.path.getsize(file_path) != content_length
        ):
            downloads.append((label, url, file_path))
            bytes_required += content_length

    print("done.\n")

    # Anything to download?
    if not downloads:
        print("Nothing to download.")
        exit()

    # Disk space check
    free_space = shutil.disk_usage("/").free
    print(f"Available space: {format_bytes(free_space)}")
    print(f"Files to download ({len(downloads)}): {format_bytes(bytes_required)}")
    if bytes_required > free_space:
        print("Not enough disk space to download all files.")
        exit()

    proceed = input("Download files? (y/n) ").strip().lower()
    if proceed != "y":
        exit()

    start_time = time.time()
    print("\nDownloading...")
    results = ThreadPool().imap_unordered(download_file, downloads)
    for result in results:
        print(f"  Downloaded '{result}'")

    print(f"\nDownloaded {len(downloads)} files in {time.time() - start_time:.1f}s.")

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
