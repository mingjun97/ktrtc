"""Lrclib (lrclib.net) LRC provider"""

from typing import List, Optional
from .base import LRCProvider
from ..utils import Lyrics


class Lrclib(LRCProvider):
    """Lrclib LRC provider class"""

    ROOT_URL = "https://lrclib.net"
    API_ENDPOINT = ROOT_URL + "/api"
    SEARCH_ENDPOINT = API_ENDPOINT + "/search"
    LRC_ENDPOINT = API_ENDPOINT + "/get/"

    def get_lrc_by_id(self, track_id: str) -> Optional[Lyrics]:
        url = self.LRC_ENDPOINT + track_id
        r = self.session.get(url)
        if not r.ok:
            return None
        track = r.json()
        lrc = Lyrics()
        lrc.synced = track.get("syncedLyrics")
        lrc.unsynced = track.get("plainLyrics")
        return lrc

    def _candidates(self, query: str) -> List[dict]:
        r = self.session.get(self.SEARCH_ENDPOINT, params={"q": query})
        if not r.ok:
            return []
        return [{"id": str(t["id"]), "title": t.get("trackName", ""),
                 "artist": t.get("artistName", "")} for t in (r.json() or [])]

    def _fetch(self, candidate: dict) -> Optional[Lyrics]:
        return self.get_lrc_by_id(candidate["id"])
