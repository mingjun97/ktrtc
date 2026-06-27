#!/usr/bin/env python3
"""Remote karaoke worker — a kgen-powered port of ``agent/poller.py``.

Long-polls the KORTC server's ``/poll_task`` endpoint for YouTube karaoke jobs,
renders each one with ``kgen.py`` (Spleeter vocal extraction → lyric lookup &
audio-driven alignment → ASS karaoke burn), and uploads the finished video back
via ``/submit_task``.

Protocol (unchanged from the original worker, so this is a drop-in replacement):
  • GET  /poll_task   → ``[stage, uuid, url, title, singer, caps]`` (long-polls;
                        204 when there's no work). The server marks the task
                        ``stage = 1`` (dispatched) when it hands it out.
  • POST /submit_task → multipart ``{uuid, file_content}`` on success.
  • Errors are reported *implicitly*: a job we never submit stays ``stage = 1``,
    and the server flips it to ``-1`` (→ "Failed" in the console) on the next
    poll. So after every job — success or failure — we poll again promptly.

Like the original, the output carries **two audio tracks** so the player's
原唱/伴奏 (vocal/accompaniment) toggle keeps working:
  track 0 = original audio (with vocals), track 1 = accompaniment + faint vocal
guide. We build track 1 from the stems Spleeter already produced inside kgen's
workdir, so there's no second separation pass.

Environment:
  KWORKER_SERVER    server base URL            (default https://kortc.lyric.today)
  KWORKER_KGEN      path to kgen.py            (default ./kgen.py next to this file)
  KWORKER_WORKDIR   scratch directory          (default /tmp/kworker)
  KWORKER_INSECURE  "1" to skip TLS verify     (self-signed dev servers)
  KWORKER_GUIDE_VOL accompaniment vocal guide  (default 0.08; 0 = full instrumental)
  KWORKER_MODEL     whisper model for kgen     (e.g. large-v3; default = kgen's own)
"""

import os
import re
import sys
import time
import shutil
import tempfile
import logging
import subprocess

import requests
from requests_toolbelt.multipart import encoder
from pytubefix import YouTube

logging.basicConfig(level=logging.INFO,
                    format="[kworker] %(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kworker")

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.environ.get("KWORKER_SERVER", "https://kortc.lyric.today").rstrip("/")
KGEN = os.environ.get("KWORKER_KGEN", os.path.join(HERE, "kgen.py"))
WORKDIR = os.environ.get("KWORKER_WORKDIR", "/tmp/kworker")
VERIFY = os.environ.get("KWORKER_INSECURE", "") not in ("1", "true", "True", "yes")
GUIDE_VOL = os.environ.get("KWORKER_GUIDE_VOL", "0.08")
MODEL = os.environ.get("KWORKER_MODEL", "")          # whisper model; "" → kgen's default

_YT_ID = re.compile(r"(?:v=|/embed/|/v/|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")


def _video_id(url):
    m = _YT_ID.search(url or "")
    return m.group(1) if m else None


def poll_task():
    """Block until the server hands out a job; returns the work list."""
    while True:
        try:
            r = requests.get(f"{SERVER}/poll_task", timeout=90, verify=VERIFY)
            if r.status_code == 200:
                data = r.json()
                if data:                      # non-empty → a real job
                    return data
            # 204 / empty body → no work right now, poll again
        except Exception as e:
            log.warning("poll failed (%s); retrying", e)
            time.sleep(3)
        time.sleep(1)


def download(url, dest):
    """Download a progressive (video+audio) mp4 for the link."""
    yt = YouTube(url)
    stream = (yt.streams.filter(progressive=True, file_extension="mp4")
              .order_by("resolution").desc().first())
    if stream is None:
        raise RuntimeError("no progressive mp4 stream available")
    stream.download(output_path=os.path.dirname(dest), filename=os.path.basename(dest))
    return dest


