import argparse
import asyncio
import json
import logging
import os
import ssl
import fractions
import gc
import aiofiles

from aiohttp import web

from aiortc.mediastreams import MediaStreamTrack, Frame
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaRelay, MediaStreamError

from lib import MediaPlayer, AudioStreamTrack

from av import VideoFrame, AudioFrame
from yt_agent import init as yt_init, add_task, get_work_list, uninit as yt_uninit, video_info
import time

from db import query, path_wrapper, get_song_by_id, preload, get_singers, increase_click_count

AUDIO_PTIME = 0.020  # 20ms audio packetization
VIDEO_CLOCK_RATE = 90000
VIDEO_PTIME = 1 / 30  # 30fps
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)

ROOT = os.path.dirname(__file__)

relay = None
webcam = None
singers  = []


class TrackManager():
    class VideoProducer(VideoStreamTrack):
        def __init__(self, parent) -> None:
            self._placeholder = VideoStreamTrack()
            self.parent = parent
            self._start = time.time()
            self._timestamp = 0

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            # print(self.parent._switch_at, self.parent._video_sync, self.parent._audio_sync)
            try:
                frame = await self.parent._video.recv()
            except MediaStreamError:
                if self.parent.queue:
                    # pass
                    asyncio.ensure_future(broadcast_queue())
                    await self.parent.skip()
                frame = await self._placeholder.recv()
            except Exception:
                frame = await self._placeholder.recv()
                self.parent._video_sync = time.time()
            frame.pts = pts
            frame.time_base = time_base
            return frame

        async def next_timestamp(self):
            self._timestamp += int(VIDEO_PTIME * VIDEO_CLOCK_RATE)
            wait = self._start + (self._timestamp / VIDEO_CLOCK_RATE) - time.time()
            await asyncio.sleep(wait)

            return self._timestamp, VIDEO_TIME_BASE


    class AudioProducer(AudioStreamTrack):

        def __init__(self, parent) -> None:
            self.parent = parent
            self._placeholder = AudioStreamTrack()
            self._start = time.time()
            self._timestamp = 0
            self.sample_rate = 48000
            self.use_vocal = True

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            try:
                frame = await self.parent._audio.recv()
                if self.parent._alt_audio:
                    if not self.use_vocal:
                        frame = await self.parent._alt_audio.recv()
                    else:
                        await self.parent._alt_audio.recv()
                self.sample_rate = frame.sample_rate
            except:
                self.parent._audio = None
                self.parent._audio_sync = time.time()
                frame = await self._placeholder.recv()

            frame.pts = pts
            frame.time_base = time_base
            return frame

        async def next_timestamp(self):
            samples = int(AUDIO_PTIME * self.sample_rate)

            self._timestamp += samples
            wait = self._start + (self._timestamp / self.sample_rate) - time.time()
            await asyncio.sleep(wait)

            return self._timestamp, fractions.Fraction(1, self.sample_rate)


    def __init__(self) -> None:
        try:
            self.queue = json.load(open('.saved.json', 'r'))
        except:
            self.queue = []
        self._video = None
        self._audio = None
        self._alt_audio = None
        self._media = None
        self._alt_media = None
        self.forcing = False
        self.channel = None

        if self.queue:
            asyncio.ensure_future(self._open_next())

        self.audio = self.AudioProducer(self)
        self.video = self.VideoProducer(self)


    async def _open_next(self):
        # try:
            asyncio.ensure_future(increase_click_count(self.queue[0][0]))
            if self._media:
                try:
                    self._media._stop(self._audio)
                    self._audio.stop()
                    self._media._stop(self._video)
                    self._video.stop()
                    if self._alt_media:
                        self._alt_media._stop(self._alt_audio)
                        self._alt_audio.stop()
                except:
                    pass

            gc.collect()
            path =  path_wrapper(await get_song_by_id(self.queue[0][0]))
            # await (await aiofiles.open(path, 'rb')).read()
            media = MediaPlayer(path)
            self._media = media
            self._video = media.video
            self._audio = media.audio
            self._audio_sync = 0
            self._video_sync = 0
            self._switch_at = time.time()
            try:
                media = MediaPlayer(path, audio=1)
                self._alt_media = media
                self._alt_audio = media.audio
            except:
                self._alt_audio = None
            await self._preload()
            if self.channel:
                self.channel.send(json.dumps({
                    "type": "info",
                    "data": self.queue
                }))

        # except:
            # self._video = None
            # self._audio = None
            # self._alt_audio = None
        # print("[!]open next: ", self.queue, self._video, self._audio, self._alt_audio)

    async def _preload(self):
        if len(self.queue) > 2:
            path =  path_wrapper(await get_song_by_id(self.queue[1][0]))
            preload(path)


    async def put(self, song):
        if song[0] in [s[0] for s in self.queue]:
            return # already in queue
        self.queue.append(song)
        await self._preload()
        preload(path_wrapper(await get_song_by_id(song[0])))

        if not self._video:
            await self._open_next()
        self.channel.send(json.dumps({
                "type": "info",
                "data": self.queue
            }))


        asyncio.ensure_future((await aiofiles.open('.saved.json', 'w')).write(json.dumps(self.queue)))

    async def skip(self, forcing=False):
        if self.forcing:
            return
        self.forcing = forcing
        if self.queue:
            self.queue.pop(0)
        if self.queue:
            await self._open_next()
        else:
            self._video = None
            self._audio = None
        asyncio.ensure_future((await aiofiles.open('.saved.json', 'w')).write(json.dumps(self.queue)))
        self.forcing = False
        self.channel.send(json.dumps({
            "type": "info",
            "data": self.queue
        }))

    async def replay(self):
        self.forcing = True
        await self._open_next()
        self.forcing = False

    def toggle_vocal(self):
        self.audio.use_vocal = not self.audio.use_vocal
        return self.audio.use_vocal

    async def top(self, song_id):
        for idx, song in enumerate(self.queue):
            if song[0] == song_id:
                song = self.queue.pop(idx)
                self.queue.insert(1, song)
                break
        asyncio.ensure_future((await aiofiles.open('.saved.json', 'w')).write(json.dumps(self.queue)))
        self.channel.send(json.dumps({
            "type": "info",
            "data": self.queue
        }))

    async def remove(self, song_id):
        for idx, song in enumerate(self.queue):
            if idx == 0:
                continue
            if song[0] == song_id:
                song = self.queue.pop(idx)
                break
        asyncio.ensure_future((await aiofiles.open('.saved.json', 'w')).write(json.dumps(self.queue)))
        self.channel.send(json.dumps({
            "type": "info",
            "data": self.queue
        }))

