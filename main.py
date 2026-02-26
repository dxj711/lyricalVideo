import base64
import json
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests
import streamlit as st
import streamlit.components.v1 as components

# -----------------------------
# App/Page Configuration
# -----------------------------
st.set_page_config(
    page_title="Music Video Lyrics Overlay",
    page_icon="??",
    layout="wide",
)

# -----------------------------
# Constants
# -----------------------------
SUPPORTED_EXTENSIONS = {"mp4", "mov", "webm", "m4v", "ogg"}
MAX_OVERLAY_FILE_MB = 60  # Keep HTML payload practical for browser rendering
LYRICS_TIMEOUT_SECONDS = 12
LYRICS_MAX_RETRIES = 3
LYRICS_BACKOFF_SECONDS = 1.2
FALLBACK_SECONDS_PER_LINE = 2.8


# -----------------------------
# Utility: Input Validation
# -----------------------------
def is_supported_video(filename: str, mime_type: str) -> bool:
    """Validate video format using extension and MIME type."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime_ok = mime_type.startswith("video/") if mime_type else False
    return extension in SUPPORTED_EXTENSIONS and mime_ok


def extract_youtube_video_id(url: str) -> Optional[str]:
    """Extract a YouTube video id from common watch/share/embed URL formats."""
    if not url:
        return None

    raw = url.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"

    try:
        parsed = urlparse(raw)
    except Exception:
        return None

    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    video_id = None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        path = (parsed.path or "").strip("/")
        if path == "watch":
            video_id = parse_qs(parsed.query or "").get("v", [None])[0]
        elif path.startswith("shorts/"):
            video_id = path.split("/", 1)[1].split("/")[0]
        elif path.startswith("embed/"):
            video_id = path.split("/", 1)[1].split("/")[0]

    if not video_id:
        return None

    video_id = video_id.strip()
    if re.fullmatch(r"[\w-]{11}", video_id):
        return video_id
    return None


def is_youtube_url(url: str) -> bool:
    """Validate YouTube URL by checking whether a video id can be extracted."""
    return extract_youtube_video_id(url) is not None


def fetch_youtube_video_title(youtube_url: str) -> Optional[str]:
    """Fetch a YouTube video title using oEmbed endpoint (no API key required)."""
    try:
        endpoint = "https://www.youtube.com/oembed"
        response = requests.get(
            endpoint,
            params={"url": youtube_url, "format": "json"},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        title = str(payload.get("title", "")).strip() if isinstance(payload, dict) else ""
        return title or None
    except requests.exceptions.RequestException:
        return None
    except Exception:
        return None


def derive_song_query_from_video_title(video_title: str) -> str:
    """Convert a YouTube video title into a probable song query."""
    title = (video_title or "").strip()
    if not title:
        return ""

    # Remove common non-song tags.
    title = re.sub(r"\[[^\]]*(official|video|lyrics?|audio|mv|hd|4k)[^\]]*\]", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\([^\)]*(official|video|lyrics?|audio|mv|hd|4k)[^\)]*\)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" -|")

    # Prefer the track segment when title is in Artist - Track format.
    for sep in (" - ", " | ", " : "):
        if sep in title:
            parts = [p.strip() for p in title.split(sep) if p.strip()]
            if len(parts) >= 2:
                title = parts[-1]
                break

    title = re.sub(r"\s+", " ", title).strip(" -|")
    return title


# -----------------------------
# Utility: Lyrics Fetching
# Primary API: https://lyricsovh.docs.apiary.io/
# Fallback API: https://lrclib.net/docs
# No API key required for either.
# -----------------------------
def _get_json_with_retries(url: str, timeout_seconds: int = LYRICS_TIMEOUT_SECONDS):
    """
    Perform GET with retry/backoff for transient failures.
    Retries on timeout, connection errors, and retryable HTTP status codes.
    """
    last_error = None

    for attempt in range(1, LYRICS_MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=timeout_seconds)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response is not None else None
            retryable = status_code in {408, 429, 500, 502, 503, 504}
            if attempt < LYRICS_MAX_RETRIES and retryable:
                time.sleep(LYRICS_BACKOFF_SECONDS * attempt)
                continue
            raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            if attempt < LYRICS_MAX_RETRIES:
                time.sleep(LYRICS_BACKOFF_SECONDS * attempt)
                continue
            raise

    if last_error:
        raise last_error
    raise requests.exceptions.RequestException("Unknown network error while requesting lyrics API.")


def _search_song_candidates(song_name: str) -> List[dict]:
    """Search possible tracks by song title via lyrics.ovh suggest endpoint."""
    payload = _get_json_with_retries(
        f"https://api.lyrics.ovh/suggest/{requests.utils.quote(song_name)}",
    )
    return payload.get("data", []) if isinstance(payload, dict) else []


def _fetch_lyrics(artist: str, title: str) -> str:
    """Fetch full lyrics for exact artist/title via lyrics.ovh v1 endpoint."""
    payload = _get_json_with_retries(
        f"https://api.lyrics.ovh/v1/{requests.utils.quote(artist)}/{requests.utils.quote(title)}",
    )
    lyrics = payload.get("lyrics") if isinstance(payload, dict) else None
    return lyrics.strip() if lyrics else ""


def _find_and_fetch_lyrics_lyricsovh(song_name: str) -> tuple[str, str, str]:
    """
    Find best match and return: (lyrics, matched_title, matched_artist).

    Strategy:
    1) Search candidate songs by title
    2) Prefer exact (case-insensitive) title match
    3) Fall back to top candidate
    4) Fetch lyrics for chosen candidate
    """
    candidates = _search_song_candidates(song_name)
    if not candidates:
        raise ValueError("Song not found in lyrics search results.")

    target_lower = song_name.strip().lower()
    chosen = None

    for candidate in candidates:
        title = str(candidate.get("title", "")).strip().lower()
        if title == target_lower:
            chosen = candidate
            break

    if chosen is None:
        chosen = candidates[0]

    matched_title = str(chosen.get("title", "")).strip()
    artist_obj = chosen.get("artist") if isinstance(chosen.get("artist"), dict) else {}
    matched_artist = str(artist_obj.get("name", "")).strip()

    if not matched_title or not matched_artist:
        raise ValueError("Unable to resolve artist/title for the requested song.")

    lyrics = _fetch_lyrics(matched_artist, matched_title)
    if not lyrics:
        raise ValueError("Lyrics were not available for the matched song.")

    return lyrics, matched_title, matched_artist


def _find_and_fetch_lyrics_lrclib(song_name: str) -> tuple[str, str, str]:
    """
    Fallback lyrics provider using LRCLIB.
    Uses search endpoint and picks exact title match first, then top result.
    """
    payload = _get_json_with_retries(
        f"https://lrclib.net/api/search?q={requests.utils.quote(song_name)}"
    )

    if not isinstance(payload, list) or not payload:
        raise ValueError("Song not found in fallback lyrics search results.")

    target_lower = song_name.strip().lower()
    chosen = None
    for item in payload:
        track_name = str(item.get("trackName", "")).strip().lower()
        if track_name == target_lower:
            chosen = item
            break
    if chosen is None:
        chosen = payload[0]

    matched_title = str(chosen.get("trackName", "")).strip()
    matched_artist = str(chosen.get("artistName", "")).strip()

    if not matched_title or not matched_artist:
        raise ValueError("Unable to resolve fallback artist/title for the requested song.")

    lyrics = str(chosen.get("plainLyrics", "")).strip()
    if not lyrics:
        # Some entries might only include synced lyrics.
        lyrics = str(chosen.get("syncedLyrics", "")).strip()
    if not lyrics:
        raise ValueError("Lyrics were not available from the fallback provider.")

    return lyrics, matched_title, matched_artist


def _search_lrclib_candidates(song_name: str) -> List[dict]:
    """Search lrclib tracks for possible matches by song name."""
    payload = _get_json_with_retries(
        f"https://lrclib.net/api/search?q={requests.utils.quote(song_name)}"
    )
    return payload if isinstance(payload, list) else []


def _pick_best_lrclib_candidate(
    candidates: List[dict],
    song_name: str,
    preferred_title: Optional[str] = None,
    preferred_artist: Optional[str] = None,
) -> Optional[dict]:
    """Pick best lrclib match, preferring exact title+artist, then exact title, then first result."""
    if not candidates:
        return None
    if preferred_title and preferred_artist:
        t = preferred_title.strip().lower()
        a = preferred_artist.strip().lower()
        for item in candidates:
            track_name = str(item.get("trackName", "")).strip().lower()
            artist_name = str(item.get("artistName", "")).strip().lower()
            if track_name == t and artist_name == a:
                return item

    if preferred_title:
        t = preferred_title.strip().lower()
        for item in candidates:
            track_name = str(item.get("trackName", "")).strip().lower()
            if track_name == t:
                return item

    target_lower = song_name.strip().lower()
    for item in candidates:
        track_name = str(item.get("trackName", "")).strip().lower()
        if track_name == target_lower:
            return item
    return candidates[0]


def fetch_synced_lyrics_lrc(
    song_name: str,
    preferred_title: Optional[str] = None,
    preferred_artist: Optional[str] = None,
) -> Optional[str]:
    """
    Fetch LRC-style synced lyrics from lrclib when available.
    Returns None if synced lyrics are not present.
    """
    candidates = _search_lrclib_candidates(song_name)
    chosen = _pick_best_lrclib_candidate(
        candidates,
        song_name,
        preferred_title=preferred_title,
        preferred_artist=preferred_artist,
    )
    if not chosen:
        return None
    synced = str(chosen.get("syncedLyrics", "")).strip()
    return synced if synced else None


def find_and_fetch_lyrics(song_name: str) -> tuple[str, str, str]:
    """
    Resolve lyrics using primary provider, then fallback provider when needed.
    """
    primary_error = None
    try:
        return _find_and_fetch_lyrics_lyricsovh(song_name)
    except (requests.exceptions.RequestException, ValueError) as exc:
        primary_error = exc

    try:
        return _find_and_fetch_lyrics_lrclib(song_name)
    except (requests.exceptions.RequestException, ValueError) as fallback_exc:
        if isinstance(primary_error, requests.exceptions.Timeout) and isinstance(
            fallback_exc, requests.exceptions.Timeout
        ):
            raise requests.exceptions.Timeout("Both lyrics providers timed out.") from fallback_exc
        if isinstance(primary_error, ValueError) and isinstance(fallback_exc, ValueError):
            raise ValueError(
                "Song not found or lyrics unavailable across both providers."
            ) from fallback_exc
        raise requests.exceptions.RequestException(
            "Lyrics lookup failed across both providers."
        ) from fallback_exc


# -----------------------------
# Utility: Prepare line-based captions
# -----------------------------
def parse_lrc_synced_lines(lrc_text: str) -> List[Tuple[float, str]]:
    """
    Parse LRC timestamps like [mm:ss.xx] into sorted (seconds, lyric line) cues.
    Supports multiple timestamps on one line.
    """
    if not lrc_text:
        return []

    cues: List[Tuple[float, str]] = []
    line_pattern = re.compile(r"(\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\])+(.+)?$")
    ts_pattern = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")

    for raw_line in lrc_text.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        m = line_pattern.match(line)
        if not m:
            continue

        lyric_text = (m.group(5) or "").strip()
        if not lyric_text:
            continue

        for ts in ts_pattern.finditer(line):
            minutes = int(ts.group(1))
            seconds = int(ts.group(2))
            fraction = ts.group(3) or "0"
            # LRC fraction can be centiseconds or milliseconds.
            millis = int((fraction + "000")[:3])
            total_seconds = (minutes * 60) + seconds + (millis / 1000.0)
            cues.append((total_seconds, lyric_text))

    cues.sort(key=lambda x: x[0])

    # Deduplicate exact repeats (same timestamp + same text)
    deduped: List[Tuple[float, str]] = []
    last = None
    for cue in cues:
        if cue != last:
            deduped.append(cue)
        last = cue
    return deduped


def normalize_lyrics_lines(lyrics_text: str) -> List[str]:
    """Split and normalize lyrics into clean, non-empty display lines."""
    lines = [line.strip() for line in lyrics_text.replace("\r", "").split("\n")]
    return [line for line in lines if line]


def _build_overlay_cues(
    lines: List[str],
    seconds_per_line: float,
    synced_cues: Optional[List[Tuple[float, str]]] = None,
) -> List[dict]:
    """Build [{start, end, text}] cues for kinetic lyric rendering."""
    cues: List[dict] = []
    if synced_cues:
        for i, (start_sec, text) in enumerate(synced_cues):
            if i + 1 < len(synced_cues):
                next_start = synced_cues[i + 1][0]
                end_sec = max(start_sec + 0.15, next_start - 0.05)
            else:
                end_sec = start_sec + max(2.0, seconds_per_line)
            cues.append({"start": round(start_sec, 3), "end": round(end_sec, 3), "text": text})
        return cues

    for i, line in enumerate(lines):
        start_sec = i * seconds_per_line
        end_sec = (i + 1) * seconds_per_line
        cues.append({"start": round(start_sec, 3), "end": round(end_sec, 3), "text": line})
    return cues


def build_overlay_html(
    video_mime: str,
    video_bytes: bytes,
    lines: List[str],
    seconds_per_line: float,
    synced_cues: Optional[List[Tuple[float, str]]] = None,
) -> str:
    """Build modern kinetic typography lyric overlay player."""
    # Base64-embed video so the player stays fully self-contained in one Streamlit file.
    b64_video = base64.b64encode(video_bytes).decode("ascii")
    overlay_cues = _build_overlay_cues(lines, seconds_per_line, synced_cues=synced_cues)
    cues_json = json.dumps(overlay_cues)

    # Kinetic typography styling: large centered line, active word accent + scale.
    return f"""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Bangers&display=swap');

      * {{
        box-sizing: border-box;
      }}

      .overlay-wrap {{
        position: relative;
        width: min(1320px, 98vw);
        margin: 1rem auto 1.25rem auto;
        border-radius: 16px;
        overflow: hidden;
        box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
        background: linear-gradient(180deg, #0e1624, #090c12);
      }}

      .overlay-video {{
        width: 100%;
        display: block;
        background: #000;
      }}

      .kinetic-layer {{
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        pointer-events: none;
        padding: 0 4vw;
      }}

      .lyric-pill {{
        position: relative;
        width: 40vw;
        max-width: 760px;
        min-width: 280px;
        text-align: center;
        font-family: 'Bangers', 'Segoe UI', sans-serif;
        font-weight: 400;
        line-height: 1.22;
        font-size: clamp(1.65rem, 4.2vw, 3.7rem);
        color: #d1d5db;
        letter-spacing: 0.03em;
        text-shadow: 0 8px 30px rgba(0, 0, 0, 0.56);
      }}

      .lyric-pill::before {{
        content: "";
        position: absolute;
        inset: -18px -26px;
        border-radius: 22px;
        background: rgba(5, 10, 20, 0.24);
        backdrop-filter: blur(10px) saturate(120%);
        -webkit-backdrop-filter: blur(10px) saturate(120%);
        z-index: -1;
      }}

      .word {{
        display: inline-block;
        margin: 0 0.14em;
        transform-origin: 50% 60%;
        transition: color 220ms ease, transform 220ms ease, text-shadow 220ms ease;
      }}

      .word.active {{
        color: #6b7280;
        transform: scale(1.05);
        text-shadow: 0 0 18px rgba(17, 24, 39, 0.42), 0 0 4px rgba(0, 0, 0, 0.58);
      }}
    </style>

    <div class="overlay-wrap">
      <video id="lyricsVideo" class="overlay-video" controls preload="metadata">
        <source src="data:{video_mime};base64,{b64_video}" type="{video_mime}">
        Your browser does not support the video tag.
      </video>

      <div class="kinetic-layer">
        <div id="lyricLine" class="lyric-pill"></div>
      </div>
    </div>

    <script>
      const cues = {cues_json};
      const videoEl = document.getElementById("lyricsVideo");
      const lineEl = document.getElementById("lyricLine");

      function esc(str) {{
        return str.replace(/&/g, "&amp;")
                  .replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;");
      }}

      function findCueIndex(t) {{
        for (let i = 0; i < cues.length; i += 1) {{
          if (t >= cues[i].start && t < cues[i].end) return i;
        }}
        return cues.length ? cues.length - 1 : -1;
      }}

      function renderCue(cue, t) {{
        const text = (cue.text || "").trim();
        if (!text) {{
          lineEl.innerHTML = "";
          return;
        }}

        const words = text.split(/\\s+/).filter(Boolean);
        if (!words.length) {{
          lineEl.innerHTML = esc(text);
          return;
        }}

        const duration = Math.max(0.15, cue.end - cue.start);
        const progress = Math.min(0.999, Math.max(0, (t - cue.start) / duration));
        const activeIdx = Math.min(words.length - 1, Math.floor(progress * words.length));

        lineEl.innerHTML = words
          .map((w, idx) => `<span class="word ${{idx === activeIdx ? "active" : ""}}">${{esc(w)}}</span>`)
          .join(" ");
      }}

      function tick() {{
        const t = videoEl.currentTime || 0;
        const idx = findCueIndex(t);
        if (idx >= 0) renderCue(cues[idx], t);
      }}

      videoEl.addEventListener("timeupdate", tick);
      videoEl.addEventListener("seeked", tick);
      videoEl.addEventListener("loadedmetadata", tick);
      videoEl.addEventListener("play", tick);
      tick();
    </script>
    """


def build_youtube_overlay_html(
    youtube_video_id: str,
    lines: List[str],
    seconds_per_line: float,
    synced_cues: Optional[List[Tuple[float, str]]] = None,
) -> str:
    """Build kinetic typography lyric overlay player for YouTube videos."""
    overlay_cues = _build_overlay_cues(lines, seconds_per_line, synced_cues=synced_cues)
    cues_json = json.dumps(overlay_cues)

    return f"""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Bangers&display=swap');

      * {{
        box-sizing: border-box;
      }}

      .overlay-wrap {{
        position: relative;
        width: min(1320px, 98vw);
        margin: 1rem auto 1.25rem auto;
        border-radius: 16px;
        overflow: hidden;
        box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
        background: linear-gradient(180deg, #0e1624, #090c12);
      }}

      .overlay-video {{
        width: 100%;
        aspect-ratio: 16 / 9;
        background: #000;
      }}

      .overlay-video iframe {{
        width: 100%;
        height: 100%;
      }}

      .kinetic-layer {{
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        pointer-events: none;
        padding: 0 4vw;
      }}

      .lyric-pill {{
        position: relative;
        width: 40vw;
        max-width: 760px;
        min-width: 280px;
        text-align: center;
        font-family: 'Bangers', 'Segoe UI', sans-serif;
        font-weight: 400;
        line-height: 1.22;
        font-size: clamp(1.65rem, 4.2vw, 3.7rem);
        color: #d1d5db;
        letter-spacing: 0.03em;
        text-shadow: 0 8px 30px rgba(0, 0, 0, 0.56);
      }}

      .lyric-pill::before {{
        content: "";
        position: absolute;
        inset: -18px -26px;
        border-radius: 22px;
        background: rgba(5, 10, 20, 0.24);
        backdrop-filter: blur(10px) saturate(120%);
        -webkit-backdrop-filter: blur(10px) saturate(120%);
        z-index: -1;
      }}

      .word {{
        display: inline-block;
        margin: 0 0.14em;
        transform-origin: 50% 60%;
        transition: color 220ms ease, transform 220ms ease, text-shadow 220ms ease;
      }}

      .word.active {{
        color: #6b7280;
        transform: scale(1.05);
        text-shadow: 0 0 18px rgba(17, 24, 39, 0.42), 0 0 4px rgba(0, 0, 0, 0.58);
      }}
    </style>

    <div class="overlay-wrap">
      <div id="ytPlayer" class="overlay-video"></div>
      <div class="kinetic-layer">
        <div id="lyricLine" class="lyric-pill"></div>
      </div>
    </div>

    <script>
      const cues = {cues_json};
      const lineEl = document.getElementById("lyricLine");
      const ytVideoId = {json.dumps(youtube_video_id)};
      let player = null;
      let rafId = null;

      function esc(str) {{
        return str.replace(/&/g, "&amp;")
                  .replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;");
      }}

      function findCueIndex(t) {{
        for (let i = 0; i < cues.length; i += 1) {{
          if (t >= cues[i].start && t < cues[i].end) return i;
        }}
        return cues.length ? cues.length - 1 : -1;
      }}

      function renderCue(cue, t) {{
        const text = (cue.text || "").trim();
        if (!text) {{
          lineEl.innerHTML = "";
          return;
        }}

        const words = text.split(/\\s+/).filter(Boolean);
        if (!words.length) {{
          lineEl.innerHTML = esc(text);
          return;
        }}

        const duration = Math.max(0.15, cue.end - cue.start);
        const progress = Math.min(0.999, Math.max(0, (t - cue.start) / duration));
        const activeIdx = Math.min(words.length - 1, Math.floor(progress * words.length));

        lineEl.innerHTML = words
          .map((w, idx) => `<span class="word ${{idx === activeIdx ? "active" : ""}}">${{esc(w)}}</span>`)
          .join(" ");
      }}

      function currentTime() {{
        if (!player || typeof player.getCurrentTime !== "function") return 0;
        return Number(player.getCurrentTime()) || 0;
      }}

      function tick() {{
        const t = currentTime();
        const idx = findCueIndex(t);
        if (idx >= 0) renderCue(cues[idx], t);
        rafId = window.requestAnimationFrame(tick);
      }}

      function stopTicking() {{
        if (rafId) {{
          window.cancelAnimationFrame(rafId);
          rafId = null;
        }}
      }}

      function onPlayerReady() {{
        const idx = findCueIndex(currentTime());
        if (idx >= 0) renderCue(cues[idx], currentTime());
      }}

      function onPlayerStateChange(event) {{
        if (!window.YT || !window.YT.PlayerState) return;
        if (event.data === window.YT.PlayerState.PLAYING) {{
          if (!rafId) tick();
        }} else {{
          stopTicking();
          const t = currentTime();
          const idx = findCueIndex(t);
          if (idx >= 0) renderCue(cues[idx], t);
        }}
      }}

      function initPlayer() {{
        if (player || !window.YT || !window.YT.Player) return;
        player = new window.YT.Player("ytPlayer", {{
          videoId: ytVideoId,
          width: "100%",
          height: "100%",
          playerVars: {{
            playsinline: 1,
            rel: 0,
            modestbranding: 1
          }},
          events: {{
            onReady: onPlayerReady,
            onStateChange: onPlayerStateChange
          }}
        }});
      }}

      (function boot() {{
        const prevReady = window.onYouTubeIframeAPIReady;
        window.onYouTubeIframeAPIReady = function () {{
          if (typeof prevReady === "function") prevReady();
          initPlayer();
        }};

        if (window.YT && window.YT.Player) {{
          initPlayer();
          return;
        }}

        const existing = document.querySelector('script[src="https://www.youtube.com/iframe_api"]');
        if (!existing) {{
          const tag = document.createElement("script");
          tag.src = "https://www.youtube.com/iframe_api";
          document.head.appendChild(tag);
        }}
      }})();
    </script>
    """


# -----------------------------
# Streamlit UI
# -----------------------------
st.markdown(
    """
    <style>
      .stApp {
        background: radial-gradient(circle at top right, #1f2a44 0%, #0a0f18 55%, #05070c 100%);
      }
      .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
        max-width: 1500px;
      }
      [data-testid="stVideo"] video {
        width: 100% !important;
        max-height: 78vh;
      }
      .app-title {
        font-size: clamp(1.6rem, 3.2vw, 2.35rem);
        font-weight: 800;
        color: #f8fafc;
        margin-bottom: 0.2rem;
      }
      .app-sub {
        color: #cbd5e1;
        margin-bottom: 1rem;
      }
      .note {
        color: #dbeafe;
        font-size: 0.93rem;
      }
    </style>
    <div class="app-title">Music Video Lyrics Overlay</div>
    <div class="app-sub">Upload a video, enter a song name (or auto-detect), and play with synchronized lyric captions.</div>
    """,
    unsafe_allow_html=True,
)

# Inputs required by prompt
video_source = st.radio(
    "Video source",
    options=["Upload from local storage", "YouTube URL"],
    horizontal=True,
)

uploaded_video = None
youtube_url = ""
if video_source == "Upload from local storage":
    uploaded_video = st.file_uploader(
        "Upload a music video",
        type=list(SUPPORTED_EXTENSIONS),
        accept_multiple_files=False,
        help="Supported: MP4, MOV, WEBM, M4V, OGG",
    )
else:
    youtube_url = st.text_input(
        "YouTube video URL",
        placeholder="https://www.youtube.com/watch?v=...",
    ).strip()

song_name = st.text_input(
    "Song name",
    placeholder="Optional",
    help="For YouTube URLs, leave blank to auto-derive from video title when possible.",
)

run_btn = st.button("Fetch Lyrics + Build Overlay", type="primary")

# API information block (requested in prompt)
with st.expander("API details and configuration", expanded=False):
    st.markdown(
        """
- **Primary lyrics API:** `lyrics.ovh` (public API)
  - `GET https://api.lyrics.ovh/suggest/<song_name>`
  - `GET https://api.lyrics.ovh/v1/<artist>/<title>`
- **Fallback lyrics API:** `lrclib.net` (public API)
  - `GET https://lrclib.net/api/search?q=<song_name>`
- **YouTube title lookup (for blank song input):** `youtube oEmbed`
  - `GET https://www.youtube.com/oembed?url=<youtube_url>&format=json`
- **API key required:** No (for all providers)
- **Configuration:** None required. The app calls public endpoints directly with automatic retry + fallback.
- **Timestamped sync:** When available, the app uses `syncedLyrics` (LRC timestamps) from `lrclib`.
        """
    )

if run_btn:
    # -----------------------------
    # Error handling for empty inputs
    # -----------------------------
    using_uploaded_video = video_source == "Upload from local storage"
    using_youtube_url = video_source == "YouTube URL"
    youtube_video_id = extract_youtube_video_id(youtube_url) if using_youtube_url else None

    if using_uploaded_video and not uploaded_video:
        st.error("Please upload a video file.")
        st.stop()
    if using_youtube_url and not youtube_url:
        st.error("Please enter a YouTube URL.")
        st.stop()
    if using_youtube_url and not youtube_video_id:
        st.error("Please enter a valid YouTube watch/share URL.")
        st.stop()

    song_query = (song_name or "").strip()
    if using_youtube_url and not song_query:
        with st.spinner("Reading video title from YouTube URL..."):
            video_title = fetch_youtube_video_title(youtube_url)
        if video_title:
            inferred_query = derive_song_query_from_video_title(video_title)
            if inferred_query:
                song_query = inferred_query
                st.info(f'Inferred song query from video title "{video_title}": {song_query}')

    if not song_query:
        if using_uploaded_video:
            st.error("Please enter the song name.")
        else:
            st.error("Please enter the song name, or use a YouTube URL with a readable video title.")
        st.stop()

    # -----------------------------
    # Error handling for invalid format
    # -----------------------------
    video_bytes = b""
    video_mime = "video/mp4"
    if using_uploaded_video:
        if not is_supported_video(uploaded_video.name, uploaded_video.type):
            st.error(
                "Unsupported video format. Please upload a valid video file (MP4, MOV, WEBM, M4V, OGG)."
            )
            st.stop()

        video_bytes = uploaded_video.getvalue()
        video_mime = uploaded_video.type or "video/mp4"
        if not video_bytes:
            st.error("Uploaded video appears to be empty or unreadable.")
            st.stop()

        # Built-in Streamlit video display (requested)
        st.video(video_bytes)

    with st.spinner("Looking up song and fetching lyrics..."):
        try:
            lyrics_text, matched_title, matched_artist = find_and_fetch_lyrics(song_query)
        except requests.exceptions.Timeout:
            st.error("Lyrics providers timed out after retries. Please try again in a moment.")
            st.stop()
        except requests.exceptions.HTTPError as exc:
            st.error(f"Lyrics API returned an HTTP error: {exc}")
            st.stop()
        except requests.exceptions.RequestException:
            st.error("Could not reach the lyrics API due to a network/API failure.")
            st.stop()
        except ValueError as exc:
            # Song not found / lyrics unavailable
            st.error(str(exc))
            st.stop()
        except Exception:
            st.error("An unexpected error occurred while fetching lyrics.")
            st.stop()

    with st.spinner("Checking for timestamped lyrics..."):
        synced_cues = []
        try:
            synced_lrc = fetch_synced_lyrics_lrc(
                song_query,
                preferred_title=matched_title,
                preferred_artist=matched_artist,
            )
            if synced_lrc:
                synced_cues = parse_lrc_synced_lines(synced_lrc)
        except requests.exceptions.RequestException:
            # Non-fatal: we can still render fixed-interval captions.
            synced_cues = []
        except Exception:
            synced_cues = []

    lines = normalize_lyrics_lines(lyrics_text)

    if not lines:
        st.error("Lyrics were fetched but no usable lines were found.")
        st.stop()

    # If the file is too large, avoid building enormous inlined HTML payload.
    st.success(f"Matched: {matched_title} - {matched_artist}")
    if synced_cues:
        st.info(f"Using timestamped lyrics sync ({len(synced_cues)} timed cues from lrclib).")
    else:
        st.info(
            f"Timestamped lyrics not available. Falling back to fixed interval ({FALLBACK_SECONDS_PER_LINE:.2f}s per line)."
        )

    if using_uploaded_video:
        size_mb = len(video_bytes) / (1024 * 1024)
        if size_mb > MAX_OVERLAY_FILE_MB:
            st.warning(
                f"Video is {size_mb:.1f} MB. Overlay player is disabled above {MAX_OVERLAY_FILE_MB} MB to keep the app stable."
            )
            st.info("Lyrics preview:")
            st.code("\n".join(lines[:30]) + ("\n..." if len(lines) > 30 else ""))
            st.stop()

        # -----------------------------
        # Render custom overlay block
        # -----------------------------
        overlay_html = build_overlay_html(
            video_mime=video_mime,
            video_bytes=video_bytes,
            lines=lines,
            seconds_per_line=FALLBACK_SECONDS_PER_LINE,
            synced_cues=synced_cues,
        )

        st.markdown("### Overlay Player")
        st.markdown(
            "Play the video below to view kinetic typography synced over the video.",
        )
        components.html(overlay_html, height=860, scrolling=False)
    else:
        overlay_html = build_youtube_overlay_html(
            youtube_video_id=youtube_video_id,
            lines=lines,
            seconds_per_line=FALLBACK_SECONDS_PER_LINE,
            synced_cues=synced_cues,
        )
        st.markdown("### Overlay Player")
        st.markdown(
            "Play the YouTube video below to view kinetic typography synced over the video.",
        )
        components.html(overlay_html, height=860, scrolling=False)

    # Optional raw lyrics block
    with st.expander("Show full fetched lyrics", expanded=False):
        st.text_area("Lyrics", value=lyrics_text, height=240)

else:
    st.markdown(
        '<div class="note">Choose local upload or YouTube URL, enter a song title (optional for YouTube), then click <b>Fetch Lyrics + Build Overlay</b>.</div>',
        unsafe_allow_html=True,
    )
