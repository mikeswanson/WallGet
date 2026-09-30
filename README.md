# WallGet Live Wallpaper Download/Delete Script for macOS

By [Mike Swanson](http://blog.mikeswanson.com/)

WallGet automates downloading and deleting the live wallpaper videos that ship with macOS Sonoma and later. Instead of downloading each wallpaper manually, this script enumerates the full catalog, shows you what is already present, and lets you download missing assets or delete the ones you no longer want.

## Highlights

- Detects whether wallpapers exist in the current user's folder (`~/Library/Application Support/com.apple.wallpaper/aerials`) or the legacy system folder (`/Library/Application Support/com.apple.idleassetsd`).
- Presents each category with a total item count and supports selecting a single category or the entire catalog.
- Lists every asset in the chosen category, including its download status and file size, and accepts individual numbers, ranges, or an `All` option when selecting items to process.
- Downloads only the files that are missing or incomplete, or deletes the selected files from disk.
- Downloads to a temporary `.part` file and renames atomically, so a crash or `Ctrl-C` never leaves a partial video where macOS looks for real assets. Interrupted transfers resume automatically on the next run.
- Uses verified HTTPS (Apple's CDN certificate chain) with network timeouts, retries, and a bounded number of concurrent connections.
- Optionally restarts `idleassetsd` after legacy-mode changes so Wallpaper settings immediately reflect the new state.

> **Storage note:** the full catalog is roughly 60 GB of high-bitrate 240 fps video. macOS may also re-download aerials on its own while a Shuffle/Aerial wallpaper or screen saver is active, so deleting files does not guarantee they stay deleted until you switch those settings to non-aerial choices.

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

## Command-Line Automation

Running the script without arguments opens the interactive menu. For scheduled or scripted use, everything can be driven with flags instead:

```bash
# Download every missing aerial, no prompts
python3 wallget.py --category all --assets all --download --yes

# Delete one category by name
python3 wallget.py --category Earth --delete --yes

# Interactive category menu, then download assets 1-4 and 8
python3 wallget.py --assets 1-4,8 --download
```

- `--category CATEGORY` accepts a category id or display name (case-insensitive), or `all`. Omit it to pick interactively (or to mean all, when combined with `--assets`).
- `--assets SELECTION` accepts the same numbers/ranges as the interactive prompt, or `all`.
- `--download` / `--delete` pick the action; one of them is required whenever you pass `--category` or `--assets`.
- `--yes` (`-y`) skips the confirmation prompt (and the legacy `idleassetsd` restart prompt).
- A failed or interrupted download leaves its `.part` file in place; re-running the same command resumes it instead of starting over.

## Using WallGet

1. **Pick a category.** WallGet lists every wallpaper category along with the number of assets it contains, plus an "All" option. Enter the category number you want.
2. **Review assets.** The script groups assets by category, showing their current status (`downloaded` when the local file size matches Apple's manifest) and its file size.
3. **Select items.** Provide the asset numbers to process. You can enter comma-separated values (`1,4,7`), ranges (`2-5`), or choose the `All` option displayed at the bottom to target every listed asset.
4. **Choose an action.** Pick `d` to download missing files or `x` to delete the selected files.
5. **Confirm.** WallGet sums the total transfer or deletion size, checks available disk space when downloading, and asks for confirmation before proceeding.

Downloads stream directly from Apple's CDN using HTTPS, and deletes are limited to the targets you selected. Existing files with the correct size are skipped automatically during download operations.

I hope that this is useful!
