# KORTC — Frontend Rewrite Notes

Working notes for the song-pick console rewrite. The real app runs inside a
**Docker container** at `/workspace/workspace`; this host dir holds editable
copies of the files I touched (`console.html`, `db.py`, `main.py`).

## What this is

`console.html` is the **user-facing song-pick UI** ("点歌台"), served at `/console`
and QR-linked from the TV display (`index.html` is the TV screen, not this).
It was rewritten from a vanilla-JS page into a **no-build React 18 app**.

## Deploy workflow (no build step, no restart for frontend)

1. Edit the host copy: `/Users/mingjun97/code/kortc/console.html`
2. Copy into the container:
   `docker cp console.html <container>:/workspace/workspace/console.html`
3. The aiohttp server reads the file **fresh per request** (`console` handler in
   `main.py`), so frontend changes are live immediately — no restart.
4. Verify it matches: compare `md5 console.html` (host) with
   `docker exec <container> md5sum /workspace/workspace/console.html`.

Find the container: `docker ps` (IDs change across restarts). The live server
listens on **:8080** (HTTPS, self-signed). To see the served bytes:
`docker exec <c> python3 -c "import ssl,urllib.request as u; ctx=ssl.create_default_context(); ctx.check_hostname=0; ctx.verify_mode=ssl.CERT_NONE; print(u.urlopen('https://127.0.0.1:8080/console',context=ctx).read()[:80])"`

**Caching:** the public domain (`kortc.lyric.today`) + iOS Safari cache the HTML.
To force-refresh: `…/console?v=N` (any new query) or pull-to-refresh. A permanent
`Cache-Control: no-cache` header was added to the `console` handler in `main.py`
(see "pending restart").

## How `console.html` is built (no bundler)

- ES modules from **esm.sh**: `react@18.3.1`, `react-dom@18.3.1/client`, `htm@3.1.1`.
- `htm` gives JSX-like tagged-template syntax — no Babel/build. Bound to a tiny
  `h()` wrapper that maps `class`→`className` and `for`→`htmlFor`.
- Single self-contained file (CSS in `<style>`, app in one `<script type=module>`).
- Validate syntax without a browser: extract the module body, stub the imports,
  run `node --check` (used throughout — there's no node toolchain in the container,
  only on the host).

### Component map
- `App` — i18n + live queue (WebSocket `/ws`), toasts, language menu, YT modal,
  confirm dialog, mini-player↔sheet state, mini "minimize" animation trigger.
