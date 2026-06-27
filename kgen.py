#!/usr/bin/env python3
"""
kgen.py — Karaoke music-video generator.

Pipeline:
  1. Extract the vocal track (Spleeter 2stems) — clean vocals align far better.
  2. (optional) Fetch lyrics from the web (syncedlyrics) or a file.
  3. Word-by-word forced alignment against the vocal track (stable_whisper).
     - If the lyrics are *synced* (LRC), each line is aligned inside its own
       human-timed window — robust, no global drift.
     - Plain lyrics -> one full-song forced alignment.
     - No lyrics    -> ASR transcription (weakest; CJK ASR is unreliable).
  4. Emit an ASS file with per-word \\kf karaoke highlighting.
  5. Burn the ASS onto the video with ffmpeg.

The ASS builder borrows the dual-line / lead-in / countdown ideas from the old
karaoke_gen.py, but the alignment is rebuilt from scratch.
"""
import argparse, os, re, subprocess, sys, tempfile

# ----------------------------- audio helpers ------------------------------

def run(cmd):
    subprocess.run(cmd, check=True)

def to_wav16k(src, dst):
    """Decode any media to 16 kHz mono PCM (what whisper wants)."""
    run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
         "-acodec", "pcm_s16le", "-loglevel", "error", dst])
    return dst

def extract_vocals(media, workdir):
    """Separate vocals with Spleeter (2stems). Returns a 16k-mono vocals wav.

    Skips separation if a vocals wav already exists in workdir.
    """
    os.makedirs(workdir, exist_ok=True)
    vocals = os.path.join(workdir, "vocals.wav")
    out16 = os.path.join(workdir, "vocals16k.wav")
    if not os.path.exists(vocals):
        print(f"[1/5] Separating vocals with Spleeter -> {workdir}")
        # Run Spleeter as a subprocess so TensorFlow's memory is released before
        # the Torch/Whisper stage (both in-process can OOM a small box).
        run([sys.executable, "-m", "spleeter", "separate", "-p", "spleeter:2stems",
             "-o", workdir, "-f", "{instrument}.{codec}", media])
    else:
        print(f"[1/5] Reusing existing vocals: {vocals}")
    return to_wav16k(vocals, out16)

# ----------------------------- lyric helpers ------------------------------

# lines that are credits/metadata, never sung (zh-simplified, zh-traditional, ja, en)
# A production-credit line: a role keyword paired with a "name" separator
# (a colon, or English "by"). Matching the *structure* — not the bare word —
# keeps real lyrics that merely mention "微博", "声明", "rain", "鼓起勇气" etc.,
# while still dropping "作詞 : Nu/查查", "Produced by X", "Mixing：Y".
_CREDIT_ROLE = (r"作词|作詞|作曲|编曲|編曲|监制|監製|制作|製作|出品|混音|母带|母帶|演唱|和声|和聲|"
                r"吉他|贝斯|貝斯|鼓手|录音|錄音|后期|後期|策划|策劃|统筹|統籌|制作人|製作人|演奏|うた|"
                r"Produc\w*|Compos\w*|Arrang\w*|Lyric\w*|Mix\w*|Master\w*|Vocals?|Writers?|Written|"
                r"Performed|Recorded|Engineer\w*|Label|微博|weibo|QQ")
_META = re.compile(r"^\s*(?:%s)\s*(?:[:：]|\bby\b)" % _CREDIT_ROLE, re.I)
# Unconditional junk: legal notices + structural artifacts from plain-text lyric
# scrapers (Genius "12 Contributors", "[Chorus]" tags, "…345Embed", URLs). These
# strings effectively never occur inside actual sung lines.
_LYRIC_NOISE = re.compile(
    r"版权声明|版權聲明|著作权|著作權|未经授权|未經授權|未经许可|未經許可|保留所有|"
    r"Copyright|All rights reserved|"
    r"^\[[^\]]*\]$|^\d+\s*Contributors?\b|Lyrics$|\d*Embed$|"
    r"Translations|Romaniz|EmbedShare|You might also like|https?://|www\.\w", re.I)
_LRC_TS = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\]")

# CJK ideographs + Japanese kana — these are highlighted one character at a time;
# Latin/other scripts are highlighted word-by-word.
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]")

