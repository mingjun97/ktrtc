"""Megalobiz (megalobiz.com) LRC provider"""

import re
from typing import List, Optional
from bs4 import SoupStrainer
from .base import LRCProvider
from ..utils import Lyrics, generate_bs4_soup


class Megalobiz(LRCProvider):
    """Megabolz provider class"""

    ROOT_URL = "https://www.megalobiz.com"
    SEARCH_ENDPOINT = ROOT_URL + "/search/all?qry={q}&searchButton.x=0&searchButton.y=0"

    def _candidates(self, query: str) -> List[dict]:
        url = self.SEARCH_ENDPOINT.format(q=query.replace(" ", "+"))
        a_tags_bound = SoupStrainer("a", href=lambda h: bool(h) and h.startswith("/lrc/maker/"))
        soup = generate_bs4_soup(self.session, url, parse_only=a_tags_bound)
        out = []
        for a in soup.find_all("a"):
            href = a.get("href")
            # the link text is "artist track ( lyrics ) [05:10.47]" — drop the cruft.
            # Artist and title aren't cleanly separable, so the whole label is used as
            # the title blob (base's fuzzy artist match still finds the artist in it).
            label = re.sub(r"\(.*?\)|\[.*?\]", " ", a.get_text())
            label = re.sub(r"\s+", " ", label).strip()
            if href and label:
                out.append({"title": label, "artist": "", "name": label, "href": href})
        return out

    def _fetch(self, candidate: dict) -> Optional[Lyrics]:
        href = candidate["href"]
        lrc_id = href.split(".")[-1]
        soup = generate_bs4_soup(self.session, self.ROOT_URL + href)
        div = soup.find("div", {"id": f"lrc_{lrc_id}_details"})
        if not div:
            return None
        lrc = Lyrics()
        lrc.add_unknown(div.get_text())
        return lrc
