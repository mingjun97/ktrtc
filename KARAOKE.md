# KORTC — Karaoke Video Generation

Notes for `kgen.py`, the automated karaoke-video generator, and the patched
`syncedlyrics` fork it depends on. The real app runs in a **Docker container**
at `/workspace/workspace`; this host dir holds editable copies. Find the
container with `docker ps` (IDs change across restarts).

## Pipeline (`kgen.py`)

```
input video ──▶ extract vocals (Spleeter, subprocess) ──▶ detect language (Whisper)
            ──▶ fetch lyrics (syncedlyrics, artist-qualified) ──▶ forced alignment (stable-ts)
            ──▶ _repair / _polish ──▶ build ASS (\kf karaoke) ──▶ burn (ffmpeg + libass)
```

Run it (vocals are cached per `--workdir` so reruns skip Spleeter):

```bash
python3 kgen.py input.mp4 --title "辞职信" --artist "ChiliChill" \
    --model small --out out.mp4 --ass /tmp/x.ass --workdir /tmp/kw_x
# --lyrics file.lrc  : skip lookup, use an explicit .lrc/.txt
# --vocals vocals.wav: skip Spleeter (reuse a cached stem)
# --no-burn          : write the ASS only
```

`batch.py` / `batch3.py` download YouTube IDs (pytubefix — **not** yt-dlp, which
OOMs the box) and call `kgen.py`, passing `yt.title` + `yt.author`.

### Design: timing is audio-driven

A single full-song **forced alignment** locates the lyrics in *this* vocal track,
so it survives the common case where web/API lyric timestamps don't match the
downloaded video (different intro / version / tempo). LRC absolute times are used
only as a **global offset** to repair lines the model couldn't align.

### Layout / rendering

- Two stacked lines at the bottom: earlier line bottom-**left** (upper), next line
  bottom-**right** (lower), alternating. Per-word `\kf` highlight + lead-in dots.
- Font: `Noto Sans CJK SC` (installed `fonts-noto-cjk` in the container — without it
  CJK renders as boxes). Size **126** default (1.5× the original 84). Works for zh / ja / en.
- **Burn canvas:** the ASS is authored at 1920×1080, so `burn()` ffprobes the source and
  renders onto **at least a 640p 16:9 canvas** (`scale=…:force_original_aspect_ratio=decrease`
  + `pad` + `setsar=1`). Tiny sources (e.g. a 640×360 progressive YouTube stream → 1136×640)
  get the subtitles rendered large and crisp since libass draws text at the *output* size;
  larger 16:9 sources are kept, capped at 1080p; odd aspect ratios are letterboxed/pillarboxed.
- If no official lyrics are found, a persistent bilingual hint is shown for the whole
  song ("⚠ 无官方歌词，以下歌词由人声识别生成 · No official lyrics — auto-recognized from vocals").
