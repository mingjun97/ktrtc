from multiprocessing import Manager, Process
from multiprocessing.managers import BaseManager
from time import sleep
from agent.downloader import Task, get_video_info
from db import add_song
import asyncio


BaseManager.register('Task', Task)
basemanager = BaseManager()
basemanager.start()
manager = Manager()
work_list = manager.list()
terminate = manager.Event()

def worker():
    index = 0
    while not terminate.is_set():
        if index >= len(work_list):
            sleep(0.1)
            continue
        t = work_list[index]
        t.run()
        _, title, singer, stage, path = t.get_tuple()
        if stage < 10000:
            index += 1
            continue

        loop = asyncio.get_event_loop()
        loop.run_until_complete(add_song(title + "(YTB)", singer, path))
        index += 1
    
def init():
    p = Process(target=worker)
    p.start()
    return p

def uninit():
    terminate.set()

def add_task(link, title, singer, caps = None):
    work_list.append(basemanager.Task(link, title, singer, caps))

def get_work_list():
    ret = []
    for i in work_list:
        ret.append(i.get_tuple()[1:4])
    return ret

async def video_info(link):
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, get_video_info, link)
    return info

if __name__ == "__main__":
    init()
    add_task("https://www.youtube.com/watch?v=1vU7XqToZso", "Pacific Rim OST Soundtrack", "MAIN THEME by Ramin Djawadi")
    add_task("https://www.youtube.com/watch?v=h0o8EtwCDaQ", "Celebrity", "IU")
    add_task("https://www.youtube.com/watch?v=HEYOsR1DlWE", "Through the Night", "IU")
    add_task("https://www.youtube.com/watch?v=rZHC1zMiWiU", "Good Day", "IU")

    while True:
        sleep(3)
        print(get_work_list())
        if work_list[-1].get_stage() > 10000:
            break
    terminate.set()