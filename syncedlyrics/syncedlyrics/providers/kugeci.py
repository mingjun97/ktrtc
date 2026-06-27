"""Kugeci (kugeci.com) LRC provider"""

from typing import List, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup, FeatureNotFound
from .base import LRCProvider
from ..utils import Lyrics


class Kugeci(LRCProvider):
    """Kugeci LRC provider — a Chinese lyrics site.

    Its search is a plain substring match and several different songs routinely
    share a title (e.g. 辞职信 by ChiliChill vs 陈红鲤), so it relies entirely on the
    base provider's aggregate-by-title + fuzzy-match-artist selection.
    """

    ROOT_URL = "https://www.kugeci.com"
    SEARCH_ENDPOINT = ROOT_URL + "/search?q={q}"
    LRC_ENDPOINT = ROOT_URL + "/download/lrc/{id}"

    def _candidates(self, query: str) -> List[dict]:
        if not query.strip():
            return []
        r = self.session.get(self.SEARCH_ENDPOINT.format(q=quote(query)))
        try:
            soup = BeautifulSoup(r.text, features="lxml")
        except FeatureNotFound:
            soup = BeautifulSoup(r.text, features="html.parser")
        rows = []
        for tr in soup.select("tbody tr"):
            links = tr.find_all("a")
            # the title is the `/song/<id>` link that actually has text
            # (each row also has a second, icon-only `/song/` link)
            song_a = next(
                (a for a in links if "/song/" in a.get("href", "") and a.get_text(strip=True)),
                None,
            )
            if not song_a:
                continue
            singer_a = next((a for a in links if "/singer/" in a.get("href", "")), None)
            rows.append({
                "id": song_a["href"].rstrip("/").split("/song/")[-1],
                "title": song_a.get_text(strip=True),
                "artist": singer_a.get_text(strip=True) if singer_a else "",
            })
        return rows

    def _fetch(self, candidate: dict) -> Optional[Lyrics]:
        return self.get_lrc_by_id(candidate["id"])

    def get_lrc_by_id(self, track_id: str) -> Optional[Lyrics]:
        r = self.session.get(self.LRC_ENDPOINT.format(id=track_id))
        if r.status_code != 200 or not r.text.strip():
            return None
        lrc = Lyrics()
        lrc.add_unknown(r.text)
        return lrc
