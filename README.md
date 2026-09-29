# Audacity Quick Downloader

_Powered with Python_

Paste a link — YouTube, SoundCloud, Bandcamp, Apple Music and 1,000+ other
sites — and it lands in Audacity as a new track. No sketchy "YouTube to MP3"
websites, no manual file juggling: the MP3 is saved to your `Downloads` folder
and imported for you.

```
 ╭─ Paste a link or type a song name ──────────────────────────────────────╮
 │ https://soundcloud.com/…                                                │
 ╰─────────────────────────────────────────────────────────────────────────╯
  ✓ Import into Audacity   Playlists Ask ▾   Quality 192 kbps ▾   → Downloads
 ╭─────────────────────────────────────────────────────────────────────────╮
 │ SoundCloud  Flickermood                                     Downloading │
 │ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   64% │
 │ 1.2 MB/s · 0:03 left                                                    │
 ╰─────────────────────────────────────────────────────────────────────────╯
 ╭─────────────────────────────────────────────────────────────────────────╮
 │ YouTube  Never Gonna Give You Up                          ✓ In Audacity │
 │ Saved 1 file to Downloads and imported into Audacity.  (0:08)           │
 ╰─────────────────────────────────────────────────────────────────────────╯
  ● Audacity running   yt-dlp 2026.8.19 ✓   gamdl 3.9.1 ✓       1 downloading
```

## Features

- **Almost any site.** YouTube, YouTube Music, SoundCloud, Bandcamp, Mixcloud,
  Audiomack, Vimeo, TikTok, X, Twitch, the Internet Archive and everything
  else [yt-dlp] supports, plus Apple Music via [gamdl].
- **Or just type a song name.** Anything that is not a link is searched for on
  YouTube and the top result is downloaded.
- **A proper terminal interface.** Every download gets a card with a live
  progress bar, speed and time left. Queue as many as you like; three run at
  once.
- **Quiet.** No walls of yt-dlp and gamdl output. The details go to an
  activity log you can open with `Ctrl+L` if you want them.
- **Starts Audacity for you.** If it is not already running, it gets launched
  while the download runs, and the import waits until it is ready.
- **Updates in the background.** yt-dlp and gamdl are both checked against PyPI
  at once while the app starts, and upgraded only if there is something newer.
  You can paste links while that happens.
- **Playlists and albums**, downloaded as tagged MP3s with cover art.
- **Nothing gets clobbered.** A file that would overwrite an existing download
  is saved as `Song (1).mp3` instead.
- **Downloads survive import failures.** If Audacity cannot be reached, the
  MP3s are still saved and the reason is explained.
- A `.bat` file for double-click running, which works from a shortcut anywhere.

Spotify, Deezer, TIDAL and Amazon Music encrypt their audio, so links from them
cannot be downloaded. Type the song name instead to grab it from YouTube.

## Requirements

| What | Why |
| --- | --- |
| Python 3.9+ | Runs the script (developed on 3.11). |
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

Run it and paste a link:

```bash
python main.py
```

On Windows you can also just double-click `main-runner.bat`, or make a shortcut
to it and put that wherever is convenient. On its first run it creates the
project's `.venv` and installs the dependencies into it, so step 2 above is
optional if you only ever launch it that way. After that it only runs pip again
when `requirements.txt` changes, so it opens straight away.

### In the app

Type or paste into the box at the top and press `Enter`. Pasting several links
at once queues them all. A link that points at a video *and* a playlist asks
which you want, unless **Playlists** is set to always or never take the whole
thing.

| Key | What it does |
| --- | --- |
| `Enter` | Download what is in the box. |
| `↓` / `↑` | Move between downloads (`Esc` goes back to the box). |
| `C` | Cancel the selected download. |
| `R` | Retry a failed or cancelled download. |
| `O` | Show the downloaded file in Explorer. |
| `Delete` | Remove a finished download from the list. |
| `Ctrl+L` | Show or hide the activity log. |
| `Ctrl+O` | Open the downloads folder. |
| `Ctrl+R` | Clear finished downloads from the list. |
| `Ctrl+P` | Command palette, including a choice of colour themes. |
| `Ctrl+Q` | Quit. |

The **Import into Audacity**, **Playlists** and **Quality** settings apply to
downloads added after you change them.

### From the command line

Links (or song names) given on the command line are queued as soon as the app
opens:

```bash
# A SoundCloud track
python main.py "https://soundcloud.com/artist/track"

# A whole YouTube playlist
python main.py --playlist "https://www.youtube.com/playlist?list=PLb911ot23pTQ"

# Just the linked video, even though the URL is part of a playlist
python main.py --no-playlist "https://www.youtube.com/watch?v=VIDEO&list=PLAYLIST"

# A Bandcamp album and an Apple Music album together
python main.py "https://artist.bandcamp.com/album/name" "https://music.apple.com/us/album/name/id1234567890"

# Search YouTube and take the top result
python main.py "daft punk one more time"

# Download only with plain line-by-line output, leaving Audacity alone
python main.py --plain --no-import "https://youtu.be/dQw4w9WgXcQ"
```

