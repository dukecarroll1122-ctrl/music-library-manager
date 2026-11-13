"""Artwork matching: text normalisation (accents, punctuation, kana, romaji), edition classification, candidate
scoring with confidence levels, and the fallback query strategies. Pure functions, no network access.

Principle: a similar title is not a match. Titles must agree after normalisation, numbers (Vol. 1 / Persona 3)
must agree, the artist must agree, and anything that depends on transliteration, a different edition, or fan/
promotional artwork is capped below "Strong" so it is never approved in bulk.
"""
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

try:  # optional: romanises Japanese so ペルソナ3 can be compared with "Persona 3"
    import pykakasi
    _KAKASI = pykakasi.kakasi()
except Exception:  # not installed
    _KAKASI = None

EXACT, STRONG, POSSIBLE, WEAK, UNVERIFIED = ("Exact release match", "Strong match", "Possible match", "Weak match",
                                             "Unverified")
LEVELS = (EXACT, STRONG, POSSIBLE, WEAK, UNVERIFIED)
RANK = {c: i for i, c in enumerate(LEVELS)}
SAFE = (EXACT, STRONG)  # the only levels that "Approve All" may touch; weak/unverified are never automatic
LEGACY = {"High": STRONG, "Possible": POSSIBLE}  # values written by the first bulk-artwork version


def level(c):
    return c if c in RANK else LEGACY.get(c, UNVERIFIED)


def worse(a, b):
    return a if RANK[a] >= RANK[b] else b


# ---------------------------------------------------------------- normalisation
_JP = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")
OST_RE = re.compile(
    r"original\s+(?:video\s+)?(?:game\s+)?(?:sound\s?track|score|music)|(?:video\s+)?game\s+(?:sound\s?track|music)|"
    r"\bo\.?s\.?t\.?\b|sound\s?track|オリジナル[・･\s]*(?:ゲーム[・･\s]*)?サウンド[・･\s]*トラック|サウンドトラック|"
    r"オリジナル[・･\s]*スコア|ゲーム[・･\s]*ミュージック|original\s+score", re.I)
_TAIL = re.compile(r"(?:\s+(?:\d+(?:st|nd|rd|th)\s+anniversary|anniversary|deluxe|expanded|remastered|remaster|"
                   r"special|limited|complete|selected|edition|version|reissue|digital|japan|japanese))+\s*$", re.I)
_EDITION_HINT = re.compile(r"deluxe|expanded|remaster|anniversary|special|bonus|collector|limited|legacy|reissue|"
                           r"re-?release|edition|version|explicit|clean|mono|stereo|complete|selected|digital|"
                           r"single|\bep\b|\bost\b|soundtrack|japan|international|\d+(?:st|nd|rd|th)", re.I)


def has_japanese(s):
    return bool(_JP.search(s or ""))


def fold(s):
    """Lowercase, NFKC (full-width → normal), strip Latin accents, katakana → hiragana, '&' → 'and', drop
    punctuation/special characters. 'Beyoncé' == 'beyonce', 'ペルソナ' == 'ぺるそな', 'Wu‐Tang!' == 'wu tang'."""
    out = []
    for ch in unicodedata.normalize("NFKC", s or "").casefold():
        if "LATIN" in unicodedata.name(ch, ""):
            ch = "".join(c for c in unicodedata.normalize("NFD", ch) if not unicodedata.combining(c))
        elif "ァ" <= ch <= "ヶ":
            ch = chr(ord(ch) - 0x60)
        out.append(ch)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", " ", "".join(out).replace("&", " and "))).strip()


def romaji(s):
    if not _KAKASI or not has_japanese(s):
        return ""
    try:
        return fold(" ".join(x["hepburn"] for x in _KAKASI.convert(s)))
    except Exception:
        return ""


def split_edition(title):
    """'Thriller (25th Anniversary Edition)' -> ('Thriller', ['25th Anniversary Edition']);
    'AIZO - Single' -> ('AIZO', ['Single'])."""
    labels = []

    def grab(m):
        labels.append(m.group(1).strip())
        return " "
    t = re.sub(r"[\(\[（【]([^\)\]）】]*)[\)\]）】]", grab, title or "")
    m = re.search(r"\s[-–—:]\s+(.+)$", t)
    if m and _EDITION_HINT.search(m.group(1)):
        labels.append(m.group(1).strip())
        t = t[:m.start()]
    return re.sub(r"\s+", " ", t).strip(), [x for x in labels if x]


def strip_ost(s):
    return re.sub(r"\s+", " ", OST_RE.sub(" ", s or "")).strip()


def clean_raw(t):
    """Title without edition labels, soundtrack words and trailing edition words (original script kept)."""
    return _TAIL.sub("", strip_ost(split_edition(t)[0])).strip()


