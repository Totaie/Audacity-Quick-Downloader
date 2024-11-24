# Audacity-Quick-Downloader

## _Powered with Python_

Audacity-Quick-Downloader is a quick and easy MP3 importer for Audacity, saving you the time of needing to download an MP3 off of any sketchy YouTube-to-MP3 website or Apple Music Website.

## Features

- A `.bat` file to quickly run the Python script, with a shortcut for easy access anywhere.
- Automatically imports your MP3 into Audacity after downloading.
- Supports YouTube playlist downloads for easy batch importing of multiple MP3s.
- **New:** Supports Apple Music downloads using cookies for authentication.

## Installation

### Prerequisites

Make sure you have the following installed:

- **Python 3.8+** (We used Python 3.11.8, but it should work with any version above 3.8).
- **Audacity 3.1+** (We used Audacity 3.4.2, but it should work with any version above 3.1).
- **yt-dlp** (Python library for downloading YouTube videos).
- **ffmpeg** (Required by yt-dlp to extract audio).
- **Apple Music Subscription** (Required to download from Apple Music).
- **Apple Music Cookies File** (Required to authenticate with Apple Music, see instructions below).

### Steps to Install

1. **Clone the Repository:**

   ``git clone https://github.com/Totaie/Audacity-YT-Downloader.git
   cd Audacity-YT-Downloader``
**Install Required Dependencies:**

You can install the required Python libraries using pip:

`pip install -r requirements.txt`
Ensure that yt-dlp is installed (it should be listed in requirements.txt):

`pip install yt-dlp
Install ffmpeg` if you haven't already:

**Windows:** Download from FFmpeg.org and add the binary to your system's PATH.
Set Up Audacity:

Ensure that the **mod-script-pipe** extension is enabled in Audacity. This is required for importing files directly via the Python script.
Instructions for enabling the script pipe can be found on the Audacity wiki.

**Set Up Apple Music:**

To download from Apple Music, you need to export your session cookies:

Sign in to your Apple Music account in your browser.
Use one of the following browser extensions to export your cookies in Netscape format:
Firefox: Export Cookies Extension
Chromium-based browsers: Export Cookies Extension
Save the exported cookies file as `cookies.txt` in the same directory as the script, or provide the path using a command-line argument or configuration file.
Note: Make sure the cookies file contains your active Apple Music session and that your subscription is valid.

## Run the Script:

You can either run the script directly from the Python terminal: `python main.py`
Or, you can use the provided .bat file (run.bat) to easily run the script by double-clicking it. The .bat file will call the Python script from anywhere on your system.

### Steps to Use the Script
Open Audacity
Make sure that Audacity is running and the mod-script-pipe extension is enabled. You can enable this by going to: `Edit > Preferences > Modules > Enable mod-script-pipe.`

Run the Script or .bat Shortcut
You can run the script directly using the Python terminal or use the provided .bat file (shortcut) for quick execution. If using the .bat file, simply double-click it.

Enter the URL

**For YouTube:** Enter the YouTube video or playlist URL when prompted.

**For Apple Music:** Enter the Apple Music track or playlist URL when prompted, and ensure the cookies.txt file is in place.

**Download and Import**
The script will automatically download the MP3(s) and import them into Audacity.

**Move and Clean Up**

After downloading and importing, the MP3 files will be moved to your Downloads folder. Any temporary folders used during the download process will be deleted.

## Troubleshooting
If you receive an error similar to this:

`FileNotFoundError: [Errno 2] No such file or directory: '\\\\.\\pipe\\ToSrvPipe'`
You can fix this by:

Restarting either the script or Audacity.
Ensure the mod-script-pipe extension is enabled in Audacity:
Go to `Edit > Preferences > Modules.`
Check the box next to Enable mod-script-pipe.

### Example Commands

**For a single YouTube video:**
`python main.py https://www.youtube.com/watch?v=dQw4w9WgXcQ`

**For a YouTube playlist:**
`python main.py https://www.youtube.com/watch?v=Uj1ykZWtPYI&list=PL9JM2aC37BG03vlqyhiYX54NG_thqqvbg`

**For an Apple Music track:**
`python main.py https://music.apple.com/us/album/song-title/id1234567890`

**For an Apple Music playlist:**
`python main.py https://music.apple.com/us/playlist/playlist-title/id1234567890`

## Contributing
If you want to contribute to this project, feel free to fork the repository, make changes, and submit a pull request. We welcome any improvements, bug fixes, or new features!

## License
This project is licensed under the MIT License - see the LICENSE file for details.

## Update
This update includes the instructions for setting up Apple Music cookies and the new Apple Music support. Let me know if you need any more changes!





