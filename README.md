
# Audacity-YT-Downloader

## _Powered with Python_

Audacity-YT-Downloader is a quick and easy MP3 importer for Audacity, saving you the time of needing to download an MP3 off of any sketchy YouTube-to-MP3 website.

## Features

-   A `.bat` file to quickly run the Python script, with a shortcut for easy access anywhere.
-   Automatically imports your MP3 into Audacity after downloading.
-   Supports YouTube playlist downloads for easy batch importing of multiple MP3s.

## Installation

### Prerequisites

Make sure you have the following installed:

-   **Python 3.5+** (We used Python 3.11.8, but it should work with any version above 3.5).
-   **Audacity 3.1+** (We used Audacity 3.4.2, but it should work with any version above 3.1).
-   **yt-dlp** (Python library for downloading YouTube videos).
-   **ffmpeg** (Required by yt-dlp to extract audio).

### Steps to Install

1.  **Clone the Repository:**
    
    `git clone https://github.com/Totaie/Audacity-YT-Downloader.git
    cd Audacity-YT-Downloader` 
    
2.  **Install Required Dependencies:**
    
    -   You can install the required Python libraries using `pip`:
    
    `pip install -r requirements.txt` 
    
    -   Ensure that `yt-dlp` is installed (it should be listed in `requirements.txt`):
    
    `pip install yt-dlp` 
    
    -   Install `ffmpeg` if you haven't already:
        -   **Windows:** Download from [FFmpeg.org](https://ffmpeg.org/download.html) and add the binary to 
3.  **Set Up Audacity:**
    
    -   Ensure that the mod-script-pipe extension is enabled in Audacity. This is required for importing files directly via the Python script.
    -   Instructions for enabling the script pipe can be found on the Audacity wiki.
4.  **Run the Script:**
    
    -   You can either run the script directly from the Python terminal:
    
    `python main.py` 
    
    -   Or, you can use the provided `.bat` file (`run.bat`) to easily run the script by double-clicking it. The `.bat` file will call the Python script from anywhere on your system.
5.  **Optional - Adding to Audacity:**
    
    -   Once the script is run, it will automatically download the MP3(s) and import them into Audacity. If you wish to customize this process, you can modify the script as needed.


### Steps to Use the Script

1.  **Open Audacity**  
    Make sure that Audacity is running and the mod-script-pipe extension is enabled. You can enable this by going to: `Edit > Preferences > Modules > Enable mod-script-pipe`.
    
2.  **Run the Script or `.bat` Shortcut**  
    You can run the script directly using the Python terminal or use the provided `.bat` file (shortcut) for quick execution. If using the `.bat` file, simply double-click it.
    
3.  **Enter the YouTube URL**  
    When prompted, enter the URL of the YouTube video or playlist you want to download.
    
4.  **Download and Import**  
    The script will automatically download the MP3(s) and import them into Audacity.
    
5.  **Move and Clean Up**  
    After downloading and importing, the MP3 files will be moved to your `Downloads` folder. Any temporary folders used during the download process will be deleted.
    

### Troubleshooting

If you receive an error similar to this:

`FileNotFoundError: [Errno 2] No such file or directory: '\\\\.\\pipe\\ToSrvPipe'` 

You can fix this by:

-   Restarting either the script or Audacity.
-   Ensure the mod-script-pipe extension is enabled in Audacity:
    1.  Go to `Edit > Preferences > Modules`.
    2.  Check the box next to **Enable mod-script-pipe**.

### Example Commands

-   For a single video:

    `python main.py https://www.youtube.com/watch?v=dQw4w9WgXcQ` 
    
-   For a playlist:
  
    `python main.py https://www.youtube.com/watchv=Uj1ykZWtPYI&list=PL9JM2aC37BG03vlqyhiYX54NG_thqqvbg` 
    

## Contributing

If you want to contribute to this project, feel free to fork the repository, make changes, and submit a pull request. We welcome any improvements, bug fixes, or new features!

## License

This project is licensed under the MIT License - see the LICENSE file for details.