def clean_title(t):
    return fold(clean_raw(t)) or fold(split_edition(t)[0]) or fold(t)


def numbers(t):
    """Volume / game numbers that distinguish albums. Years, ordinals (20th) and 'disc N' are ignored."""
    t = re.sub(r"\d+\s*(?:st|nd|rd|th)\b|\b(?:19|20)\d\d\b|\b(?:disc|cd|disk)\s*\d+", " ", unicodedata.normalize(
        "NFKC", t or ""), flags=re.I)
    return {str(int(n)) for n in re.findall(r"\d+", t)}


def _sm(a, b):
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def title_sim(a, b):
    """(similarity 0..1, how). how: exact | base | fuzzy | translit. Different numbers cap the score hard."""
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return 0.0, "none"
    na, nb = numbers(a), numbers(b)
    cap = 0.55 if (na and nb and na != nb) else 0.78 if bool(na) != bool(nb) else 1.0
    if fa == fb:
        return min(1.0, cap), "exact"
    ca, cb = clean_title(a), clean_title(b)
    if ca and ca == cb:
        return min(0.97, cap), "base"
    best, how = min(0.84, max(_sm(fa, fb), _sm(ca, cb))), "fuzzy"  # never reaches "Possible" on its own
    if has_japanese(a) or has_japanese(b):
        ra, rb = romaji(clean_raw(a)) or ca, romaji(clean_raw(b)) or cb
        t = _sm(ra.replace(" ", ""), rb.replace(" ", ""))
        if t >= 0.8 and min(0.88, t) > best:
            best, how = min(0.88, t), "translit"
    return min(best, cap), how


def game_title_sim(game, cand_title):
    """The game's name appears in the candidate's title (the album needn't be named after the game)."""
    g, c = clean_title(game), clean_title(cand_title)
    if not g or not c:
        return 0.0, "none"
    if g == c:
        return 0.93, "base"
    if re.search(rf"(?:^| ){re.escape(g)}(?: |$)", c):
        return 0.74, "fuzzy"  # "persona 5" inside "persona 5 royal": likely a different game version
    s, how = title_sim(game, cand_title)
    return s, how


# ---------------------------------------------------------------- artists
_VARIOUS = re.compile(r"various|soundtrack|ost$|original cast|^va$|^v\.?a\.?$", re.I)


def split_artists(s):
    parts = re.split(r"\s*(?:,|&|\+|/|;|\band\b|\bx\b|\bwith\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bvs\.?|・)\s*",
                     s or "", flags=re.I)
    return [p.strip() for p in parts if p.strip()]


def is_various(s):
    return not s or bool(_VARIOUS.search(s.strip()))


def artist_variants(s):
    """Spelling variants of an artist for the fallback strategies."""
    out, seen = [], set()

    def add(x):
        x = (x or "").strip()
        if x and fold(x) not in seen:
            seen.add(fold(x))
            out.append(x)
    add(s)
    if is_various(s):
        return out
    parts = split_artists(s)
    if len(parts) > 1:
        add(parts[0])
    if ", " in (s or "") and len(parts) == 2:
        add(f"{parts[1]} {parts[0]}")
    add(romaji(s))
    add(re.sub(r"\s*[\(\[].*?[\)\]]", "", s or ""))
    add(re.sub(r"^the\s+", "", s or "", flags=re.I))
    return out


def artist_sim(wanted, names):
    """(similarity, how) between local artist names and a candidate's names (credit + sort names).
    wanted may hold several names (artist, album artist, composer, publisher)."""
    wanted = [w for w in wanted if w and not is_various(w)]
    names = [n for n in names if n]
    if not wanted:
        return 0.6, "unknown"  # nothing to verify against: neutral, never counts as confirmation
    if not names:
        return 0.0, "none"
    best, how = 0.0, "none"
    for w in wanted:
        wf, wparts = fold(w), {fold(p) for p in split_artists(w)}
        for n in names:
            nf, nparts = fold(n), {fold(p) for p in split_artists(n)}
            if wf == nf:
                return 1.0, "exact"
            if wparts & nparts and (wparts <= nparts or nparts <= wparts):
                s, h = 0.92, "part"  # "Jay-Z" within "Jay-Z & Alicia Keys"
            else:
                s, h = (0.9 * _sm(wf, nf)), "fuzzy"
                rw, rn = romaji(w) or wf, romaji(n) or nf
                if (has_japanese(w) or has_japanese(n)) and _sm(rw.replace(" ", ""), rn.replace(" ", "")) >= 0.85:
                    s, h = max(s, 0.88), "translit"
            if s > best:
                best, how = s, h
    return best, how


