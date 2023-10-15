# WallGet Live Wallpaper Download Script for macOS

By [Mike Swanson](http://blog.mikeswanson.com/)

I love the live wallpaper videos in macOS Sonoma, but I don't like that I have to download each video individually. So, until Apple adds a "download all" button to their Wallpaper settings, you can use this script.

The script allows you to download just one wallpaper category (e.g. "Earth") at a time, or you can choose to download all categories at once (which currently results in 134 video files and ~65 GB of data). The downloaded files are placed where macOS expects them.

After downloads complete, the script can optionally kill the **idleassetsd** process (or you can just restart your Mac). Either of these operations causes **idleassetsd** to update the now-downloaded status of each file.

## Requirements

This script only makes sense on macOS Sonoma (and presumably future OS releases), and it requires admin permission to write to the correct wallpaper folder.

## Setup

If you just want to run the script, use the **Download raw file** button to save [wallget.py](https://github.com/mikeswanson/wallget/blob/main/wallget.py) to a folder.

Or, if you're a developer:

    git clone https://github.com/mikeswanson/wallget.git

## Usage

From **Terminal**, change to the folder that contains **wallget.py**, and execute the script with admin permission:

    sudo python3 wallget.py

If you're a non-programmer, you may see a pop-up window asking you to install the command-line developer tools. These are necessary to run the script, so select **Install** and wait for the installation to finish before trying the above command a second time.

After entering your admin password, you should be presented with a numbered list of live wallpaper categories, including a final "All" category. To see the videos in each category, you can preview them in the Settings app under Wallpaper (or right-click the desktop and choose **Change Wallpaper...**). Select a category to continue.

The script determines the required storage space for the selected files, reports the total, and prompts to continue. Note that the script only downloads files that don't exist or have mismatched file sizes (possibly because a prior download failed part-way through). Confirm the download to continue.

When all downloads are complete, you are prompted to optionally kill the **idleassetsd** process so that each wallpaper's download status is correctly reflected in the Settings app. If you choose not to do this, you can simply restart your Mac.

I hope that this is useful!
