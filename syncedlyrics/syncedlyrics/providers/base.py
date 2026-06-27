import re
import requests
from typing import List, Optional
import logging

from rapidfuzz import fuzz

from ..utils import Lyrics


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


class TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (2, 10))
        return super().request(method, url, **kwargs)


class LRCProvider:
    """
    Base class for all of the synced (LRC format) lyrics providers.

    Providers opt into the shared **aggregate-by-title + fuzzy-match-artist**
    selection by implementing two hooks:

    - ``_candidates(query)`` → a list of ``{"title", "artist", ...}`` dicts (plus
      whatever the provider needs to fetch each one — an ``id``, ``href``, ``url``…).
    - ``_fetch(candidate)`` → the ``Lyrics`` for a chosen candidate.

    ``get_lrc`` then searches broadly by the song *title*, keeps the candidates whose
    title matches, fuzzy-matches the *artist*, and fetches the best fit.
    """

    def __init__(self) -> None:
        self.session = TimeoutSession()

        # Logging setup
        formatter = logging.Formatter("[%(name)s] %(message)s")
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.addHandler(handler)

    def __str__(self) -> str:
        return self.__class__.__name__

    # --- per-provider hooks (override these) -------------------------------

    def _candidates(self, query: str) -> List[dict]:
        """Return search-result candidates as ``{"title", "artist", <ref>}`` dicts."""
        raise NotImplementedError

    def _fetch(self, candidate: dict) -> Optional[Lyrics]:
        """Return the lyrics for a chosen candidate."""
        raise NotImplementedError

    # --- shared selection heuristic ----------------------------------------

    @staticmethod
    def _clean_artist(artist: Optional[str]) -> str:
        a = artist or ""
        a = re.sub(r"\s*[-–|]\s*Topic\s*$", "", a, flags=re.I)   # YouTube "X - Topic" channel
        a = re.sub(r"\b(VEVO|Official)\b", " ", a, flags=re.I)
        return a.strip()

    @staticmethod
    def _cand_key(c: dict):
        return (_norm(c.get("title")), _norm(c.get("artist")),
                str(c.get("id") or c.get("href") or c.get("url") or ""))

    @staticmethod
    def _artist_field(c: dict) -> str:
        # prefer a dedicated artist; fall back to the whole label/title blob for
        # scrapers that can't cleanly split artist from title
        return c.get("artist") or c.get("name") or c.get("title") or ""

    def _select(self, search_term: str, artist: Optional[str] = None,
                title_floor: int = 55, min_title: int = 45, max_cands: int = 40) -> Optional[Lyrics]:
        artist = self._clean_artist(artist)
        # song title = the term with a trailing artist stripped, so the search casts
        # a wide net over every recording of the song
        title = search_term
        if artist:
            stripped = re.sub(r"\s*" + re.escape(artist) + r"\s*$", "", search_term, flags=re.I).strip()
            title = stripped or search_term

        # Aggregate candidates, deduped. The *full term* is searched first because
        # smart APIs rank a targeted "<title> <artist>" query well; we only broaden
        # to the bare title (to gather every recording of the song, which matters for
        # dumb substring searches) when there's no strong artist hit yet.
        queries, seen_q = [], set()
        toks = (title or search_term).split()
        for q in (search_term, title, toks[0] if toks else ""):
            q = (q or "").strip()
            if q and q.lower() not in seen_q:
                seen_q.add(q.lower()); queries.append(q)
        cands, seen = [], set()
        for q in queries:
            try:
                rows = self._candidates(q) or []
            except Exception as e:
                self.logger.debug(f"candidate search failed for {q!r}: {e}")
                rows = []
            for c in rows:
                k = self._cand_key(c)
                if k not in seen:
                    seen.add(k); cands.append(c)
            if not cands:
                continue                         # nothing yet → try the next (broader) query
            if len(cands) >= max_cands:
                break
            if not artist:
                break                            # no artist to disambiguate → targeted hit is enough
            best_as = max(fuzz.token_set_ratio(_norm(artist), _norm(self._artist_field(c))) for c in cands)
            if best_as >= 85:
                break                            # already a confident artist match
        if not cands:
            return None

        # keep candidates whose title is a reasonable match for the song title
        titled = [c for c in cands
                  if fuzz.token_set_ratio(_norm(c.get("title")), _norm(title)) >= title_floor] or cands

        if artist:
            best = max(titled, key=lambda c: fuzz.token_set_ratio(_norm(artist), _norm(self._artist_field(c))))
            if fuzz.token_set_ratio(_norm(artist), _norm(self._artist_field(best))) < 50:
                # The requested artist isn't matched here. Abstain *only* if several
                # different artists share this title (a real collision we can't
                # resolve) so the chain can fall through to a provider that has the
                # right recording. If the title is essentially unique, trust it —
                # the artist metadata is just off (e.g. a YouTube uploader name).
                strong = [c for c in titled
                          if fuzz.token_set_ratio(_norm(c.get("title")), _norm(title)) >= 80]
                distinct = {_norm(c.get("artist")) for c in strong if _norm(c.get("artist"))}
                if len(distinct) >= 2:
                    return None
                best = max(titled, key=lambda c: fuzz.token_set_ratio(_norm(c.get("title")), _norm(title)))
        else:
            best = max(titled, key=lambda c: fuzz.token_set_ratio(
                _norm(f"{c.get('title','')} {c.get('artist','')}"), _norm(search_term)))

        if fuzz.token_set_ratio(_norm(best.get("title")), _norm(title)) < min_title:
            return None
        return self._fetch(best)

    def get_lrc_by_id(self, track_id: str) -> Optional[Lyrics]:
        """
        Returns the synced lyrics of the song in [LRC](https://en.wikipedia.org/wiki/LRC_(file_format)) format if found.

        ### Arguments
        - track_id: The ID of the track defined in the provider database. e.g. Spotify/Deezer track ID
        """
        raise NotImplementedError

    def get_lrc(self, search_term: str, artist: Optional[str] = None) -> Optional[Lyrics]:
        """
        Returns the synced lyrics of the song in [LRC](https://en.wikipedia.org/wiki/LRC_(file_format)) format if found.

        Default implementation = aggregate by title + fuzzy-match artist over the
        provider's ``_candidates``/``_fetch`` hooks. ``artist`` is optional and only
        improves disambiguation between same-title songs.
        """
        return self._select(search_term, artist)