# ---------------------------------------------------------------- editions
def edition_of(title, secondary=(), note="", provider=""):
    """(kind, label). kind: original | remaster | deluxe | compilation | regional | alternate | promo | fan."""
    labels = split_edition(title)[1]
    low = f"{title} {note} {' '.join(secondary)}".casefold()
    label = ", ".join(labels)
    if provider == "fanart":
        return "fan", label or "Fan-uploaded artwork"
    if re.search(r"\bpromo(tional)?\b|\bsampler\b|\badvance\b|for promotional", low):
        return "promo", label or "Promotional"
    if "compilation" in low or re.search(r"best of|greatest hits|the very best|anthology|collection", low):
        return "compilation", label or "Compilation"
    if re.search(r"deluxe|expanded|collector|special edition|legacy edition|bonus|limited edition|box set", low):
        return "deluxe", label or "Deluxe"
    if re.search(r"remaster|reissue|re-?release|anniversary|\b(19|20)\d\d (edition|version)", low):
        return "remaster", label or "Remaster / reissue"
    if re.search(r"japan(ese)?\b|international|asia|korea|european|\buk\b edition|\bus\b edition", low):
        return "regional", label or "Regional release"
    if re.search(r"alternate|alt\.|remix|\blive\b|demo|\bversion\b", low):
        return "alternate", label or "Alternate version"
    return "original", label


