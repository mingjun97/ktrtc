"""NetEase (music.163.com) china-based provider"""

from typing import List, Optional
from .base import LRCProvider
from ..utils import Lyrics


class NetEase(LRCProvider):
    """NetEase provider class"""

    API_ENDPOINT_METADATA = "https://music.163.com/api/search/pc"
    API_ENDPOINT_LYRICS = "https://music.163.com/api/song/lyric"

    def __init__(self) -> None:
        super().__init__()
        self.session.headers["cookie"] = (
            "NMTID=00OAVK3xqDG726ITU6jopU6jF2yMk0AAAGCO8l1BA; JSESSIONID-WYYY=8KQo11YK2GZP45RMlz8Kn80vHZ9%2FGvwzRKQXXy0iQoFKycWdBlQjbfT0MJrFa6hwRfmpfBYKeHliUPH287JC3hNW99WQjrh9b9RmKT%2Fg1Exc2VwHZcsqi7ITxQgfEiee50po28x5xTTZXKoP%2FRMctN2jpDeg57kdZrXz%2FD%2FWghb%5C4DuZ%3A1659124633932; _iuqxldmzr_=32; _ntes_nnid=0db6667097883aa9596ecfe7f188c3ec,1659122833973; _ntes_nuid=0db6667097883aa9596ecfe7f188c3ec; WNMCID=xygast.1659122837568.01.0; WEVNSM=1.0.0; WM_NI=CwbjWAFbcIzPX3dsLP%2F52VB%2Bxr572gmqAYwvN9KU5X5f1nRzBYl0SNf%2BV9FTmmYZy%2FoJLADaZS0Q8TrKfNSBNOt0HLB8rRJh9DsvMOT7%2BCGCQLbvlWAcJBJeXb1P8yZ3RHA%3D; WM_NIKE=9ca17ae2e6ffcda170e2e6ee90c65b85ae87b9aa5483ef8ab3d14a939e9a83c459959caeadce47e991fbaee82af0fea7c3b92a81a9ae8bd64b86beadaaf95c9cedac94cf5cedebfeb7c121bcaefbd8b16dafaf8fbaf67e8ee785b6b854f7baff8fd1728287a4d1d246a6f59adac560afb397bbfc25ad9684a2c76b9a8d00b2bb60b295aaafd24a8e91bcd1cb4882e8beb3c964fb9cbd97d04598e9e5a4c6499394ae97ef5d83bd86a3c96f9cbeffb1bb739aed9ea9c437e2a3; WM_TID=AAkRFnl03RdABEBEQFOBWHCPOeMra4IL; playerid=94262567"
        )

    def _candidates(self, query: str) -> List[dict]:
        params = {"limit": 10, "type": 1, "offset": 0, "s": query}
        response = self.session.get(self.API_ENDPOINT_METADATA, params=params)
        songs = response.json().get("result", {}).get("songs") or []
        # carry cookies/referer forward for the lyric fetch
        self.session.cookies.update(response.cookies)
        self.session.headers.update({"referer": response.url})
        return [{"id": s["id"], "title": s.get("name", ""),
                 "artist": (s.get("artists") or [{}])[0].get("name", "")} for s in songs]

    def _fetch(self, candidate: dict) -> Optional[Lyrics]:
        return self.get_lrc_by_id(candidate["id"])

    def get_lrc_by_id(self, track_id: str) -> Optional[Lyrics]:
        params = {"id": track_id, "lv": 1}
        response = self.session.get(self.API_ENDPOINT_LYRICS, params=params)
        lrc = Lyrics()
        lrc.add_unknown(response.json().get("lrc", {}).get("lyric"))
        return lrc
