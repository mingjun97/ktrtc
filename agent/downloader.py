from pytube import YouTube
import os
from html import unescape
import math, time
from youtube_transcript_api import YouTubeTranscriptApi
import re
import regex

OUTPUT_DIR = '/media/downloads'

def float_to_srt_time_format(d: float) -> str:
    """Convert decimal durations into proper srt format.

    :rtype: str
    :returns:
        SubRip Subtitle (str) formatted time duration.

    float_to_srt_time_format(3.89) -> '00:00:03,890'
    """
    fraction, whole = math.modf(d)
    time_fmt = time.strftime("%H:%M:%S,", time.gmtime(whole))
    ms = f"{fraction:.3f}".replace("0.", "")
    return time_fmt + ms

def youtube_url_validation(url):
    youtube_regex = (
        r'(https?://)?(www\.)?'
        '(youtube|youtu|youtube-nocookie)\.(com|be)/'
        '(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})')

    youtube_regex_match = re.match(youtube_regex, url)
    if youtube_regex_match:
        return youtube_regex_match

    return youtube_regex_match

def caption_to_srt( captions) -> str:
        """Convert xml caption tracks to "SubRip Subtitle (srt)".

        :param str xml_captions:
            XML formatted caption tracks.
        """
        segments = []
        captions.sort(key=lambda x: x.get('start', 0.0))
        last_start = max(captions[0].get('start', 0.0) - 3.0, 0.0)
        last_end = 0.0
        i = 0
        for caption in captions:
            text = caption.get('text', '')
            duration = caption.get('duration', 0.0)
            start = caption.get('start', 0.0)
            end = start + duration
            if start - last_end > 3.0:
                sequence_number = i + 1  
                for countdown in range(3, 0, -1):
                    line = "{seq}\n{start} --> {end}\n{text}\n".format(
                        seq=sequence_number,
                        start=float_to_srt_time_format(start - countdown),
                        end=float_to_srt_time_format(start - countdown + 1.0),
                        text="O" * countdown + "-" * (3 - countdown),
                    )
                    i += 1
                    segments.append(line)
            sequence_number = i + 1 
            line = "{seq}\n{start} --> {end}\n{text}\n".format(
                seq=sequence_number,
                start=float_to_srt_time_format(max(last_start, start - 3.0)),
                end=float_to_srt_time_format(end),
                text=text,
            )
            last_start = start
            last_end = end
            i += 1
            segments.append(line)
        return "\n".join(segments).strip()

class Task:
    def __init__(self, link, title, singer, caps = None):
        self.link = link
        self.caps = caps
        self._stage = 0
        self.title = title
        self.singer = singer
        self.path = f'{OUTPUT_DIR}/{self.title}-{self.singer}(YTB).mp4'
        self.cb = None

    @property
    def stage(self):
        return self._stage
    
    def get_stage(self):
        return self._stage

    @stage.setter
    def stage(self, val):
        self._stage = val
        print("Stage: ", val)
        if self.cb:
            self.cb()
    
    def on_progress(self, cb):
        self.cb = cb
        return cb

    def get_tuple(self):
        return (self.link, self.title, self.singer, self.stage, self.path)

    def run(self):
        # start donwloading
        cleanup()
        self.stage += 1
        downloaded = False
        for i in range(3):
            try:
                yt = YouTube(self.link)
                video = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
                audio = yt.streams.filter(only_audio=True).first()
                audio.download(filename='./audio.mp4')
                video.download(filename='./video.mp4')
                downloaded = True
            except:
                pass
        if not downloaded:
            self.stage = -1
            return False

        # process subtitles
        self.stage += 1
        if self.caps:
            try:
                m = youtube_url_validation(self.link)
                src = YouTubeTranscriptApi.get_transcript(m[6], languages=[self.caps])
                open('sub.srt','w').write(caption_to_srt(src))
            except:
                self.caps = None

        # split the audio
        self.stage += 1
        ret = os.system("LD_PRELOAD='/usr/local/lib/python3.8/dist-packages/scikit_learn.libs/libgomp-d22c30c5.so.1.0.0' spleeter separate -p spleeter:2stems -o output audio.mp4")
        if ret != 0:
            self.stage = -1
            return False
        
        # compose the video
        self.stage += 1
        command = 'ffmpeg -i video.mp4 -i audio.mp4 -i output/audio/accompaniment.wav -c:a aac -map 1:a -map 2:a -map 0:v'
        if self.caps:
            command += ' -vf subtitles=sub.srt'
        else:
            command += ' -c:v copy'

        command += ' output.mp4'
        ret = os.system(command)

        if ret != 0:
            self.stage = -1
            return False
    
        # move the file
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        os.system(f'mv output.mp4 "{self.path}"')
        self.stage += 10000
        cleanup()
        return True

def get_available_captions(link):
    m = youtube_url_validation(link)
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(m[6])
        ret = {
            'original': [],
            'generated': [],
        }
        for transcript in transcript_list:
            if transcript.is_generated:
                ret['generated'].append((transcript.language, transcript.language_code))
            else:
                ret['original'].append((transcript.language, transcript.language_code))

        return ret
    except:
        return None

def get_video_info(link):
    m = youtube_url_validation(link)
    try:
        yt = YouTube(link)
        return {
            'title': yt.title,
            'singer': yt.author,
            'duration': yt.length,
            'thumbnail': yt.thumbnail_url,
            'captions': get_available_captions(link),
            'suggestions': [i for i in regex.split(r"[\p{P}|\p{Z}|\p{N}|\p{S}|\s]+", yt.title + " " + yt.author) if i != '']
        }
    except:
        return None

def cleanup():
    os.system('rm -rf audio.mp4 video.mp4 output output.mp4 sub.srt')


if __name__ == "__main__":
    # yt = YouTube("https://www.youtube.com/watch?v=1vQ7b1gEfdM")
    # print(get_available_captions("1vQ7b1gEfdM"))
    
    cleanup()
    # src = YouTubeTranscriptApi.get_transcript("1vQ7b1gEfdM", languages=['zh-TW'])
    # open('sub.srt','w').write(caption_to_srt(src))
    t = Task("https://www.youtube.com/watch?v=1vQ7b1gEfdM", caps='zh-TW')
    # t.run()