def _units(text):
    """Split a line into karaoke units: each CJK char alone, Latin runs as words
    (keeping a trailing space so words stay separated when rendered)."""
    units, buf = [], ""
    for ch in text:
        if _CJK.match(ch):
            if buf.strip():
                units.append(buf)
            buf = ""
            units.append(ch)
        elif ch.isspace():
            if buf.strip():
                units.append(buf + " ")
            buf = ""
        else:
            buf += ch
    if buf.strip():
        units.append(buf)
    return units or [text]

def lyric_queries(title, artist=None):
    """From a messy YouTube title, build clean candidate search queries (most
    specific first). Strips channel cruft like '- Topic', VEVO, '(Official MV)',
    bracketed tags, and 'Artist - Song' prefixes that break lyric search.

    When `artist` is given, artist-qualified queries ('<song> <artist>') are tried
    first — many sites host several different songs under the same title, so the
    artist is what disambiguates them (e.g. 辞职信 by ChiliChill vs 陈红鲤)."""
    if not title:
        return []
    t = re.sub(r"\s*[-–|]\s*Topic\s*$", "", title, flags=re.I)        # 'X - Topic' channel
    t = re.sub(r"\b(VEVO|Official\s*(Music\s*Video|Video|Audio|MV|Lyric.*|Artist.*))\b", " ", t, flags=re.I)
    cands = []
    m = re.search(r"[【「《\[]([^】」》\]]+)[】」》\]]", title)               # bracketed text (song OR artist)
    brk = m.group(1).strip() if m else ""
    base = re.sub(r"[\(\[【「][^)\]】」]*[\)\]】」]", " ", t).strip()        # non-bracket remainder
    if " - " in base:                                                # 'Artist - Song'
        parts = [p.strip() for p in base.split(" - ") if p.strip()]
        if len(parts) >= 2:
            cands.append(parts[-1])                                  # song only
        cands.append(" ".join(parts))                               # artist + song
    else:
        if brk and base:
            cands.append(brk + " " + base)                          # combined (covers either order)
        if base:
            cands.append(base)                                      # song when bracket = artist
        if brk:
            cands.append(brk)                                       # song when bracket = song name
    cands.append(re.sub(r"[\(\[【「][^)\]】」]*[\)\]】」]", " ", title))     # original minus brackets
    cands.append(title)
    out, seen = [], set()
    for c in cands:
        c = re.sub(r"\s+", " ", c).strip(" -–|·")
        k = c.lower()
        if c and len(c) > 1 and k not in seen:
            seen.add(k); out.append(c)
    if artist:
        a = re.sub(r"\s*[-–|]\s*Topic\s*$", "", artist, flags=re.I)
        a = re.sub(r"\b(VEVO|Official)\b", " ", a, flags=re.I).strip(" -–|·")
        if a and a.lower() not in (title or "").lower():
            qualified = [f"{c} {a}" for c in out if a.lower() not in c.lower()]
            out = qualified + out                               # artist-qualified first
    return out

def _script_ok(text, lang):
    """Reject fetched lyrics whose script clashes with the detected audio
    language (e.g. a Russian translation matched to an English song)."""
    if not lang or lang == "auto":
        return True
    cyr = sum(1 for c in text if "Ѐ" <= c <= "ӿ")
    cjk = sum(1 for c in text if "぀" <= c <= "鿿")
    lat = sum(1 for c in text if "a" <= c.lower() <= "z")
    tot = cyr + cjk + lat or 1
    if lang in ("zh", "ja", "ko"):
        return cjk / tot > 0.15
    return cyr / tot < 0.2 and cjk / tot < 0.3      # Latin-script languages