def caption_lrc(url, caps, dest):
    """Write the user-chosen YouTube caption track as a .lrc for ``kgen --lyrics``.

    Returns the path, or None if unavailable (kgen then looks lyrics up itself).
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        vid = _video_id(url)
        if not vid:
            return None
        cues = YouTubeTranscriptApi.get_transcript(vid, languages=[caps])
        lines = []
        for c in cues:
            txt = (c.get("text") or "").replace("\n", " ").strip()
            if not txt:
                continue
            mm, ss = divmod(float(c.get("start", 0.0)), 60)
            lines.append(f"[{int(mm):02d}:{ss:05.2f}]{txt}")
        if not lines:
            return None
        with open(dest, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return dest
    except Exception as e:
        log.warning("caption fetch failed (%s); falling back to lyric lookup", e)
        return None


def add_accompaniment(karaoke, workdir, dest):
    """Mux a 2nd audio track (accompaniment + faint vocal guide) into the burned
    karaoke video, reusing the Spleeter stems kgen left in ``workdir``.

    Falls back to the single-track karaoke file if the stems aren't present.
    """
    acc = os.path.join(workdir, "accompaniment.wav")
    voc = os.path.join(workdir, "vocals.wav")
    if not (os.path.exists(acc) and os.path.exists(voc)):
        log.info("stems not found in workdir — keeping single audio track")
        return karaoke
    mixed = os.path.join(workdir, "mixed.wav")
    subprocess.run([
        "ffmpeg", "-y", "-i", acc, "-i", voc, "-filter_complex",
        f"[0:a]volume=1.0[a0];[1:a]volume={GUIDE_VOL}[a1];[a0][a1]amix=inputs=2:duration=longest[out]",
        "-map", "[out]", "-c:a", "pcm_s16le", "-loglevel", "error", mixed,
    ], check=True)
    subprocess.run([
        "ffmpeg", "-y", "-i", karaoke, "-i", mixed,
        "-map", "0:v:0", "-map", "0:a:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac",
        "-metadata:s:a:0", "title=Vocal", "-metadata:s:a:1", "title=Accompaniment",
        "-loglevel", "error", dest,
    ], check=True)
    return dest


def submit_task(uuid, path):
    """Upload the finished video to the server (multipart: uuid + file)."""
    with open(path, "rb") as f:
        data = encoder.MultipartEncoder(fields={
            "uuid": uuid,
            "file_content": ("output.mp4", f, "video/mp4"),
        })
        r = requests.post(f"{SERVER}/submit_task", data=data,
                          headers={"Content-Type": data.content_type}, verify=VERIFY)
    r.raise_for_status()
    log.info("submitted %s (HTTP %s)", uuid, r.status_code)


def process(job, work):
    """Render one job end-to-end and submit it. Raises on any failure."""
    uuid, url, title, singer = job[1], job[2], job[3], job[4]
    caps = job[5] if len(job) > 5 else None
    log.info("job %s — %r by %r%s", uuid, title, singer, f" (caps={caps})" if caps else "")

    inp = os.path.join(work, "input.mp4")
    karaoke = os.path.join(work, "karaoke.mp4")
    kgdir = os.path.join(work, "kg")
    download(url, inp)

    cmd = [sys.executable, KGEN, inp, "--out", karaoke,
           "--ass", os.path.join(work, "karaoke.ass"), "--workdir", kgdir]
    if MODEL:
        cmd += ["--model", MODEL]
    if title:
        cmd += ["--title", title]
    if singer:
        cmd += ["--artist", singer]
    # a chosen YouTube caption is a *video-synced* subtitle → kgen trusts its line
    # timings and only word-aligns locally
    lrc = caption_lrc(url, caps, os.path.join(work, "caps.lrc")) if caps else None
    if lrc:
        cmd += ["--subtitle", lrc]

    log.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)                    # non-zero exit → job fails
    if not os.path.exists(karaoke):
        raise RuntimeError("kgen produced no output file")

    final = add_accompaniment(karaoke, kgdir, os.path.join(work, "final.mp4"))
    submit_task(uuid, final)


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    if not os.path.exists(KGEN):
        log.error("kgen.py not found at %s (set KWORKER_KGEN)", KGEN)
        sys.exit(1)
    log.info("worker online → %s  (kgen=%s, verify=%s)", SERVER, KGEN, VERIFY)
    while True:
        job = poll_task()
        uuid = job[1] if len(job) > 1 else "?"
        work = tempfile.mkdtemp(prefix="job_", dir=WORKDIR)
        try:
            process(job, work)
        except subprocess.CalledProcessError as e:
            log.error("job %s: a subprocess failed (exit %s); server will mark it Failed on next poll",
                      uuid, e.returncode)
        except Exception as e:
            log.exception("job %s failed (%s); server will mark it Failed on next poll", uuid, e)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        time.sleep(1)                                   # re-poll → flush success/failure state


if __name__ == "__main__":
    main()
