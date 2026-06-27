"""Genius (genius.com) provider API"""

from typing import List, Optional
from .base import LRCProvider
from ..utils import Lyrics, generate_bs4_soup


class Genius(LRCProvider):
    """Genius provider class"""

    SEARCH_ENDPOINT = "https://genius.com/api/search/multi?per_page=5&q="
    COOKIES = {"obuid": "e3ee67e0-7df9-4181-8324-d977c6dc9250"}

    def _candidates(self, query: str) -> List[dict]:
        r = self.session.get(self.SEARCH_ENDPOINT, params={"q": query, "per_page": 5},
                             cookies=self.COOKIES)
        if not r.ok:
            return []
        sections = r.json().get("response", {}).get("sections", [])
        # the "songs" section carries the track hits
        hits = next((s["hits"] for s in sections if s.get("type") == "song"), [])
        if not hits and len(sections) > 1:
            hits = sections[1].get("hits", [])
        out = []
        for h in hits:
            res = h.get("result", {})
            out.append({"title": res.get("title", ""),
                        "artist": (res.get("primary_artist") or {}).get("name", ""),
                        "url": res.get("url", "")})
        return out

    def _fetch(self, candidate: dict) -> Optional[Lyrics]:
        if not candidate.get("url"):
            return None
        soup = generate_bs4_soup(self.session, candidate["url"])
        els = soup.find_all("div", attrs={"data-lyrics-container": True})
        if not els:
            return None
        lrc_str = ""
        for el in els:
            lrc_str += el.get_text(separator="\n", strip=True).replace("\n[", "\n\n[")
        lrc = Lyrics()
        lrc.unsynced = lrc_str
        return lrc
