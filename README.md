# Audacity Quick Downloader

_Powered with Python_

Paste a YouTube or Apple Music link and it lands in Audacity as a new track. No
sketchy "YouTube to MP3" websites, no manual file juggling — the MP3 is saved to
your `Downloads` folder and imported for you.

## Features

- **Starts Audacity for you.** If it is not already running, it gets launched
  and the import waits until it is ready.
- **YouTube videos and playlists**, downloaded as tagged MP3s with the video
  thumbnail embedded as cover art.
- **Apple Music tracks, albums and playlists** via [gamdl], converted to MP3
  with their tags and artwork intact.
- **Command line or interactive.** Pass URLs as arguments, or run it with none
  and paste links one after another.
- **Nothing gets clobbered.** A file that would overwrite an existing download
  is saved as `Song (1).mp3` instead.
- **Downloads survive import failures.** If Audacity cannot be reached, the
  MP3s are still saved and the reason is explained.
- A `.bat` file for double-click running, which works from a shortcut anywhere.

## Requirements

| What | Why |
| --- | --- |
| Python 3.8+ | Runs the script (developed on 3.11). |
| [Audacity] 3.1+ | The destination (developed on 3.7). |
| Audacity's `mod-script-pipe` module | How the script talks to Audacity. |
| [ffmpeg] on your `PATH` | Converts downloads to MP3. |
| Apple Music subscription + `cookies.txt` | Only needed for Apple Music links. |

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/Totaie/Audacity-YT-Downloader.git
```

**2. Install the Python dependencies**

```bash
pip install -r requirements.txt
```

**3. Install ffmpeg**

On Windows, download a build from [ffmpeg.org][ffmpeg] and add the folder
containing `ffmpeg.exe` to your `PATH`. Check it with:

```bash
ffmpeg -version
```

**4. Enable Audacity's scripting module**

In Audacity, go to `Edit > Preferences > Modules`, set **mod-script-pipe** to
`Enabled`, and restart Audacity. Without this, Audacity has no way to accept the
import command.

If the module is switched off when the app starts Audacity for you, it offers to
turn it on for you.

## Usage

Run it and paste a link when prompted:

```bash
python main.py
```

Or pass links straight in — several at a time is fine:

```bash
python main.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

On Windows you can also just double-click `main-runner.bat`, or make a shortcut
to it and put that wherever is convenient. It uses the project's `.venv` if
there is one, and falls back to whatever `python` is on your `PATH`.

### Examples

```bash
# A single YouTube video
python main.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# A whole YouTube playlist
python main.py --playlist "https://www.youtube.com/playlist?list=PLb911ot23pTQ"

# Just the linked video, even though the URL is part of a playlist
python main.py --no-playlist "https://www.youtube.com/watch?v=VIDEO&list=PLAYLIST"

# An Apple Music album
python main.py "https://music.apple.com/us/album/album-name/id1234567890"

# Download only, leaving Audacity alone
python main.py --no-import "https://youtu.be/dQw4w9WgXcQ"
```

When a YouTube link is part of a playlist and you have not said which you want,
you are asked whether to grab the whole thing.

### Options

| Option | What it does |
| --- | --- |
| `-o`, `--output DIR` | Where to save the MP3s (default: `Downloads` next to the script). |
| `-q`, `--quality KBPS` | MP3 bitrate for YouTube downloads (default: `192`). |
| `--playlist` / `--no-playlist` | Answer the playlist question up front. |
| `--cookies FILE` | Apple Music cookies file (default: `cookies.txt` next to the script). |
| `--youtube-cookies FILE` | Cookies for age-restricted YouTube videos. |
| `--no-launch` | Never start Audacity automatically. |
| `--no-import` | Download only; do not touch Audacity. |
| `--no-thumbnail` | Skip embedding the video thumbnail as cover art. |

Set the `AUDACITY_PATH` environment variable if Audacity is installed somewhere
unusual and the app cannot find it.

## Apple Music setup

Apple Music downloads go through [gamdl], which needs your browser session:

1. Sign in to <https://music.apple.com> in your browser.
2. Export your cookies in **Netscape format** with a cookie-export extension.
3. Save the file as `cookies.txt` next to `main.py` (or point `--cookies` at it).

`cookies.txt` is a login credential — `.gitignore` keeps it out of the
repository, and it should stay that way.

## How it works

1. Audacity is started if it is not already running, so it can warm up while the
   download runs.
2. yt-dlp (YouTube) or gamdl (Apple Music) downloads into a scratch folder, so
   the app knows exactly which files belong to this run.
3. Audio is converted to MP3 and tagged, then moved into your output folder.
4. The app connects to Audacity's scripting pipes and imports each file as a new
   track, in playlist order.
5. The scratch folder is deleted. Your MP3s stay put.

## Project layout

| File | Role |
| --- | --- |
| `main.py` | Command line, URL routing and the interactive loop. |
| `audacity.py` | Finds, starts and talks to Audacity. |
| `youtubedownloader.py` | yt-dlp wrapper. |
| `applemusicdownloader.py` | gamdl wrapper plus MP3 conversion. |
| `utils.py` | Scratch folders, safe moves, shared errors. |
| `main-runner.bat` | Double-click launcher for Windows. |

## Troubleshooting

**"Audacity does not appear to be running"** or
`FileNotFoundError: \\.\pipe\ToSrvPipe`

The scripting pipes are not there. Check that `mod-script-pipe` is set to
`Enabled` under `Edit > Preferences > Modules` and restart Audacity. Only one
script may be connected to Audacity at a time, so close any other tool using it.

**"Audacity did not reply within 60 seconds"**

Audacity is up but stuck on a dialog. The usual culprit is the **Automatic Crash
Recovery** window, which blocks importing until you answer it. Deal with the
dialog and run the command again — your MP3s are already saved.

**YouTube returns `HTTP Error 403` or claims a video is unavailable**

Usually a stale yt-dlp. Update it first:

```bash
pip install --upgrade yt-dlp
```

For age-restricted videos, export your YouTube cookies and pass
`--youtube-cookies cookies-youtube.txt`.

**Apple Music downloads fail immediately**

Either `cookies.txt` has expired (export it again) or gamdl has fallen behind a
change on Apple's website:

```bash
pip install --upgrade gamdl
```

**`ffmpeg was not found on your PATH`**

Install ffmpeg and reopen your terminal so the new `PATH` takes effect.

## A note on the pipes

If you are poking at Audacity scripting yourself, two things bite hard and are
worth knowing:

- Do not write the commands through a text-mode file on Windows. Python turns
  `\n` into `\r\n`, which corrupts the `\r\n\0` terminator Audacity expects, and
  it then silently ignores the command. Write bytes instead.
- Do not poll `os.path.exists()` on the pipe to see whether Audacity is ready.
  On Windows, that opens a client handle on the pipe, and Audacity hands out one
  pipe instance at a time — probe it in a loop and it drops the pipes entirely.
  Just try to open them for real and retry on failure.

## Contributing

Fork it, change it, open a pull request. Improvements, bug fixes and new
features are all welcome.

## License

MIT — see [LICENSE](LICENSE).

[Audacity]: https://www.audacityteam.org/
[ffmpeg]: https://ffmpeg.org/
[gamdl]: https://github.com/glomatico/gamdl