- `BrowseView` — 3 pick modes via a segmented control:
  **Songs** (title/pinyin + singer autocomplete), **Singers** (grid → that
  singer's catalog), **YouTube** (`source:'ytb'` filter + "Add from YouTube" CTA).
  Infinite scroll for both song results and the singer grid; search header
  shrinks on scroll (`compact`).
- `Stage` — now-playing card + controls (vocal/replay/skip) + up-next queue with
  the custom overscroll reveal. Rendered twice: mobile bottom sheet + desktop side panel.
- `MiniPlayer` — glass pill in a fixed dock; tap or swipe-up to open the sheet.
- `PlayerSheet` — bottom sheet with swipe-down-to-dismiss.
- `Avatar` — real artist art via Deezer→iTunes JSONP, emoji fallback (see below).
- `Marquee` — long titles float left/right only when they overflow.

## Backend contract (unchanged unless noted)
- `POST /op` → `query`(returns `[count, rows]`, 10/page) | `add` | `top` | `remove` | `skip` | `replay`.
  `query` gained an optional `source:'ytb'` param (backend change).
- `GET /vocal`, `GET /singers`, `GET /i18n-{lang}.json`, `WS /ws` (live queue; idx 0 = now playing).
- Song row shape: `[SongID, name, singer, fileName]`. YouTube-added songs have a
  `(YTB)` suffix in the song NAME (stripped for display, shown as a `▶ YT` badge).

## Artist avatars
- JSONP (not fetch) avoids CORS: Deezer artist photo first, then iTunes album art,
  else emoji. Multi-singer `S1_S2` → look up the first (`firstSinger` splits on `_&＆、/`).
- Cached in memory + `localStorage` (hits persisted, misses retried next session),
  with in-flight dedupe. Singer grid loads avatars 60-at-a-time via infinite scroll.

## Backend changes — PENDING SERVER RESTART to take effect
These are deployed to the container but the running process still has the old
modules loaded. One restart (when no karaoke session is live — a busy
`python3 main.py` PID using lots of RAM/CPU means someone's singing) activates all:
1. `db.py get_singers()` — sources singers from `VOD_song` (27,862) instead of the
   stale `Singerinfo` table (1,702), **ranked by pick frequency** (`SUM(ClickCount)`),
   with a **≥2h TTL cache** (`_SINGERS_TTL = 7200`).
2. `db.py query()` + `main.py operation` — `source:'ytb'` filter (`SONGNAME LIKE '%(YTB)%'`).
3. `main.py console` handler — `Cache-Control: no-cache` headers (`NO_CACHE`).

Frontend degrades gracefully until then (YT browse works via the `(YTB)` keyword;
singer list shows 1,702 alphabetical). Restart roughly:
`pkill -f "python3 main.py"; cd /workspace/workspace && nohup ./start.sh >/tmp/kortc.log 2>&1 &`

## Gotchas / lessons learned (so future-me doesn't repeat them)
- **Bottom-sheet drag wouldn't move:** the sheet's open animation used
  `animation: slideUp … both`. A *filled* CSS animation overrides inline styles,
  so `style.transform` for drag/close was ignored. Fix: fill mode `backwards`.
- **`transition: none → 0.42s` + value change in the same frame doesn't animate
  in Safari** (it coalesces and snaps). Force a reflow (`void el.offsetHeight`)
  between setting the transition and changing the property.
- **`preventDefault` on a non-cancelable touchmove** throws the "Intervention …
  cancelable=false" warning and fights native scroll (jitter). Guard every
  touchmove `preventDefault` with `if (e.cancelable)`; only *claim* a custom
  gesture when cancelable.
- **Custom bottom-overscroll reveal** (pull up at the end of the queue to show
  "已经到底啦", spring back): the end-label must live **outside the scroll
  container**, anchored to a non-scrolling wrapper (`.upnext-zone`). An absolutely
  positioned element whose containing block is *inside* the scroller counts toward
  `scrollHeight`, so native scroll can reach it and (with `overscroll-behavior:none`)
  park there "stuck". Also: don't make the scroller `position:absolute` inside the
  zone or the flex chain collapses and the list disappears — keep it an in-flow
  `flex:1` child. The pull slides content + label in sync; `overscroll-behavior:none`
  disables the native rubber-band so a fast fling just clamps.
- **Long queue clipped the now-playing card:** the stage is a flex column, so a
  long list squeezed the fixed parts. `flex-shrink: 0` on
  `.np`/`.controls`/`.section-title`; only `.upnext-zone` absorbs space.
- **Mobile uses document-level scrolling** (not a locked `100dvh` shell) so iOS
  Safari can auto-hide its address bar; the desktop ≥860px breakpoint restores the
  fixed side-rail + stage-panel shell with inner scroll panes.

## Add-from-YouTube flow (`YtAddView`)

The "Add from YouTube" button used to open the **legacy `/yt` page in an iframe**.
It's now a native React component (`YtAddView`) inside the same `.yt-modal`, matching
the console design. Same backend endpoints, improved UX:

- **Two tabs** — *Add* and *Queue* (with a live badge counting in-flight imports).
- **Paste-in-search shortcut:** pasting a YouTube link into the Songs or YouTube search
  box (`matchYoutubeUrl` normalises it to a canonical watch URL) opens the modal already
  fetching that link — skipping the manual paste step. `openYt(url)` seeds `YtAddView`'s
  `initialUrl`, which auto-fetches on mount and shows a "Reading the video…" spinner until
  the review step is ready.
- **Paste link → Fetch** (`POST /yt_link`) → **review step**: video **thumbnail +
  duration** (new — the legacy page showed neither), editable song name / singer
  pre-filled from the video, **quick-fill chips** from `suggestions` (smart spacing:
  space only between ASCII tokens, CJK stays tight), singer **autocomplete** reusing
  `/singers`, and a **lyrics-source selector** (original / auto captions, or
  "recognize from audio").
- **Quick-fill candidates** show *all* tokens from `suggestions` (no cap). Each chip
  is **tap-to-append** to its field, *and* **press-and-drag for iOS-Photos pan-to-select**:
  the stroke selects the whole index range between the **anchor** (where the drag started)
  and the chip under the finger — so panning past chips you skipped fills them in, and it's
  **fully revertible** (pan back toward the anchor and the dropped chips are removed) until
  you lift. Implemented by rebuilding the field from its captured pre-drag value +
  `smartAppend` over `[anchor…current]` on every move (so reverting is just a shorter range);
  the contiguous range flashes `.picked`. On touch the stroke only **starts on a clearly
  horizontal swipe** (`|dx| > 1.2·|dy|` past a 12 px threshold); a vertical drag bails to native
  scrolling — chips use `touch-action: pan-y` (not `none`) so the browser keeps vertical scroll
  while horizontal gestures reach the handler, and a `touchmove` blocker engages only once a
  horizontal stroke commits. Mouse has no scroll conflict, so it paints in any direction past 8 px.
  Smart spacing on insert (space between ASCII tokens, none between CJK).
  Range logic is covered by node unit tests (forward / revert / reverse / cross-anchor / spacing).
- **Multiline title box:** the song-title field is an auto-growing `<textarea>` so a long
  title is always fully visible. Critically, its auto-resize is **frozen for the duration of
  a pan-select stroke** (`freezeTitleRef`) and only re-grows on release — otherwise adding
  tokens mid-pan would grow the box and shift the chips below it under the finger. Icon +
  clear button are pinned to the first line (`.ta-field`). Every `preventDefault` in the pan
  handler is guarded with `cancelable` to avoid the `[Intervention]` touchmove warning once
  the browser has committed to scrolling.
- **Double-check gate:** if the user submits the auto-parsed title *and* singer
  **untouched**, a confirm dialog first reminds them the server matches lyrics from the
  exact song name + singer. Editing either field skips the gate.
- **Submit** (`POST /add_yt_source`) → toast + jumps to the Queue tab.
- **Queue auto-polls** `/yt_list` (2.5 s while active, 6 s idle) — the legacy page
  needed a manual refresh — with color-coded status pills: Waiting / Processing
  (pulsing dot) / Ready / Failed (status thresholds: `>1000` done, `-1` error,
  `0` pending, else processing).
- Inline errors (no `alert()`), bilingual via `t()` (`yt_*` keys in `EXTRA`),
  mobile-responsive. The `.yt-modal` was narrowed (600 px) to suit a form.

Backend precondition: the `/yt_*` endpoints only exist when the server runs with
`--youtube`; errors are caught and surfaced inline if the agent isn't up.

Verified by rendering the component in headless Chrome with stubbed data
(entry / review / queue states) — see the standalone harness approach used during dev.

## Status
Frontend work is wrapped up. Remaining action item is the single backend restart above.