With `--plain`, or whenever the output is piped somewhere, you get a line or
two per download instead of the full screen interface, and the app exits once
everything has finished. Run `--plain` without links to be prompted for them.

### Options

| Option | What it does |
| --- | --- |
| `-o`, `--output DIR` | Where to save the MP3s (default: `Downloads` next to the script). |
| `-q`, `--quality KBPS` | MP3 bitrate for everything except Apple Music (default: `192`). |
| `--playlist` / `--no-playlist` | Answer the playlist question up front. |
| `--cookies FILE` | Apple Music cookies file (default: `cookies.txt` next to the script). |
| `--site-cookies FILE` | Cookies for yt-dlp, e.g. for age-restricted YouTube videos. `--youtube-cookies` still works too. |
| `--no-launch` | Never start Audacity automatically. |
| `--no-import` | Download only; do not touch Audacity. |
| `--no-thumbnail` | Skip embedding the thumbnail as cover art. |
| `--no-update` | Do not check for newer yt-dlp and gamdl releases. |
| `--plain` | Simple line-by-line output instead of the full screen interface. |

Set the `AUDACITY_PATH` environment variable if Audacity is installed somewhere
unusual and the app cannot find it, or `AQD_SKIP_UPDATE=1` to turn the update
check off for good.

### Updates

Sites change all the time, and an old yt-dlp or gamdl is the most common reason
downloads break. Each time the app starts it asks PyPI for the latest version
of both at once, behind the interface, which takes a second or two. Anything
out of date is upgraded with pip, and the status bar shows how that is going.
Downloads added in the meantime wait for it to finish, so a package is never
replaced while it is being used.

It also installs [Deno](https://deno.com), the JavaScript runtime yt-dlp needs
for full YouTube support, and `yt-dlp-ejs` if either is missing. Deno comes
from its official PyPI package and stays inside the project's `.venv`. Nothing
is installed system-wide.

Upgrades only happen inside a virtual environment (such as the `.venv` the
`.bat` file creates). Run with your system Python and it just tells you when
something newer is out.

## Apple Music setup

Apple Music downloads go through [gamdl], which needs your browser session:

1. Sign in to <https://music.apple.com> in your browser.
2. Export your cookies in **Netscape format** with a cookie-export extension.
3. Save the file as `cookies.txt` next to `main.py` (or point `--cookies` at it).

`cookies.txt` is a login credential — `.gitignore` keeps it out of the
repository, and it should stay that way.

## How it works

1. The link is identified: Apple Music goes to gamdl, plain text becomes a
   YouTube search, and everything else goes to yt-dlp.
2. Audacity is started if it is not already running, so it can warm up while the
   download runs.
3. yt-dlp or gamdl downloads into a scratch folder, so the app knows exactly
   which files belong to this download. Their output is captured and turned
   into the progress bar instead of being printed.
4. Audio is converted to MP3 and tagged, then moved into your output folder.
5. The app connects to Audacity's scripting pipes and imports each file as a new
   track, in playlist order. Downloads that finish together take turns, since
   Audacity only has one scripting connection.
6. The scratch folder is deleted. Your MP3s stay put.

## Project layout

| File | Role |
| --- | --- |
| `main.py` | Command line, and the plain line-by-line mode. |
| `tui.py` | The full screen interface, built with [Textual]. |
| `jobs.py` | The download queue and its worker threads. |
| `sources.py` | Works out which site a link is from and what downloads it. |
| `ytdlpdownloader.py` | yt-dlp wrapper: YouTube, SoundCloud and everything else. |
| `applemusicdownloader.py` | gamdl wrapper plus MP3 conversion. |
| `audacity.py` | Finds, starts and talks to Audacity. |
| `updater.py` | Background update check for yt-dlp and gamdl. |
| `utils.py` | Scratch folders, safe moves, progress reporting, shared errors. |
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

**A site returns `HTTP Error 403` or claims a video is unavailable**

Usually a stale yt-dlp. The app updates it when it starts if it can; to do it by
hand:

```bash
pip install --upgrade yt-dlp
```

For age-restricted videos or sign-in-only tracks, export that site's cookies
and pass `--site-cookies cookies-site.txt`.

**The activity log mentions "No supported JavaScript runtime"**

yt-dlp needs [Deno](https://deno.com) and `yt-dlp-ejs` to unlock every YouTube
format. Both are installed automatically: by the `.bat` file, and by the app
itself in the background if they are missing. If you run the app outside a
virtual environment, install them yourself:

```bash
pip install --upgrade -r requirements.txt
```

**The interface looks garbled**

Use Windows Terminal (the default on Windows 11) rather than the old console
host, or run with `--plain`.

**Apple Music downloads fail immediately**

If the error mentions **status code 403** while fetching account info, your
`cookies.txt` has expired — export it again as described above. Cookies are
tied to a browser session and do not last indefinitely.

If it mentions a **token** or an unexpected page, gamdl has fallen behind a
change on Apple's website. gamdl 2.x no longer works at all, so make sure you
are on 3.x or newer:

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
[yt-dlp]: https://github.com/yt-dlp/yt-dlp
[Textual]: https://textual.textualize.io/
