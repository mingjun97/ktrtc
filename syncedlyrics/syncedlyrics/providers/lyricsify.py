"""Lyricsify (lyricsify.com) LRC provider"""

from typing import List, Optional
from bs4 import SoupStrainer
from .base import LRCProvider
from ..utils import Lyrics, generate_bs4_soup


class Lyricsify(LRCProvider):
    """Lyricsify LRC provider class.

    Lyricsify sits behind Cloudflare's anti-bot system, so a plain
    ``requests`` session is rejected. We swap the session for a
    ``cloudscraper`` scraper, which transparently solves the JS challenge.
    """

    ROOT_URL = "https://www.lyricsify.com"
    SEARCH_ENDPOINT = ROOT_URL + "/search?q="

    def __init__(self) -> None:
        super().__init__()
        self.parser = "html.parser"
        # Bypass Cloudflare by replacing the session with a cloudscraper one.
        # Falls back to the default session if cloudscraper is unavailable.
        try:
            import cloudscraper

            self.session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "darwin", "mobile": False}
            )
        except Exception as e:
            self.logger.warning(f"cloudscraper unavailable, Cloudflare may block: {e}")

    def _candidates(self, query: str) -> List[dict]:
        url = self.SEARCH_ENDPOINT + query.replace(" ", "+")
        a_tags_bound = SoupStrainer("a", href=lambda h: bool(h) and h.startswith("/lyric/"))
        soup = generate_bs4_soup(self.session, url, parse_only=a_tags_bound)
        out = []
        for a in soup.find_all("a"):
            href = a.get("href")
            # link text is "Artist - Song"; keep the whole label as the title blob
            label = a.get_text(strip=True)
            if href and label:
                out.append({"title": label, "artist": "", "name": label, "href": href})
        return out

    def _fetch(self, candidate: dict) -> Optional[Lyrics]:
        href = candidate["href"]
        lrc_id = href.split(".")[-1]
        soup = generate_bs4_soup(self.session, self.ROOT_URL + href)
        div = soup.find("div", {"id": f"lyrics_{lrc_id}_details"})
        if not div:
            return None
        lrc = Lyrics()
        lrc.add_unknown(div.get_text())
        return lrc
