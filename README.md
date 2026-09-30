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
- **Or use files you already have.** Give it a file or a whole folder and it
  imports and/or separates them, no download needed.
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
- **Stem separation with Ultimate Vocal Remover 5.** If you have [UVR5]
  installed, songs can be split into vocals and instrumental, lead and backing
  vocals, or drums, bass and the rest, with the stems saved in their own folder
  and imported into Audacity too. It reuses the models you already downloaded
  in UVR5, and runs on your NVIDIA GPU if you have one. Off by default.
- **A settings screen** (`Ctrl+S`) for your defaults: where things are saved,
  the audio format and bitrate, Audacity behaviour, separation presets and
  more. They are saved per user, so an update never resets them.
- **MP3, M4A, Opus, Ogg, FLAC or WAV**, whichever you prefer.
- **Playlists and albums**, downloaded as tagged files with cover art.
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
| Python 3.10+ | Runs the script (developed on 3.11). |
| [ffmpeg] on your `PATH` | Converts downloads to your chosen format. |
| [Audacity] 3.1+ *(optional)* | Where downloads get imported (developed on 3.7). |
| Audacity's `mod-script-pipe` module | How the script talks to Audacity. |
| [Ultimate Vocal Remover 5][UVR5] *(optional)* | Only needed for stem separation. |
| An NVIDIA GPU *(optional)* | Makes separation much faster; the CPU works too. |
| Apple Music subscription + `cookies.txt` | Only needed for Apple Music links. |

Without Audacity the app is simply a downloader: turn off **Import into
Audacity** in Settings and it will not try. Without UVR5 the separation options
stay hidden.

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

