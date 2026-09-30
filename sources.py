"""Work out what a pasted link (or bit of text) is and who should download it.

Apple Music goes through gamdl. A handful of streaming services encrypt their
audio, so there is nothing to download and we say so up front. Plain text is
treated as a YouTube search, and every other link is handed to yt-dlp, which
understands well over a thousand sites.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APPLE_HOSTS = {
    "music.apple.com",
    "geo.music.apple.com",
    "itunes.apple.com",
    "beta.music.apple.com",
}

# Friendly names for sites people are likely to paste. Anything missing still
# works through yt-dlp; it is just labelled with its domain instead.
SITE_NAMES = {
    "music.youtube.com": "YouTube Music",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "youtube-nocookie.com": "YouTube",
    "soundcloud.com": "SoundCloud",
    "snd.sc": "SoundCloud",
    "bandcamp.com": "Bandcamp",
    "mixcloud.com": "Mixcloud",
    "audiomack.com": "Audiomack",
    "hearthis.at": "hearthis.at",
    "vimeo.com": "Vimeo",
    "dailymotion.com": "Dailymotion",
    "tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "x.com": "X",
    "twitter.com": "X",
    "twitch.tv": "Twitch",
    "reddit.com": "Reddit",
    "archive.org": "Internet Archive",
    "bilibili.com": "Bilibili",
    "nicovideo.jp": "Niconico",
    "newgrounds.com": "Newgrounds",
    "bitchute.com": "BitChute",
    "rumble.com": "Rumble",
    "odysee.com": "Odysee",
    "podcasts.apple.com": "Apple Podcasts",
}

# Services whose audio is DRM protected, so neither yt-dlp nor anything else
# we ship can download it.
DRM_SERVICES = {
    "spotify.com": "Spotify",
    "spotify.link": "Spotify",
    "deezer.com": "Deezer",
    "deezer.page.link": "Deezer",
    "tidal.com": "TIDAL",
    "music.amazon.com": "Amazon Music",
    "music.amazon.co.uk": "Amazon Music",
    "music.amazon.de": "Amazon Music",
    "pandora.com": "Pandora",
}

# What counts as audio when a local file or folder is given. Video files are
# included: Audacity imports their soundtrack.
LOCAL_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".alac", ".aif",
    ".aiff", ".wma", ".mp4", ".m4v", ".mkv", ".webm", ".mov",
}

# Brand colours for the badges in the interface.
SITE_COLORS = {
    "YouTube": "#ff0033",
    "YouTube Music": "#ff0033",
    "SoundCloud": "#ff5500",
    "Bandcamp": "#1da0c3",
    "Apple Music": "#fa2d48",
    "Apple Podcasts": "#9933cc",
    "Mixcloud": "#5000ff",
    "Vimeo": "#1ab7ea",
    "TikTok": "#25f4ee",
    "Twitch": "#9146ff",
    "X": "#555555",
    "Search": "#7c5cff",
    "Spotify": "#1db954",
}

# Something with a dot and no spaces, e.g. "soundcloud.com/artist/track".
# One pasted item: "double quoted", 'single quoted', or bare with
# backslash-escaped spaces.
PASTED_TOKEN = re.compile(r'"([^"]+)"|\'([^\']+)\'|((?:\\\s|\S)+)')

# The start of a second link stuck to the end of the first.
GLUED_URLS = re.compile(r"(?<=\S)(?=https?://)", re.I)

URL_LIKE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?$", re.I)


@dataclass(frozen=True)
class Source:
    """How one bit of input is going to be downloaded."""

    kind: str  # "ytdlp", "apple", "search" or "unsupported"
    name: str  # what to call it in the interface, e.g. "SoundCloud"
    url: str  # what gets handed to the downloader
    problem: str = ""  # why it cannot be downloaded, for "unsupported"
    ambiguous_playlist: bool = False  # a video link that is also in a playlist

    @property
    def supported(self):
        return self.kind != "unsupported"


def looks_like_url(text):
    return bool(URL_LIKE.match(text.strip()))


def _host(url):
    parsed = urlparse(url)
    return parsed.netloc.lower().split("@")[-1].split(":")[0]


def _match_domain(host, table):
    """Look ``host`` up in ``table``, allowing subdomains ("artist.bandcamp.com")."""
    for domain in sorted(table, key=len, reverse=True):
        if host == domain or host.endswith("." + domain):
            return table[domain]
    return None


def _is_ambiguous_playlist(url, host):
    """A YouTube link to one video that also carries a playlist ("&list=")."""
    if _match_domain(host, {"youtube.com": 1, "youtu.be": 1}) is None:
        return False
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "list" not in query:
        return False
    # youtube.com/playlist?list=... is unambiguously the whole playlist.
    return "v" in query or host.endswith("youtu.be") or "/shorts/" in parsed.path


def local_path(text):
    """The file or folder ``text`` names on this computer, or None.

    Accepts what terminals paste when a file is dragged in: a plain path,
    one in single or double quotes, one with backslash-escaped spaces
    ("My\\ Song.mp3"), or a file:// URI.
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    if text.lower().startswith("file://"):
        text = unquote(urlparse(text).path)
        if re.match(r"^/[A-Za-z]:", text):  # file:///C:/Music on Windows
            text = text[1:]
    elif "://" in text:
        return None
    if not text:
        return None

    candidates = [text]
    if os.sep == "/" and "\\" in text:
        candidates.append(re.sub(r"\\(.)", r"\1", text))
    for candidate in candidates:
        try:
            path = Path(candidate).expanduser()
            if path.exists():
                return path.resolve()
        except (OSError, ValueError):
            continue
    return None