def create_local_tracks(play_from):
    global relay, webcam

    webcam = TrackManager()

    relay = MediaRelay()
    return relay.subscribe(webcam.audio), relay.subscribe(webcam.video)

async def operation(request):
    global relay, webcam
    params = await request.json()
    if not params.get('op'):
        return web.Response(content_type="application/json", text="{}")

    elif params['op'] == 'query':
        results = await query(keyword=params.get('keyword', ''),
              singer=params.get('singer', ''),
              page=int(params.get('page', 0))
              )
        return web.Response(content_type="application/json", text=json.dumps(results))

    elif params['op'] == 'skip':
        webcam.channel.send(json.dumps({
            "type": "op",
            "data": "skip"
        }))
        await webcam.skip(forcing=True)
        

    elif params['op'] == 'top':
        await webcam.top(params['id'])

    elif params['op'] == 'add':
        await webcam.put(params['song'])

    elif params['op'] == 'remove':
        await webcam.remove(params['id'])

    elif params['op'] == 'replay':
        webcam.channel.send(json.dumps({
            "type": "op",
            "data": "replay"
        }))
        await webcam.replay()


    asyncio.ensure_future(broadcast_queue())

    return web.Response(content_type="application/json", text="{}")

async def toggle_vocal(reuest):
    global webcam
    webcam.toggle_vocal()
    webcam.channel.send(json.dumps({
            "type": "op",
            "data": "vocal"
        }))
    return web.Response(content_type="text/html", text=str(webcam.audio.use_vocal))


