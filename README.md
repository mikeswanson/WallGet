# WallGet Live Wallpaper Download/Delete Script for macOS

By [Mike Swanson](http://blog.mikeswanson.com/)

WallGet automates downloading and deleting the live wallpaper videos that ship with macOS Sonoma and later. Instead of downloading each wallpaper manually, this script enumerates the full catalog, shows you what is already present, and lets you download missing assets or delete the ones you no longer want.

## Highlights

- Detects whether wallpapers exist in the current user's folder (`~/Library/Application Support/com.apple.wallpaper/aerials`) or the legacy system folder (`/Library/Application Support/com.apple.idleassetsd`).
- Presents each category with a total item count and supports selecting a single category or the entire catalog.
- Lists every asset in the chosen category, including its download status and file size, and accepts individual numbers, ranges, or an `All` option when selecting items to process.
- Downloads only the files that are missing or incomplete, or deletes the selected files from disk.
- Optionally restarts `idleassetsd` after legacy-mode changes so Wallpaper settings immediately reflect the new state.

## Requirements

- macOS Sonoma or later.
- Python 3 (the version that ships with macOS is fine).
- Network access.
- Administrator privileges **only** when you need to work with the legacy system folder.

## Getting Started

If you just want to run the script, use the **Download raw file** button to save [wallget.py](https://github.com/mikeswanson/wallget/blob/main/wallget.py) to a folder.

Or, if you're a developer:

```bash
git clone https://github.com/mikeswanson/wallget.git
cd wallget
```

## Running the Script

Open Terminal, change into the folder that contains `wallget.py`, and run:

```bash
python3 wallget.py
```

If you're a non-programmer, you may see a pop-up window asking you to install the command-line developer tools. These are necessary to run the script, so select **Install** and wait for the installation to finish before trying the above command a second time.

WallGet will detect the active storage location:

- **User mode**: the script runs as your account and places files inside your home folder. No `sudo` is required.
- **Legacy mode** (older versions): assets live under `/Library/Application Support/com.apple.idleassetsd`, so you must run the script with administrator privileges:

  ```bash
  sudo python3 wallget.py
  ```

  After actions complete in legacy mode, WallGet offers to kill the `idleassetsd` daemon so Wallpaper settings immediately display the updated download state. If you decline, a reboot will update the status as well.

## Using WallGet

1. **Pick a category.** WallGet lists every wallpaper category along with the number of assets it contains, plus an "All" option. Enter the category number you want.
2. **Review assets.** The script groups assets by category, showing their current status (`downloaded` when the local file size matches Apple's manifest) and its file size.
3. **Select items.** Provide the asset numbers to process. You can enter comma-separated values (`1,4,7`), ranges (`2-5`), or choose the `All` option displayed at the bottom to target every listed asset.
4. **Choose an action.** Pick `d` to download missing files or `x` to delete the selected files.
5. **Confirm.** WallGet sums the total transfer or deletion size, checks available disk space when downloading, and asks for confirmation before proceeding.

Downloads stream directly from Apple's CDN using HTTPS, and deletes are limited to the targets you selected. Existing files with the correct size are skipped automatically during download operations.

I hope that this is useful!