def local_audio(path):
    """The audio files a local file or folder holds, in name order."""
    path = Path(path)
    if path.is_file():
        return [path] if path.suffix.lower() in LOCAL_EXTENSIONS else []
    files = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in LOCAL_EXTENSIONS]
    return sorted(files, key=lambda p: [part.lower() for part in p.relative_to(path).parts])


def identify(text):
    """Return a :class:`Source` describing how to download ``text``."""
    text = text.strip()

    path = local_path(text)
    if path is not None:
        if not local_audio(path):
            return Source("unsupported", "Local", str(path), problem="No audio files there.")
        return Source("local", "Folder" if path.is_dir() else "File", str(path))

    if not looks_like_url(text):
        return Source("search", "Search", f"ytsearch1:{text}")

    url = text if "://" in text else f"https://{text}"
    host = _host(url)

    if host in APPLE_HOSTS:
        return Source("apple", "Apple Music", url)

    service = _match_domain(host, DRM_SERVICES)
    if service:
        return Source(
            "unsupported",
            service,
            url,
            problem=(
                f"{service} encrypts its audio, so it cannot be downloaded. "
                "Type the song's name instead and it will be found on YouTube."
            ),
        )

    name = _match_domain(host, SITE_NAMES)
    if name is None:
        name = host[4:] if host.startswith("www.") else host
    return Source(
        "ytdlp",
        name,
        url,
        ambiguous_playlist=_is_ambiguous_playlist(url, host),
    )


def split_input(text):
    """Split pasted text into one entry per link.

    Several links pasted together become separate downloads, even when they
    ended up glued together without a space ("https://a...https://b...").
    Anything that is not purely links is kept whole, since it is probably a
    search.

    Files dragged into the terminal arrive as paths, quoted when they contain
    spaces; each file or folder becomes its own entry too.
    """
    whole = local_path(text)
    if whole:
        return [str(whole)]
    tokens = ["".join(match) for match in PASTED_TOKEN.findall(text)]
    if len(tokens) > 1 and all(local_path(token) or looks_like_url(token) for token in tokens):
        return [str(local_path(token) or token) for token in tokens]

    parts = [part for chunk in text.split() for part in GLUED_URLS.split(chunk) if part]
    if len(parts) > 1 and all(looks_like_url(part) for part in parts):
        return parts
    text = " ".join(parts)
    return [text] if text else []