async def get_list(request):
    global webcam
    return web.Response(content_type="application/json", text=json.dumps(webcam.queue))

async def index(request):
    content = open(os.path.join(ROOT, "index.html"), "r").read()
    return web.Response(content_type="text/html", text=content)

async def ret_singers(request):
    global singers
    if not singers:
        singers = await get_singers()
    return web.Response(content_type="application/json", text=json.dumps(singers))


async def javascript(request):
    content = open(os.path.join(ROOT, "client.js"), "r").read()
    return web.Response(content_type="application/javascript", text=content)


async def offer(request):
    global webcam
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        print("Connection state is %s" % pc.connectionState)
        if pc.connectionState == "failed":
            await pc.close()
            pcs.discard(pc)

    # open media source
    audio, video = create_local_tracks(args.play_from)

    await pc.setRemoteDescription(offer)
    for t in pc.getTransceivers():
        if t.kind == "audio" and audio:
            pc.addTrack(audio)
        elif t.kind == "video" and video:
            pc.addTrack(video)

    @pc.on("datachannel")
    def on_datachannel(channel):
        webcam.channel = channel


    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        ),
    )

async def console(request):
    content = open(os.path.join(ROOT, "console.html"), "r").read()
    return web.Response(content_type="text/html", text=content)


async def consolejs(request):
    content = open(os.path.join(ROOT, "console.js"), "r").read()
    return web.Response(content_type="application/javascript", text=content)

async def yt_page(request):
    content = open(os.path.join(ROOT, "yt.html"), "r").read()
    return web.Response(content_type="text/html", text=content)

async def get_yt_list(request):
    ret = get_work_list()
    return web.Response(content_type="application/json", text=json.dumps(ret))

async def add_yt_source(request):
    params = await request.json()
    add_task(params['url'], params['title'], params['singer'], params.get('caps', None))
    return web.Response(content_type="application/json", text=json.dumps("success"))

async def put_link(request):
    params = await request.json()
    return web.Response(content_type="application/json", text=json.dumps( await video_info(params['url'])))

pcs = set()
websockets = set()

async def websocket_handler(request):
    global websockets, webcam
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    websockets.add(ws)
    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            if msg.data == 'close':
                await ws.close()
            else:
                try:
                    await ws.send_json(webcam.queue)
                except:
                    pass
        elif msg.type == web.WSMsgType.ERROR:
            print('ws connection closed with exception %s' %
                  ws.exception())

    websockets.discard(ws)
    return ws

async def broadcast_queue():
    global websockets, webcam
    for ws in websockets:
        try:
            await ws.send_json(webcam.queue)
        except:
            pass

async def on_shutdown(app):
    # close peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()
    yt_uninit()
    os._exit(0)
    for ws in websockets:
        await ws.close(code=999, message='Server shutdown')
    websockets.clear()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WebRTC webcam demo")
    parser.add_argument("--cert-file", help="SSL certificate file (for HTTPS)")
    parser.add_argument("--key-file", help="SSL key file (for HTTPS)")
    parser.add_argument("--play-from", help="Read the media from a file and sent it."),
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port for HTTP server (default: 8080)"
    )
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if args.cert_file:
        ssl_context = ssl.SSLContext()
        ssl_context.load_cert_chain(args.cert_file, args.key_file)
    else:
        ssl_context = None

    yt_init()
    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/client.js", javascript)
    app.router.add_post("/offer", offer)
    app.router.add_post("/op", operation)
    app.router.add_get('/vocal', toggle_vocal)
    app.router.add_get('/list', get_list)
    app.router.add_get('/console', console)
    app.router.add_get('/singers', ret_singers)
    app.router.add_get('/console.js', consolejs)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/yt_list', get_yt_list)
    app.router.add_post('/add_yt_source', add_yt_source)
    app.router.add_get('/yt', yt_page)
    app.router.add_post('/yt_link', put_link)
    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context)