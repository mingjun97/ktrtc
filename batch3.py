#!/usr/bin/env python3
"""Karaoke for an explicit list of YouTube IDs (passed as args)."""
import sys, os, re, subprocess
from pytubefix import YouTube

IDS = sys.argv[1:] or ["GA-OkMm1AJA", "S7qTs3MIuPU", "F64yFFnZfkI"]
OUT = "/workspace/workspace/kout"
os.makedirs(OUT, exist_ok=True)
os.makedirs("/tmp/yt", exist_ok=True)

def clean_query(title, author):
    m = re.search(r"[【「《\[]([^】」》\]]+)[】」》\]]", title)
    song = m.group(1) if m else re.sub(
        r"(official|music\s*video|lyric\s*video|\bMV\b|\(.*?\)|\[.*?\])", " ", title, flags=re.I)
    return re.sub(r"\s+", " ", (song + " " + (author or "")).strip())

for i, vid in enumerate(IDS, 1):
    url = f"https://www.youtube.com/watch?v={vid}"
    print(f"\n===== [{i}/{len(IDS)}] {url} =====", flush=True)
    try:
        yt = YouTube(url)
        title, author = yt.title, yt.author
        print("TITLE :", title, flush=True)
        print("AUTHOR:", author, flush=True)
        mp4 = f"/tmp/yt/{vid}.mp4"
        if not os.path.exists(mp4):
            st = yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").last()
            st.download(output_path="/tmp/yt", filename=f"{vid}.mp4")
        out = f"{OUT}/{vid}.karaoke.mp4"
        r = subprocess.run([sys.executable, "kgen.py", mp4, "--title", title, "--artist", author or "",
                            "--out", out, "--ass", f"/tmp/{vid}.ass", "--workdir", f"/tmp/kw_{vid}"])
        print(f"[{i}] exit={r.returncode} -> {out if r.returncode == 0 else 'FAILED'}", flush=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[{i}] FAILED: {e}", flush=True)

print("\nDONE3.", flush=True)