def load_lyrics(title, lyrics_file, lang=None, artist=None):
    """Return (lines, synced) where lines is a list of (start|None, text).

    Tries an explicit file first, then syncedlyrics across cleaned query
    variants (artist-qualified first), skipping hits whose script doesn't match
    `lang`. `synced` is True if line-level timestamps are available.
    """
    raw = None
    if lyrics_file and os.path.exists(lyrics_file):
        raw = open(lyrics_file, encoding="utf-8").read()
        print(f"[2/5] Loaded lyrics from {lyrics_file}")
    elif title:
        try:
            import syncedlyrics
            for q in lyric_queries(title, artist):
                hit = syncedlyrics.search(q, artist=artist)
                if hit and len(hit) > 60:
                    if not _script_ok(hit, lang):
                        print(f"      lyrics for {q!r} look like the wrong language for '{lang}' — skipping")
                        continue
                    print(f"[2/5] Lyrics found for query: {q!r}")
                    raw = hit
                    break
                print(f"      no match for: {q!r}")
        except Exception as e:
            print(f"      lyric search failed: {e}")
    if not raw:
        print("[2/5] No lyrics — will transcribe (lower accuracy).")
        return [], False

    lines, synced = [], False
    for ln in raw.splitlines():
        stamps = _LRC_TS.findall(ln)
        text = _LRC_TS.sub("", ln).replace("\xa0", " ").strip()
        if not text or _META.search(text) or _LYRIC_NOISE.search(text) or text.startswith("《"):
            continue
        if stamps:
            synced = True
            m, s, c = stamps[0]
            start = int(m) * 60 + int(s) + (int((c or "0").ljust(2, "0")[:2]) / 100)
            lines.append((start, text))
        else:
            lines.append((None, text))
    if synced:
        lines = [(t, x) for (t, x) in lines if t is not None]
        lines.sort(key=lambda r: r[0])
    print(f"[2/5] {len(lines)} lyric lines ({'synced LRC' if synced else 'plain'})")
    return lines, synced

# ----------------------------- alignment ----------------------------------

def _nchars(text):
    return max(1, len([c for c in text if not c.isspace()]))

def detect_lang(model, audio16k):
    """Detect the spoken/sung language from the vocal track."""
    import whisper
    try:
        audio = whisper.load_audio(audio16k)
        mel = whisper.log_mel_spectrogram(
            whisper.pad_or_trim(audio), getattr(model.dims, "n_mels", 80)).to(model.device)
        _, probs = model.detect_language(mel)
        return max(probs, key=probs.get)
    except Exception:
        return "en"

def align(model, audio16k, lines, synced, lang, trusted=False):
    """Two-level alignment → a list of lines, each a list of (word, start, end).

    Stage 1 (line level) — locate each line's [start, end] span in the audio:
      • `trusted` subtitle (timings pulled from the YouTube video itself): the
        subtitle timestamps ARE the spans, so no global pass is needed.
      • lyrics (LRC/plain): one full-song forced alignment locates the lines,
        robust to web LRC timestamps that don't match this video.
      • no lyrics: transcription yields both the text and the spans (ASR).

    Stage 2 (word level) — slice each sentence's own audio (±pad) and force-align
    just that line, so word timing is decided from a short *local* clip instead of
    a single drift-prone whole-song pass.
    """
    import whisper
    audio = whisper.load_audio(audio16k)
    dur = len(audio) / 16000.0

    if lang in (None, "auto"):
        lang = detect_lang(model, audio16k)
        print(f"[3/5] Detected language: {lang}")

    if not lines:
        print("[3/5] Transcribing (no lyrics — weakest path)…")
        res = model.transcribe(audio, language=lang, regroup=True, verbose=False)
        spans = []
        for seg in res.segments:
            ws = [w for w in (seg.words or []) if w.word.strip()]
            if ws:
                spans.append((seg.text.strip(), ws[0].start, ws[-1].end))
        print(f"      local word alignment of {len(spans)} lines…")
        return _polish(_local_align(model, audio, spans, lang, dur), audio)

    texts = [t for _, t in lines]
    lrc_times = [t for t, _ in lines] if synced else None

    if trusted and lrc_times:
        print(f"[3/5] Trusting subtitle timings; local word alignment of {len(texts)} lines…")
        spans = _spans_from_times(texts, lrc_times, dur)
    else:
        print(f"[3/5] Global line alignment of {len(texts)} lines…")
        res = model.align(audio, "\n".join(texts), language=lang, original_split=True)
        repaired = _repair(_segments_to_lines(res.segments, texts), texts, lrc_times, dur)
        spans, prev_e = [], 0.0
        for i, rw in enumerate(repaired):
            if rw:
                s, e = rw[0][1], rw[-1][2]
            else:                                       # degenerate line → tuck after previous
                s, e = prev_e, prev_e + max(0.5, _nchars(texts[i]) * 0.3)
            spans.append((texts[i], s, e)); prev_e = e
        print(f"      local word alignment of {len(texts)} lines…")
    return _polish(_local_align(model, audio, spans, lang, dur), audio)