- **Section countdown:** at the song start or after a >4s instrumental gap, a blue
  `3 → 2 → 1` countdown (one second per frame) shows on its own line *above* the lyrics
  (3rd line from bottom, left-aligned, `CD` style), and the line it leads into is forced
  onto the first/upper line. This replaced the old inline `●●●` (which could overrun a
  <3s lead-in and eat the first word's time).

## Key fixes (and the bugs behind them)

**Trailing held-notes (`_fit_trailing`).** The aligner mis-places sustained final
words — labelling them late (a long unsung gap before the word) and/or running the
end into the next line. Fix: snap the final word onto the **dominant voiced run**
right after the previous word, measured from vocal RMS/dB. A held note must be
**followed by silence**; if the voiced run reaches the analysis-window edge (legato
stem / instrumental bleed / outro), the true end is unknown, so the aligner's timing
is left alone (prevents smearing the word to a 6–10 s blob). Verified against energy
ground truth (e.g. *If I Can Stop* `dream` ends 71.5 s, `unchained` 1:52→1:56).

**Wrong / polluted lyrics.** Two classes, both fixed:
- *Wrong song.* A bare title ("辞职信", "明年夏天") matches a different song with the
  same name. `kgen.py` now takes `--artist` and tries **artist-qualified queries first**
  (`lyric_queries(title, artist)`); the artist is what disambiguates.
- *Junk lines.* A copyright / anti-AI notice embedded in an LRC was not only shown
  on-screen but **broke forced alignment** (bogus +43 s offset). The lyric filter is
  now **structural**, not substring-based:
  - `_META` — credit lines = a role keyword + separator (`作词：`, `Produced by`). This
    keeps real lyrics that merely contain 微博 / 声明 / 鼓 / Written / Mix / by.
  - `_LYRIC_NOISE` — unconditional junk: legal notices, Genius artifacts (`[Chorus]`,
    `N Contributors`, `…Embed`), URLs.

**Unreliable alignment fallback.** When the forced-alignment anchors don't agree on a
single constant offset (the aligner drifted / locked onto a repeat), `_repair` falls
back to the **raw LRC timeline** instead of applying a meaningless median offset.

## syncedlyrics fork

Patched clone at `/Users/mingjun97/code/kortc/syncedlyrics` (dev venv at `.venv`):

- **`Kugeci`** (`providers/kugeci.py`) — kugeci.com Chinese lyrics. The site search is
  a dumb substring match, so it falls back to searching the song name alone, then
  fuzzy-matches `"<title> <artist>"` against the full term. Clean LRC comes from
  `kugeci.com/download/lrc/<id>`. Correctly disambiguates same-title songs.
- **`Lyricsify`** — session swapped to `cloudscraper` to pass Cloudflare. Caveat: the
  site currently serves a **managed/Turnstile challenge** that free cloudscraper can't
  solve (403); the integration is correct and works for solvable challenge types.
- Fixed a closure bug in `utils.sort_results` (string `compare_key` branch).
- Added `cloudscraper` to `pyproject.toml`; registered both providers in the chain.

### Installing the fork into the container

The container's old pip can't editable-install a pyproject-only project, so the files
were copied over the installed package + `cloudscraper` installed as a dep:

```bash
tar --exclude='.venv' --exclude='.git' -czf /tmp/sl.tgz syncedlyrics
docker cp /tmp/sl.tgz <c>:/tmp/ && docker exec <c> bash -lc 'cd /opt && tar xzf /tmp/sl.tgz'
docker exec <c> bash -lc 'cp -rf /opt/syncedlyrics/syncedlyrics/* \
    /usr/local/lib/python3.8/dist-packages/syncedlyrics/ && pip install cloudscraper'
```

⚠ This overwrites installed files; a future `pip install --upgrade syncedlyrics`
reverts it. For a permanent install, bake `pip install /opt/syncedlyrics` into the
image build.

## Remote worker (`kworker.py`)

A kgen-powered port of `agent/poller.py` — the script an external worker box runs to
take karaoke jobs off the server. Same wire protocol, so it's a drop-in replacement:

```
loop: GET /poll_task                      # long-poll → [stage, uuid, url, title, singer, caps]
      download progressive mp4 (pytubefix)
      [caps → fetch that YouTube caption track as an .lrc for --lyrics]
      python3 kgen.py input.mp4 --title <title> --artist <singer> [--lyrics caps.lrc]
      mux a 2nd audio track (accompaniment + faint vocal guide) from kgen's Spleeter stems
      POST /submit_task                   # multipart {uuid, file_content}
```

- **Lyrics** come from kgen (syncedlyrics incl. the Kugeci provider, audio-driven
  alignment, ASR fallback) instead of the old "burn the YouTube SRT or copy" path.
  `--artist` is passed so same-title songs resolve correctly.
- **Dual audio preserved** for the 原唱/伴奏 toggle: track 0 = original (with vocals),
  track 1 = accompaniment + `KWORKER_GUIDE_VOL` (default 0.08) vocal guide, built from
  the `accompaniment.wav`/`vocals.wav` Spleeter already produced in kgen's workdir — no
  second separation pass. Falls back to single-track if the stems are missing.
- **Error reporting is implicit** (unchanged from the original): a job that never gets
  submitted stays `stage = 1`, and the server flips it to `-1` ("Failed" in the console
  queue) on the next poll — so the worker always re-polls after each job. Finished jobs
  are reported explicitly via `/submit_task`. (If you want immediate/explicit failure
  signaling, add a small `/fail_task` endpoint and POST the uuid on exception.)
- **Config via env:** `KWORKER_SERVER` (default `https://kortc.lyric.today`),
  `KWORKER_KGEN` (path to kgen.py), `KWORKER_WORKDIR`, `KWORKER_INSECURE=1` (skip TLS
  verify for self-signed dev servers), `KWORKER_GUIDE_VOL`.
- **Deploy:** put `kworker.py` next to `kgen.py` on a box with the kgen deps (ffmpeg +
  fonts-noto-cjk, spleeter, stable-ts, syncedlyrics, pytubefix, requests,
  requests-toolbelt, youtube-transcript-api) and run `python3 kworker.py`. Verified
  end-to-end against a cached video: kgen found lyrics via the artist-qualified Kugeci
  query, aligned, burned, and the submitted file carried two aac audio tracks.

## Gotchas / environment

- Container is **CPU-only**, ~2.9 GB free RAM. Whisper `large` won't fit; `small`
  (~460 MB) is the default, `medium` is RAM-risky. Run Spleeter as a **subprocess**
  so its memory is freed before Whisper loads.
- `pytubefix` for downloads (yt-dlp install OOMs). Bot-detection can throw
  `KeyError 'videoDetails'`; recover by reusing a cached mp4 + a known title.
- Reusing a cached vocals stem: pass `--vocals .../vocals.wav` (the full-rate one,
  **not** `vocals16k.wav` — same path as the workdir output collides in ffmpeg).
