from sqlalchemy import Column, Integer, String, MetaData, Table, select, func, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base
import threading
import os
import pinyin

MDB = 'db.sqlite'
media_prefix = "/media/"

Base = declarative_base()

class Song(Base):
   __tablename__ = 'VOD_song'
   ID = Column(Integer)
   SongID = Column(Integer, primary_key=True, autoincrement=True)
   SONGNAME = Column(String)
   SINGER = Column(String)
   SongTYPE2 = Column(Integer)
   WordCount = Column(Integer)
   Bihua = Column(Integer)
   spell = Column(String)
   track1 = Column(String)
   track2 = Column(String)
   volume1 = Column(Integer)
   volume2 = Column(Integer)
   volkey = Column(String)
   Brightness = Column(Integer)
   Contrast = Column(Integer)
   Saturation = Column(Integer)
   WeekCount = Column(Integer)
   MonthsCount = Column(Integer)
   ClickCount = Column(Integer)
   CDate = Column(Integer)
   FileMode = Column(String)
   FileSize = Column(Integer)
   FileSel = Column(String)
   FileName = Column(String)
   Version = Column(String)
   EDITION = Column(String)
   NewSong = Column(String)
   PubSong = Column(String)
   AudioFormat = Column(Integer)
   Special = Column(Integer)
   VCD_DVD = Column(String)
   USED = Column(Integer)
   SongTYPE = Column(Integer)
   SongLANG = Column(Integer)


engine = create_async_engine('sqlite+aiosqlite:///{0}'.format(MDB), echo=False)

# session = AsyncSession(engine, expire_on_commit=False)
metadata = MetaData()

song = Song.__table__

def preload(path):
   threading.Thread(target=os.system, args=(f'cat "{path}" > /dev/null', )).start()


def path_wrapper(path):
   ret = path.replace('\\', '/').replace('//Mac/ktv/', media_prefix)
   return ret

async def query(keyword="", singer="", page=0, per_page=10):
   async with engine.begin() as conn:
      stmt = select(
         song.c.SongID, song.c.SONGNAME, song.c.SINGER, song.c.FileName
      ).where(
         (song.c.SONGNAME + song.c.spell).like(f"%{keyword}%")
      ).where(
         song.c.SINGER.like(f"%{singer}%")
      )
      
      count = await conn.scalar(select(func.count()).select_from(stmt.subquery()))

      if keyword:
         stmt = stmt.order_by(song.c.WordCount)
      else:
         stmt = stmt.order_by(song.c.ClickCount.desc())

      stmt = stmt.offset(page * per_page).limit(per_page)
      result = await conn.execute(stmt)
      rows = result.fetchall()

      return count, list(map(list,rows))

async def get_singers():
   async with engine.begin() as conn:
      stmt = "SELECT SingerName FROM Singerinfo ORDER BY SingerName"
      result = await conn.execute(text(stmt))
      rows = result.fetchall()
      return [row[0] for row in rows]

async def get_song_by_id(song_id):
   async with engine.begin() as conn:
      stmt = select(song.c.FileName).where(song.c.SongID == song_id)
      result = await conn.execute(stmt)
      row = result.fetchone()
      return row[0] if row else None

async def increase_click_count(song_id):
   async with engine.begin() as conn:
      stmt = song.update().where(song.c.SongID == song_id).values(ClickCount=song.c.ClickCount + 1)
      await conn.execute(stmt)

async def add_song(song_name, singer, filename):
   async with engine.begin() as conn:
      spell = ""
      for ch in song_name:
         if ch in [" ", "　", ",", "(", "（", ")", "）", "!", "！"]:
            continue
         try:
            spell += pinyin.get(ch, format="strip", delimiter="")[0]
         except:
            if not str.is_punctuation(ch):
               spell += ch
      stmt = song.insert().values(SONGNAME=song_name, SINGER=singer, FileName=filename, spell=spell, WordCount=len(song_name))
      await conn.execute(stmt)

if __name__ == '__main__':
   import asyncio

   async def main():
      # await create_tables()
      print(await query(keyword="a", singer="周杰伦", page=0))
      # print(await query(keyword="", singer="周杰伦", page=0))
      print(path_wrapper(await get_song_by_id(184354)))

      # print(await get_singers())


   loop = asyncio.get_event_loop()

   loop.run_until_complete(main())
