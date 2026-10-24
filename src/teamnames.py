"""Team-name normalisation & matching.

Different sources use different names (football-data.co.uk: "Man United",
The Odds API: "Manchester United", etc.). We normalise aggressively, keep a
hand-maintained alias table, and fall back to fuzzy matching within a league.
Unmatched names are reported, never silently guessed.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

_STRIP_TOKENS = {
    "fc", "afc", "cf", "sc", "ac", "as", "ud", "rcd", "rc", "cd", "bk",
    "fk", "sk", "nk", "ogc", "ssc", "sv", "vfl", "vfb", "tsv", "fsv",
    "1", "1899", "1904", "1907", "1909", "1913", "04", "05", "the",
    "club", "de", "futbol", "calcio", "football", "united", "city",  # kept only if needed below
}

# canonical -> list of accepted variants (all pre-normalised by us)
ALIASES = {
    "manchester united": ["man united", "man utd", "manchester utd"],
    "manchester city": ["man city"],
    "tottenham hotspur": ["tottenham", "spurs", "tottenham hotspurs"],
    "wolverhampton wanderers": ["wolves", "wolverhampton"],
    "newcastle united": ["newcastle", "newcastle utd"],
    "west ham united": ["west ham"],
    "brighton and hove albion": ["brighton", "brighton hove albion"],
    "nottingham forest": ["nott'm forest", "nottm forest"],
    "sheffield united": ["sheff utd", "sheffield utd"],
    "sheffield wednesday": ["sheff wed", "sheffield weds"],
    "leeds united": ["leeds"],
    "leicester city": ["leicester"],
    "ipswich town": ["ipswich"],
    "southampton": ["southampton", "soton"],
    "bournemouth": ["af", "afc bournemouth"],
    "paris saint germain": ["paris sg", "psg", "paris saint-germain", "paris fc"],
    "borussia monchengladbach": ["m'gladbach", "gladbach", "monchengladbach", "borussia m'gladbach"],
    "bayern munich": ["bayern munchen", "bayern", "fc bayern", "fc bayern munchen"],
    "borussia dortmund": ["dortmund"],
    "bayer leverkusen": ["leverkusen", "bayer 04 leverkusen"],
    "rb leipzig": ["leipzig", "rasenballsport leipzig", "red bull leipzig"],
    "stuttgart": ["vfb stuttgart"],
    "union berlin": ["1 fc union berlin"],
    "fc koln": ["cologne", "1 fc koln", "koln", "fc koeln"],
    "st pauli": ["fc st pauli"],
    "inter milan": ["inter", "internazionale", "fc internazionale milano"],
    "ac milan": ["milan"],
    "napoli": ["ssc napoli"],
    "roma": ["as roma", "roma 1907"],
    "lazio": ["ss lazio"],
    "atalanta": ["atalanta bc"],
    "fiorentina": ["acf fiorentina"],
    "hellas verona": ["verona"],
    "como": ["como 1907"],
    "atletico madrid": ["atletico", "atlético madrid", "atletico de madrid", "atl madrid"],
    "athletic club": ["athletic bilbao", "ath bilbao"],
    "real sociedad": ["sociedad"],
    "real betis": ["betis"],
    "celta vigo": ["celta"],
    "sevilla": ["sevilla fc"],
    "girona": ["girona fc"],
    "alaves": ["deportivo alaves"],
    "espanyol": ["rcd espanyol"],
    "valencia": ["valencia cf"],
    "villarreal": ["villarreal cf"],
    "psv eindhoven": ["psv"],
    "feyenoord": ["feyenoord rotterdam"],
    "ajax": ["afc ajax", "ajax amsterdam"],
    "az alkmaar": ["az"],
    "fc twente": ["twente"],
    "sporting cp": ["sporting", "sporting lisbon", "sporting clube de portugal"],
    "benfica": ["sl benfica"],
    "porto": ["fc porto"],
    "celtic": ["celtic fc"],
    "rangers": ["rangers fc"],
    "club brugge": ["club brugge kv", "club bruges"],
    "anderlecht": ["rsc anderlecht"],
    "genk": ["krc genk"],
    "galatasaray": ["galatasaray istanbul"],
    "fenerbahce": ["fenerbahce istanbul"],
    "besiktas": ["besiktas jk"],
    "olympiacos": ["olympiacos piraeus"],
    "panathinaikos": ["panathinaikos fc"],
    "cardiff city": ["cardiff"],
    "swansea city": ["swansea"],
    "hull city": ["hull"],
    "norwich city": ["norwich"],
    "stoke city": ["stoke"],
    "west bromwich albion": ["west brom"],
    "blackburn rovers": ["blackburn"],
    "preston north end": ["preston"],
    "middlesbrough": ["middlesbrough", "boro"],
    "queens park rangers": ["qpr"],
    "derby county": ["derby"],
    "coventry city": ["coventry"],
    "sunderland": ["sunderland afc"],
}


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[&]'?s?\b", " and ", s)  # '&' -> 'and'
    s = re.sub(r"['\.\-()]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [t for t in s.split() if t not in {"fc", "afc", "cf", "sc", "ac", "as",
                                              "ud", "rcd", "rc", "cd", "bk", "fk",
                                              "sk", "nk", "ogc", "ssc", "sv", "tsv",
                                              "fsv", "the", "1"}]
    return " ".join(toks)


# build reverse alias map on normalised keys
_REV = {}
for canon, variants in ALIASES.items():
    _REV[_norm(canon)] = canon
    for v in variants:
        _REV[_norm(v)] = canon


def canonical(name: str) -> str:
    """Canonical display name: alias if known else normalised string."""
    n = _norm(name)
    return _REV.get(n, n)


class LeagueMatcher:
    """Match raw names to the canonical team pool of one league."""

    def __init__(self, teams: list[str]):
        # pool of canonical names built from historical data
        self.pool = sorted({canonical(t) for t in teams})
        self._map = {p: p for p in self.pool}

    def match(self, raw: str, cutoff: float = 0.72) -> str | None:
        c = canonical(raw)
        if c in self._map:
            return c
        close = difflib.get_close_matches(c, self.pool, n=1, cutoff=cutoff)
        return close[0] if close else None