def _spans_from_times(texts, lrc_times, dur):
    """Line spans straight from synced (video-accurate) subtitle start times:
    each line runs to the next line's start; the last gets an estimated tail."""
    n = len(texts)
    out = []
    for i in range(n):
        s = lrc_times[i]
        e = lrc_times[i + 1] if i + 1 < n else min(dur, s + _nchars(texts[i]) * 0.4 + 1.5)
        out.append((texts[i], s, max(e, s + 0.3)))
    return out

def _local_align(model, audio, spans, lang, dur, sr=16000, pad=0.5):
    """Force-align each sentence inside its own padded slice, mapping word times
    back to absolute. A short clip gives the aligner local context only, so a
    mistake on one line can't drift the rest of the song."""
    out = []
    for text, s, e in spans:
        if s is None:
            out.append([]); continue
        e = e if (e is not None and e > s) else s + 1.0
        lo = max(0.0, s - pad)
        hi = min(dur, e + pad)
        clip = audio[int(lo * sr):int(hi * sr)]
        ws = []
        if len(clip) > int(0.2 * sr):
            try:
                r = model.align(clip, text, language=lang, original_split=True)
                ws = [(w.word, w.start + lo, w.end + lo)
                      for seg in r.segments for w in (seg.words or []) if w.word.strip()]
            except Exception:
                ws = []
        if not ws:                                      # alignment gave nothing → even split
            ws = _even_words(text, s, e)
        out.append(ws)
    return out

def _segments_to_lines(segs, texts):
    """1:1 line→segment when original_split holds, else regroup by char count."""
    if len(segs) == len(texts):
        return [[(w.word, w.start, w.end) for w in (s.words or []) if w.word.strip()] for s in segs]
    words = [(w.word, w.start, w.end) for s in segs for w in (s.words or []) if w.word.strip()]
    return _regroup_to_lines(words, texts)

def _regroup_to_lines(words, texts):
    out, wi = [], 0
    for text in texts:
        target, got, count = _nchars(text), [], 0
        while wi < len(words) and count < target:
            got.append(words[wi]); count += _nchars(words[wi][0]); wi += 1
        out.append(got)
    return out