# ---------------------------------------------------------------- scoring
def _norm_cat(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").casefold())


def _year(s):
    m = re.match(r"\s*(\d{4})", str(s or ""))
    return int(m.group(1)) if m else None


def judge(want, cand):
    """Score one candidate against what the user's files say. Returns (score 0..1, confidence, reasons[]);
    confidence is None when the title doesn't match well enough for the candidate to be worth showing."""
    reasons = []
    game = want.get("mode") == "game"
    if not (cand.get("title") or "").strip():
        return 0.0, UNVERIFIED, ["The source gave no title to verify"]
    targets = [t for t in (want.get("album"), want.get("soundtrack_title")) if t]
    ts, how, via_game = 0.0, "none", False
    for t in targets:
        s, h = title_sim(t, cand["title"])
        if s > ts:
            ts, how = s, h
    if game and want.get("game"):
        s, h = game_title_sim(want["game"], cand["title"])
        if s > ts:
            ts, how, via_game = s, h, True
            reasons.append("matched on the game's title, not the album title")
    if not targets and not (game and want.get("game")):
        return 0.0, UNVERIFIED, ["No album title in your files to verify against"]

    names = cand.get("names") or [cand.get("artist")]
    a_sim, a_how = artist_sim([want.get("artist"), want.get("albumartist"), want.get("composer"),
                               want.get("publisher")], names)
    if a_how == "unknown":
        reasons.append("No artist in your files to verify")
    if is_various(cand.get("artist")) and a_how != "unknown":
        a_sim = max(a_sim, 0.55)
    ys, wy, cy = 0.6, _year(want.get("year")), _year(cand.get("year"))
    if wy and cy:
        ys = 1.0 if wy == cy else 0.8 if abs(wy - cy) <= 1 else 0.4 if abs(wy - cy) <= 5 else 0.1
        if abs(wy - cy) > 1:
            reasons.append(f"Release year {cy} differs from your {wy}")
    n, wn = cand.get("tracks") or 0, want.get("n_tracks") or 0
    ns = 0.6 if not (n and wn) else max(0.0, 1 - abs(n - wn) / max(n, wn))
    if n and wn and abs(n - wn) > max(2, 0.5 * wn):
        reasons.append(f"{n} tracks vs your {wn}")
    score = round(0.5 * ts + 0.3 * a_sim + 0.1 * ns + 0.1 * ys, 3)

    ids = [i for i in (cand.get("release_id"),) if i]
    wanted_ids = {str(want.get(k)).lower() for k in ("mb_release_id",) if want.get(k)}
    wanted_rg = str(want.get("mb_rg_id") or "").lower()
    cat_match = bool(want.get("catalog") and cand.get("catalog")
                     and _norm_cat(want["catalog"]) == _norm_cat(cand["catalog"]))
    if (ids and str(ids[0]).lower() in wanted_ids) or (wanted_rg and str(cand.get("rg_id") or "").lower() == wanted_rg):
        conf = EXACT
        reasons.insert(0, "MusicBrainz ID in your files matches this release")
    elif cat_match and ts >= 0.6:
        conf = EXACT
        reasons.insert(0, f"Catalog number {cand['catalog']} matches")
    elif ts >= 0.93 and how in ("exact", "base") and a_sim >= 0.85:
        conf = STRONG
    elif ts >= 0.93 and how in ("exact", "base") and a_how == "unknown":
        # compilations / "Various Artists": the title alone can't prove it, but the track count or year can
        close = bool(n and wn and abs(n - wn) / max(n, wn) <= 0.15) or bool(wy and cy and wy == cy)
        conf = STRONG if ts >= 0.97 and close else POSSIBLE
        if conf == STRONG:
            reasons.append("Title and track count/year agree (no artist to compare)")
    elif ts >= 0.85 and (a_sim >= 0.6 or game):  # soundtrack artists are often credited in another script
        conf = POSSIBLE
    elif ts >= 0.7:
        conf = WEAK
    else:
        return score, None, reasons + [f"Title similarity too low ({ts:.0%})"]  # None: not a candidate at all

    kind = cand.get("kind") or "original"
    wk = want.get("edition_kind") or "original"
    if via_game:
        conf = worse(conf, POSSIBLE)  # the album's own title wasn't matched: a candidate to check, never automatic
    if how == "translit":
        conf = worse(conf, POSSIBLE)
        reasons.append("Matched through romanised/Japanese transliteration — please verify")
    if kind != "original" and kind != wk and conf in (EXACT, STRONG):
        if kind == "remaster":
            reasons.append("Remaster/reissue artwork — usually the same cover")
        else:
            conf = POSSIBLE
            reasons.append(f"{kind.title()} release — cover may differ from your edition")
    if kind in ("fan", "promo"):
        conf = worse(conf, POSSIBLE)
    if game and a_how != "unknown" and a_sim < 0.6 and conf == POSSIBLE:
        ost = bool(OST_RE.search(cand["title"]) or "soundtrack" in (cand.get("rtype") or "").casefold()
                   or "soundtrack" in " ".join(cand.get("names") or []).casefold())
        if not ost:  # "Persona 5" by an unrelated artist is a different record, not the game's soundtrack
            conf = WEAK
            reasons.append("Same name as the game, but not a soundtrack and a different artist")
    return score, conf, reasons


# ---------------------------------------------------------------- fallback strategies
@dataclass
class Sub:
    """One concrete search. Providers translate it into their own query syntax."""
    name: str
    artist: str = ""
    album: str = ""
    year: str = ""
    terms: str = ""     # extra keywords (soundtrack, composer, publisher…)
    region: str = ""    # ISO country: JP, US, GB…
    catalog: str = ""

    def key(self):
        # raw text, not folded: "MM..FOOD" and "MM FOOD" fold to the same words but are different queries
        return (self.artist.casefold(), self.album.casefold(), self.year, self.terms.casefold(), self.region,
                self.catalog)


def special_free(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", " ", unicodedata.normalize("NFKC", s or ""))).strip()


def strategies(want, max_n=12):
    """Ordered fallback searches. The orchestrator stops at the first one that yields a Strong/Exact match, so
    most albums cost a single query per provider."""
    album, artist = want.get("album") or "", want.get("artist") or want.get("albumartist") or ""
    game = want.get("mode") == "game"
    title = want.get("soundtrack_title") or album
    gname = want.get("game") or strip_ost(split_edition(album)[0])
    base = split_edition(title)[0]
    year, cat = str(want.get("year") or ""), want.get("catalog") or ""
    composer = want.get("composer") or ""
    out = []

    def add(name, **kw):
        s = Sub(name, **kw)
        if (s.album or s.artist or s.terms) and s.key() not in {o.key() for o in out}:
            out.append(s)
    if cat:
        add("catalog number", catalog=cat, album=base, artist=artist)
    add("exact artist + album", artist=artist, album=title, year="")
    if game:
        add("game title + composer/artist", artist=composer or artist, album=gname)
        add("game title + original soundtrack", album=f"{gname} Original Soundtrack")
    add("album without special characters", artist=artist, album=special_free(base))
    for v in artist_variants(artist)[1:4]:
        add(f"artist variation “{v}”", artist=v, album=base)
    if year:
        add("album + year", artist=artist, album=base, year=year)
    if not game and not OST_RE.search(album):
        add("album + soundtrack", artist=artist, album=base, terms="soundtrack")
    if game:
        add("game title + soundtrack", album=gname, terms="soundtrack")
        if want.get("publisher"):
            add("game title + publisher", album=gname, terms=want["publisher"])
    if has_japanese(base) or has_japanese(gname):
        r = romaji(gname if game else base)
        if r:
            add("romanised title", artist=artist if not has_japanese(artist) else "", album=r)
    elif game:
        add("Japanese edition", album=f"{gname} オリジナル・サウンドトラック", region="JP")
    add("album only", album=base)
    for region in ("JP", "US", "GB"):
        add(f"regional edition ({region})", artist=artist, album=base, region=region)
    return out[:max_n]