**4. Set up Audacity (optional)** — see [Setting up Audacity](#setting-up-audacity).

**5. Set up stem separation (optional)** — see
[Stem separation with UVR5](#stem-separation-with-uvr5).

The first time the app opens it shows the Settings screen, so you can choose
where downloads (and separated stems) are saved before you start.

## Setting up Audacity

1. Download Audacity from <https://www.audacityteam.org/download/> and install
   it with the default options. The app finds it in `Program Files`
   automatically; if you put it somewhere else, set the `AUDACITY_PATH`
   environment variable to the full path of `Audacity.exe`.
2. Open Audacity, go to `Edit > Preferences > Modules`, set **mod-script-pipe**
   to `Enabled`, and restart Audacity. Without this, Audacity has no way to
   accept the import command.

   Or let the app do it: Settings shows a **Turn it on** button when the module
   is off (close Audacity first), and it also offers when it starts Audacity for
   you.
3. That's it. The status bar at the bottom of the app shows whether Audacity is
   running, and each download's card says when its tracks are in Audacity.

## Stem separation with UVR5

Separation splits a song into stems — vocals, instrumental, drums and so on —
using the models from [Ultimate Vocal Remover 5][UVR5]. Stems are saved to a
folder of their own (one sub-folder per song), and the original and/or the
stems can be imported into Audacity.

UVR5 has no command line, so the app runs its models through the
[audio-separator] package instead of through UVR5's window. It only offers
separation when UVR5 is installed, and it reuses the models you downloaded in
UVR5, linking them rather than copying so they take no extra space.

### 1. Install UVR5

- **Windows:** download `UVR_v5.6.0_setup.exe` (or newer) from the
  [UVR5 releases page](https://github.com/Anjok07/ultimatevocalremovergui/releases)
  and install it with the default options.
- **macOS:** download the `.dmg` for your Mac (Apple Silicon or Intel) from the
  same page and drag it to Applications.
- **Linux:** follow the install steps in the UVR5 README, then set the
  **UVR5 folder** in Settings to where you cloned it.

### 2. Models

The presets use the best models audio-separator can run, chosen from the
public benchmarks on [MVSEP](https://mvsep.com/quality_checker/multisong_leaderboard).
To get them all in one go (about 1.2 GB), click **Download all** next to
**Models** in Settings once the engine is set up. Otherwise each is downloaded
the first time a preset needs it. If UVR5 already has a model, its copy is used
instead.

| Preset | Stems | Best / Balanced | Fast |
| --- | --- | --- | --- |
| Vocals + Instrumental | Vocals, Instrumental | BS-Roformer Resurrection | MDX-Net Inst HQ 3 |
| Instrumental + Lead + Backing vocals | Instrumental, Lead Vocals, Backing Vocals | BS-Roformer Resurrection, then BS-Roformer Karaoke | Inst HQ 3, then KARA 2 |
| Vocals, Drums, Bass, Other | 4 stems | BS-Roformer SW (guitar and piano folded into Other) | Demucs v4 htdemucs |
| Six stems | Vocals, Drums, Bass, Guitar, Piano, Other | BS-Roformer SW | Demucs v4 htdemucs_6s |
| All stems | Lead and Backing Vocals, Drums, Bass, Guitar, Piano, Other | BS-Roformer SW, then BS-Roformer Karaoke | htdemucs, then KARA 2 (no guitar/piano) |

Why these models (MVSEP scores, SDR in dB, higher is better):

| Model | Scores | Replaces |
| --- | --- | --- |
| BS-Roformer "Resurrection" by unwa | vocals 11.36 | MDX-Net Inst HQ 3, htdemucs vocals (about 9–10) |
| BS-Roformer SW (ships with UVR 5.6) | drums 14.11, bass 14.62, vocals 11.30; #1 for guitar, ahead of Logic Pro's stem splitter | Demucs v4 (drums and bass 2–3 dB lower) |
| BS-Roformer Karaoke by anvuew | lead vocals 10.23, the best public single model | UVR_MDXNET_KARA_2 (about 5.4) |

### Quality

**Quality** in Settings trades speed for cleanliness:

- **Best**: the models above, predicting every moment of the song 8 times
  over (overlap 8) and averaging. On an RTX 5070 Ti a 5-minute song takes about
  a minute for two stems.
- **Balanced**: the same models at overlap 4, about twice as fast, and very
  close in quality.
- **Fast**: the classic UVR5 models. Several times quicker than the Roformer
  models, which is what makes separation practical without an NVIDIA GPU.
- **Automatic** (the default) picks Best when the engine has a GPU and Fast when
  it does not.

On a GPU the engine also runs in half precision (fp16), which is about 1.7×
faster; measured against full precision, the difference was 78 dB below the
music, which is inaudible.

### 3. Set up the separation engine

Open Settings (`Ctrl+S`) and, under **Stem separation**, click **Set up**. This
installs PyTorch and audio-separator into `.venv-separator` in the app's
folder, away from everything else:

- With an **NVIDIA GPU**, it installs PyTorch with CUDA (about 3 GB). A song
  separates in about a minute at Best quality.
- Without one, it installs the CPU version (about 200 MB). It works, just more
  slowly; the Fast quality level is the one to use.

You can keep downloading while it installs; the status bar shows how it is
going. **Remove** in Settings deletes it again (your UVR5 models are never
touched).

### 4. Turn it on

Tick **Separate** in Settings to separate every download by default, choose a
**Preset** and **Quality**, and pick where the stems should go with **Save stems
to**. Stems are saved the way your downloads are: the same audio format and
bitrate, with the song's tags (titled like `Song (Vocals)`) and cover art. The
**Separate stems** tick box and preset list above the download list change it
just for this session. Separations run one at a time; downloads carry on in
parallel.

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

Type or paste into the box at the top and press `Enter`. It takes:

- **A link**, or several pasted at once.
- **A song name**, to download the top YouTube result.
- **A file or folder on this PC.** Paste its path, or drag it from Explorer
  into the window. Nothing is downloaded: the songs are imported and/or
  separated where they are, following your settings (a folder includes its
  sub-folders). Dropping a file or folder onto `main-runner.bat` works too.

A link that points at a video *and* a playlist asks which you want, unless
**Playlists** is set to always or never take the whole thing.

Under the box are three chips. Click one (or `Tab` to it and press `Enter`)
to change it for this session; your saved defaults live in Settings:

- **Audacity**: import into Audacity, on or off.
- **Stems**: off, or which preset to separate with. Only shown when UVR5 is
  installed.
- **Playlists**: ask, whole playlist, or just the track.

When a song is both imported and separated, the song goes into Audacity as
soon as it is downloaded so you can start working on it, and its stems follow
as soon as they are ready.

| Key | What it does |
| --- | --- |
| `Enter` | Download what is in the box. |
| `↓` / `↑` | Move between downloads (`Esc` goes back to the box). |
| `C` | Cancel the selected download. |
| `R` | Retry a failed or cancelled download. |
| `O` | Show the downloaded file (or its stems) in Explorer. |
| `Delete` | Remove a finished download from the list. |
| `Ctrl+S` | Settings. |
| `Ctrl+L` | Show or hide the activity log. |
| `Ctrl+O` | Open the downloads folder. |
| `Ctrl+R` | Clear finished downloads from the list. |
| `Ctrl+Q` | Quit. |

The line at the bottom always shows the keys that apply right now.

### Settings

`Ctrl+S` opens the Settings screen. Everything in it is saved to
`%APPDATA%\AudacityQuickDownloader\settings.json` (`~/.config/...` on Linux,
`~/Library/Application Support/...` on macOS), so every user of a PC has their
own, and updating the app never resets them.

| Section | Settings |
| --- | --- |
| Downloads | Download folder, audio format (MP3, M4A, Opus, Ogg, FLAC, WAV), bitrate, playlist behaviour, cover art. |
| Audacity | Import automatically, start Audacity automatically, turn on its scripting module. |
| Stem separation | Set up or remove the engine, download every model in one go, separate automatically, preset, quality, stems folder, import the original and/or the stems, GPU or CPU, UVR5's folder. Stems use the Downloads format and bitrate. |
| Cookies | Apple Music cookies file, cookies for other sites. |
| Appearance | Colour theme. |

The **Browse…** buttons open the normal Windows folder picker.

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

These override your saved settings for one run; they never change them.

| Option | What it does |
| --- | --- |
| `-o`, `--output DIR` | Where to save downloads. |
| `-f`, `--format FORMAT` | `mp3`, `m4a`, `opus`, `ogg`, `flac` or `wav`. |
| `-q`, `--quality KBPS` | Bitrate for lossy formats: `128`, `192`, `256` or `320`. |
| `--playlist` / `--no-playlist` | Answer the playlist question up front. |
| `--separate` / `--no-separate` | Split into stems with UVR5's models, or don't. |
| `--preset NAME` | `vocals_instrumental`, `lead_backing`, `four_stems`, `all_stems` or `six_stems`. |
| `--stems-dir DIR` | Where to save separated stems. |
| `--cookies FILE` | Apple Music cookies file (default: `cookies.txt` next to the script). |
| `--site-cookies FILE` | Cookies for yt-dlp, e.g. for age-restricted YouTube videos. `--youtube-cookies` still works too. |
| `--no-launch` | Never start Audacity automatically. |
| `--no-import` | Download only; do not touch Audacity. |
| `--no-thumbnail` | Skip embedding the thumbnail as cover art. |
| `--no-update` | Do not check for newer yt-dlp and gamdl releases. |
| `--plain` | Simple line-by-line output instead of the full screen interface. |

Set the `AUDACITY_PATH` environment variable if Audacity is installed somewhere
unusual and the app cannot find it, `UVR_PATH` likewise for UVR5, or
`AQD_SKIP_UPDATE=1` to turn the update check off for good.

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
4. Audio is converted to your chosen format and tagged, then moved into your
   downloads folder. The scratch folder is deleted.
5. If separation is on, the song is split into stems by a separate process
   running in `.venv-separator`, one song at a time, into
   `<stems folder>\<song name>\<song name> (Vocals).mp3` and so on, in your
   chosen format.
6. The app connects to Audacity's scripting pipes and imports the original
   and/or the stems as new tracks, in playlist order. Downloads that finish
   together take turns, since Audacity only has one scripting connection.

## Project layout

| File | Role |
| --- | --- |
| `main.py` | Command line, and the plain line-by-line mode. |
| `tui.py` | The full screen interface, built with [Textual]. |
| `settings_screen.py` | The Settings screen. |
| `settings.py` | Loading and saving the user's settings. |
| `jobs.py` | The download queue, separation and importing. |
| `sources.py` | Works out which site a link is from and what downloads it. |
| `ytdlpdownloader.py` | yt-dlp wrapper: YouTube, SoundCloud and everything else. |
| `applemusicdownloader.py` | gamdl wrapper plus format conversion. |
| `separation.py` | Finds UVR5, installs the separation engine, runs separations. |
| `separator_worker.py` | Runs one separation inside `.venv-separator`. |
| `audacity.py` | Finds, starts and talks to Audacity. |
| `updater.py` | Background update check for yt-dlp and gamdl. |
| `utils.py` | Scratch folders, safe moves, progress reporting, shared errors. |
| `main-runner.bat` | Double-click launcher for Windows. |

## Troubleshooting

**The separation options are missing**

They only appear when UVR5 is installed. If it is installed somewhere unusual,
set **UVR5 folder** in Settings (or the `UVR_PATH` environment variable) to the
folder that holds `UVR.exe` and its `models` folder.

**"Not separated: set up the separation engine"**

The download worked, but the engine is not installed yet. Open Settings
(`Ctrl+S`) and click **Set up** under Stem separation.

**Separation is very slow**

It is running on the CPU. With an NVIDIA GPU, update your graphics driver,
set **Run on** to `GPU (NVIDIA)` in Settings and click **Reinstall**, so the
CUDA version of PyTorch is installed. Settings shows `GPU` or `CPU` next to the
engine once it is set up.

**Separation fails with "out of memory"**

Another program is using the GPU's memory, or the card is small. Close it, or
set **Run on** to `CPU only`.

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
[UVR5]: https://github.com/Anjok07/ultimatevocalremovergui
[audio-separator]: https://github.com/karaokenerds/python-audio-separator