def _repair(line_words, texts, lrc_times, dur):
    """Trust well-aligned lines; rebuild degenerate ones from a global LRC offset
    (if synced) or interpolation between confident neighbours."""
    n = len(texts)
    nchars = [_nchars(t) for t in texts]
    spans = []
    for ws in line_words:
        spans.append([min(w[1] for w in ws), max(w[2] for w in ws)] if ws else None)

    def confident(i):
        s = spans[i]
        if not s:
            return False
        dur_pc = (s[1] - s[0]) / nchars[i]
        return s[1] > s[0] + 0.15 and 0.05 <= dur_pc <= 1.6

    conf = [confident(i) for i in range(n)]

    # global LRC->audio offset from confident lines (median); handles a shifted intro
    offset = None
    if lrc_times:
        diffs = sorted(spans[i][0] - lrc_times[i] for i in range(n)
                       if conf[i] and lrc_times[i] is not None)
        if diffs:
            offset = diffs[len(diffs) // 2]
            # The model has aligned the lyrics to *this* audio only if the
            # confident anchors agree on a single constant offset. When they
            # scatter (the aligner drifted / locked onto a repeated section),
            # the median is meaningless — the synced LRC's own timestamps are
            # then the more reliable timeline, so trust them directly.
            inliers = sum(1 for d in diffs if abs(d - offset) <= 2.5)
            if len(diffs) >= 4 and inliers / len(diffs) < 0.6:
                print(f"      forced alignment unreliable "
                      f"({inliers}/{len(diffs)} anchors agree) — using raw LRC timestamps")
                offset, conf = 0.0, [False] * n
            else:
                print(f"      LRC↔audio offset ≈ {offset:+.2f}s ({len(diffs)} anchors)")

    starts = [spans[i][0] if conf[i] else
              (lrc_times[i] + offset if (offset is not None and lrc_times and lrc_times[i] is not None) else None)
              for i in range(n)]
    starts = _interp_starts(starts, nchars, dur)

    out, floor = [], 0.0
    n_fixed = sum(1 for c in conf if not c)
    for i in range(n):
        a = max(starts[i], floor)
        nxt = starts[i + 1] if i + 1 < n else dur
        if conf[i]:
            ws = _sanitize(line_words[i], a, min(spans[i][1], nxt) if nxt > a else spans[i][1])
        else:
            end = min(nxt, a + nchars[i] * 0.45 + 0.4)   # est. line length, capped by next line
            ws = _even_words(texts[i], a, max(end, a + 0.3))
        floor = ws[-1][2] if ws else floor
        out.append(ws)
    if n_fixed:
        print(f"      repaired {n_fixed}/{n} lines the model could not align")
    return out

def _interp_starts(starts, nchars, dur):
    """Fill None starts by char-proportional interpolation/extrapolation; monotonic."""
    n = len(starts)
    cum = [0] * (n + 1)
    for i in range(n):
        cum[i + 1] = cum[i] + nchars[i]
    known = [i for i in range(n) if starts[i] is not None]
    if not known:
        return [dur * cum[i] / cum[n] for i in range(n)]
    f = known[0]
    for i in range(f):                                    # leading lines
        starts[i] = starts[f] * (cum[i] / cum[f]) if cum[f] else starts[f]
    for k in range(len(known) - 1):                       # between anchors
        a, b = known[k], known[k + 1]
        span = max(1, cum[b] - cum[a])
        for i in range(a + 1, b):
            starts[i] = starts[a] + (starts[b] - starts[a]) * (cum[i] - cum[a]) / span
    last = known[-1]
    pace = 0.3
    if len(known) >= 2:
        a, b = known[-2], known[-1]
        pace = max(0.1, (starts[b] - starts[a]) / max(1, cum[b] - cum[a]))
    for i in range(last + 1, n):                          # trailing lines
        starts[i] = starts[last] + pace * (cum[i] - cum[last])
    for i in range(1, n):
        starts[i] = max(starts[i], starts[i - 1])
    return [min(max(0.0, s), dur) for s in starts]

def _even_words(text, start, end):
    units = _units(text)
    if not units:
        return []
    step = (end - start) / len(units)
    return [(u, start + i * step, start + (i + 1) * step) for i, u in enumerate(units)]

def _sanitize(words, lo, hi):
    """Clamp words into [lo, hi], monotonic, non-zero duration."""
    out, floor = [], lo
    span = max(hi - lo, 0.3)
    for txt, a, b in words:
        a = min(max(a, floor), lo + span)
        b = min(max(b, a + 0.05), hi if hi > a else a + 0.2)
        out.append((txt, a, b)); floor = b
    return out

def _group_by_segments(res):
    out = []
    for seg in res.segments:
        ws = [(w.word, w.start, w.end) for w in (seg.words or []) if w.word.strip()]
        if ws:
            out.append(_sanitize(ws, ws[0][1], ws[-1][2]))
    return out

# ----------------------- per-word timing polish ---------------------------
# Whisper's word boundaries within a line can be jittery on hard/effected
# vocals (near-zero "crammed" words, function words held >1s, big gaps). These
# passes fix only *clearly broken* lines, so well-aligned lines are untouched.

def _is_short_word(w):
    w = w.strip()
    return bool(w) and not _CJK.search(w) and len(w) <= 3

def _syl_weight(w):
    """Rough sung-duration weight: syllables for Latin, char count for CJK."""
    w = w.strip()
    if _CJK.search(w):
        return max(1, len(_CJK.findall(w)) + len(re.findall(r"[A-Za-z]+", w)))
    return max(1, len(re.findall(r"[aeiouyAEIOUY]+", w)))

def _smooth_line(ws):
    """If a line's word timing is clearly broken, redistribute only the LEADING
    words proportionally to syllable weight, across the span up to the last
    word's start. The final word keeps its own span, so a held final note (and
    its sharp cut-off, handled by _trim_trailing) is preserved rather than
    flattened into an even sweep."""
    n = len(ws)
    if n < 3:
        return ws
    durs = [b - a for _, a, b in ws]
    gaps = [ws[i + 1][1] - ws[i][2] for i in range(n - 1)]
    near_zero = sum(1 for d in durs if d < 0.05)
    # only the non-final words count toward the 'broken' decision so a long held
    # final note doesn't trip it on its own
    long_func = any(_is_short_word(w) and d > 0.7 for (w, _, _), d in zip(ws[:-1], durs[:-1]))
    big_gap = max(gaps[:-1]) if len(gaps) > 1 else 0.0
    if near_zero < 2 and not long_func and big_gap < 0.9:
        return ws                                   # looks fine — leave it alone
    body, last = ws[:-1], ws[-1]
    # redistribute up to the 2nd-to-last word's end, preserving any pause before
    # the final word (so the body isn't inflated to fill an instrumental gap)
    t0, tend = ws[0][1], ws[-2][2]
    span = tend - t0
    if span < 0.3 or not body:
        return ws
    weights = [_syl_weight(w) for w, _, _ in body]
    tot = sum(weights) or len(body)
    out, cur = [], t0
    for (w, _, _), wt in zip(body, weights):
        d = span * wt / tot
        out.append((w, cur, cur + d)); cur += d
    out.append(last)                                # keep the held final word as-is
    return out

def _min_dur(ws, lo=0.08):
    """Give every word at least `lo` seconds (kills 0.00s 'flash' words)."""
    out, floor = [], None
    for w, a, b in ws:
        if floor is not None and a < floor:
            a = floor
        if b < a + lo:
            b = a + lo
        out.append((w, a, b)); floor = b
    return out

def _fit_trailing(ws, audio, next_start, sr=16000):
    """Re-time the final word of a line onto the *held vocal note* itself.

    The aligner frequently mis-places a sustained final word: it labels the word
    late (leaving a long unsung gap before it) and/or lets its end run on toward
    the next line.  The audible truth is the dominant voiced run that follows the
    previous word — the held note.  We measure the vocal energy there and snap the
    final word onto that run, so the \\kf highlight covers the note exactly while
    it is sung and cuts off sharply when the voice stops, rather than sweeping
    late or bleeding into the next line.

    Ordinary short final words (no gap, not held, sane end) are left untouched."""
    if not ws:
        return ws
    import numpy as np
    w, a, b = ws[-1]
    prev_end = ws[-2][2] if len(ws) >= 2 else a
    lo = prev_end
    hi = min(prev_end + 10.0, len(audio) / sr)
    if next_start:
        hi = min(hi, next_start - 0.05)              # never bleed into the next line
    if hi - lo < 0.4:
        return ws
    seg = audio[int(lo * sr):int(hi * sr)]
    win, hop = int(0.03 * sr), int(0.015 * sr)
    nfr = max(1, (len(seg) - win) // hop)
    db = np.array([20 * np.log10(np.sqrt(np.mean(seg[i*hop:i*hop+win] ** 2)) + 1e-7) for i in range(nfr)])
    thr = max(db.max() - 22, -45)                    # relative silence floor
    voiced = db > thr
    # longest contiguous voiced run (merging dips < ~0.25s) = the held note
    merge = int(0.25 * sr / hop)
    runs, i = [], 0
    while i < nfr:
        if not voiced[i]:
            i += 1; continue
        j, gap, k = i, 0, i
        while k < nfr:
            if voiced[k]:
                j, gap = k, 0
            else:
                gap += 1
                if gap > merge:
                    break
            k += 1
        runs.append((i, j)); i = k + 1
    if not runs:
        return ws
    s_fr, e_fr = max(runs, key=lambda r: r[1] - r[0])
    t_of = lambda fr: lo + (fr * hop + win / 2) / sr
    # A held note that ends sharply must be followed by silence. If the voiced
    # run reaches the window edge (no vocal-stop observed — e.g. a legato stem
    # with instrumental bleed, or an instrumental outro), its true end is unknown,
    # so leave the aligner's timing alone rather than smear the word to the edge.
    if t_of(e_fr) > hi - 0.2:
        return ws
    new_a = max(prev_end, t_of(s_fr))
    new_b = min(hi, t_of(e_fr) + 0.10)               # small tail past last voiced frame
    if new_b - new_a < 0.3:
        return ws
    held = (new_b - new_a) > 1.2                      # a sustained note
    fake_gap = (a - prev_end) > 0.5                   # aligner left a gap before it
    overrun = (b - a) > 2.0                           # alignment ran long
    if held or fake_gap or overrun:
        ws[-1] = (w, new_a, max(new_a + 0.3, new_b))
    return ws

def _polish(lines, audio):
    out = []
    for i, ws in enumerate(lines):
        ws = _smooth_line(ws)
        ws = _min_dur(ws)
        nxt = next((lines[j][0][1] for j in range(i + 1, len(lines)) if lines[j]), None)
        ws = _fit_trailing(ws, audio, nxt)
        out.append(ws)
    return out

# ------------------------------- ASS --------------------------------------

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: L,{font},{size},&H0030A2FF,&H00F5F5F5,&H66101010,&H96000000,1,0,0,0,100,100,0,0,1,3.5,1.5,1,90,90,{mv_top},1
Style: R,{font},{size},&H0030A2FF,&H00F5F5F5,&H66101010,&H96000000,1,0,0,0,100,100,0,0,1,3.5,1.5,3,90,90,{mv_bot},1
Style: CD,{font},{size},&H00FF901E,&H00FF901E,&H66101010,&H96000000,1,0,0,0,100,100,0,0,1,3.5,1.5,1,90,90,{mv_cd},1
Style: Hint,{font},{hint_size},&H64F0F0F0,&H64F0F0F0,&HB4000000,&HB4000000,0,1,0,0,100,100,0,0,1,2,0,8,40,40,26,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

def _t(sec):
    sec = max(0.0, sec)
    h = int(sec // 3600); m = int(sec % 3600 // 60); s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100: s += 1; cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def build_ass(lines, ass_path, font="Microsoft YaHei", size=126, lead=3.0, note=None):
    """Two stacked lines at the bottom: earlier line bottom-left (upper), next
    line bottom-right (lower), alternating; per-word \\kf highlight + lead-in.
    `note`, if given, is shown dimmed at the top for the whole song (used to flag
    ASR-recognized lyrics when no official lyrics were found)."""
    mv_bot = 45
    mv_top = mv_bot + int(size * 1.5)          # upper line sits one line-height above
    mv_cd = mv_top + int(size * 1.05)          # countdown sits just above the upper lyric line
    hint_size = max(28, int(size * 0.42))
    slot_free = {"L": 0.0, "R": 0.0}
    last_sung = 0.0
    slot = "L"
    first = True
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ASS_HEADER.format(font=font, size=size, mv_top=mv_top, mv_bot=mv_bot, mv_cd=mv_cd, hint_size=hint_size))
        if note:
            f.write("Dialogue: 0,0:00:00.00,9:59:59.99,Hint,,0,0,0,,%s\n" % note)
        for words in lines:
            if not words:
                continue
            w0, wlast = words[0][1], words[-1][2]
            gap = w0 - last_sung

            # A real break before this line (song start, or a >4s instrumental gap)
            # starts a new "section": show a 3→2→1 blue countdown on its own line
            # above the lyrics, and put the line itself on the first (upper) line.
            is_cd = (first or gap > 4.0) and w0 >= 1.5
            first = False
            if is_cd:
                slot = "L"
            style = slot
            start = max(slot_free[style], w0 - lead)
            lead_in = w0 - start

            if is_cd:
                # one blue frame per second — three dots, then two, then one — ending
                # exactly as the line begins.
                for k, dots in enumerate(("● ● ●", "● ●", "●")):
                    cs, ce = max(0.0, w0 - (3 - k)), w0 - (2 - k)
                    if ce - cs > 0.05:
                        f.write(f"Dialogue: 0,{_t(cs)},{_t(ce)},CD,,0,0,0,,{dots}\n")

            text = ""
            if lead_in > 0.01:
                text += "{\\k%d}" % int(lead_in * 100)   # invisible lead-in hold

            cur = w0
            for word, a, b in words:
                g = a - cur
                if g > 0.02:
                    text += "{\\k%d}" % int(g * 100)
                # keep spacing (English word separation); guard ASS override chars
                w = word.replace("\n", " ").replace("{", "(").replace("}", ")")
                text += "{\\kf%d}%s" % (max(1, int((b - a) * 100)), w)
                cur = b
            last_sung = wlast
            slot_free[style] = wlast
            slot = "R" if slot == "L" else "L"
            f.write(f"Dialogue: 0,{_t(start)},{_t(wlast)},{style},,0,0,0,,{text}\n")
    print(f"[4/5] Wrote {ass_path}")

# ------------------------------- burn -------------------------------------

def _video_dims(path):
    """(width, height) of the first video stream, or (0, 0) if unknown."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True).stdout.strip()
        w, h = (int(x) for x in out.split("x")[:2])
        return w, h
    except Exception:
        return 0, 0

def burn(video, ass, out):
    print(f"[5/5] Burning subtitles -> {out}")
    w, h = _video_dims(video)
    # Render onto at least a 640p 16:9 canvas so the (1080p-authored) ASS stays large
    # and crisp even when the source is tiny (libass renders text at the output size).
    # Bigger 16:9 sources are kept as-is, capped at 1080p; odd aspect ratios get padded.
    th = min(1080, max(640, h or 640)); th -= th % 2
    tw = (th * 16) // 9; tw -= tw % 2
    esc = ass.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    vf = (f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
          f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,ass='{esc}'")
    print(f"      canvas {tw}x{th} (source {w or '?'}x{h or '?'})")
    run(["ffmpeg", "-y", "-i", video, "-vf", vf,
         "-c:a", "copy", "-loglevel", "error", out])

# ------------------------------- main -------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate a karaoke music video.")
    ap.add_argument("input", help="source video (or audio) file")
    ap.add_argument("--title", help="song title, used to fetch lyrics")
    ap.add_argument("--artist", help="artist/uploader, disambiguates same-title songs")
    ap.add_argument("--lyrics", help="lyrics file (.lrc or plain .txt); timings treated as untrusted")
    ap.add_argument("--subtitle", help="video-synced subtitle (.lrc) from the source — trust its line "
                                       "timings and only align words locally")
    ap.add_argument("--vocals", help="pre-extracted vocals wav (skip Spleeter)")
    ap.add_argument("--model", default="small", help="whisper model (tiny/base/small/medium)")
    ap.add_argument("--lang", default="auto", help="language code or 'auto' to detect")
    ap.add_argument("--ass", default="karaoke.ass")
    ap.add_argument("--out", help="output video (default: <input>.karaoke.mp4)")
    ap.add_argument("--font", default="Noto Sans CJK SC", help="renders CN/JP/KR + Latin")
    ap.add_argument("--size", type=int, default=126)   # 1.5x of the original 84
    ap.add_argument("--no-burn", action="store_true", help="only produce the ASS")
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args()

    workdir = args.workdir or tempfile.mkdtemp(prefix="kgen_")
    os.makedirs(workdir, exist_ok=True)
    if args.vocals:
        print(f"[1/5] Using provided vocals: {args.vocals}")
        audio16k = to_wav16k(args.vocals, os.path.join(workdir, "vocals16k.wav"))
    else:
        audio16k = extract_vocals(args.input, workdir)

    import stable_whisper
    model = stable_whisper.load_model(args.model)

    lang = args.lang
    if lang in (None, "auto"):
        lang = detect_lang(model, audio16k)
        print(f"[2/5] Detected language: {lang}")

    # A video-synced subtitle (e.g. fetched from the YouTube captions) is trusted
    # for its line timings → local word alignment only. Otherwise fetch lyrics and
    # validate their script against the detected language (rejects e.g. a Russian
    # translation matched to an English song), then global-then-local align.
    trusted = False
    if args.subtitle and os.path.exists(args.subtitle):
        lines, synced = load_lyrics(None, args.subtitle, lang)
        trusted = bool(lines and synced)
        if trusted:
            print(f"[2/5] Trusting subtitle line timings from {args.subtitle}")
    else:
        lines, synced = load_lyrics(args.title, args.lyrics, lang, args.artist)
    # no usable official lyrics -> fall back to vocal recognition (ASR); flag it.
    note = None if lines else "⚠ 无官方歌词，以下歌词由人声识别生成  ·  No official lyrics — auto-recognized from vocals"
    aligned = align(model, audio16k, lines, synced, lang, trusted=trusted)
    build_ass(aligned, args.ass, font=args.font, size=args.size, note=note)

    if not args.no_burn:
        out = args.out or (os.path.splitext(args.input)[0] + ".karaoke.mp4")
        burn(args.input, args.ass, out)
        print(f"Done -> {out}")
    else:
        print("Done (ASS only).")

if __name__ == "__main__":
    main()
