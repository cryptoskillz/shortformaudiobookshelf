"""Look up book details from the same public sources Audiobookshelf uses.

Files that carry no DESCRIPTION tag have nothing to show, so the description,
narrator, series and cover have to come from somewhere else. Three providers,
none of which needs an API key:

    audible   api.audible.com catalog search — the best source for audiobooks:
              narrator, runtime and a proper blurb.
    itunes    itunes.apple.com search, media=audiobook — good fallback.
    google    Google Books — aggressively rate limited without a key, so it is
              offered but never relied on.

Nothing here is applied automatically. A search returns candidates, and the
result only sticks when someone picks one, because a confidently wrong blurb on
the wrong book is worse than a blank field.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "ShortlistAudio/1.0 (+personal audiobook player)"
TIMEOUT = 12
PROVIDERS = ("audible", "itunes", "google")

_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<\s*(?:br|/p|/div)\s*/?\s*>", re.IGNORECASE)
_BLANK_LINES = re.compile(r"\n{3,}")


class LookupError_(Exception):
    """A provider could not be reached or returned nonsense."""


def clean_text(raw):
    """Provider blurbs arrive as HTML fragments; keep the paragraphs, drop the markup."""
    if not raw:
        return ""
    text = _BREAK.sub("\n", str(raw))
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise LookupError_("rate limited — try again in a minute, or use another source")
        raise LookupError_(f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        raise LookupError_(f"could not reach the service ({exc.reason})")
    except (ValueError, TimeoutError) as exc:
        raise LookupError_(str(exc) or "unreadable response")


def _candidate(**fields):
    base = {
        "provider": "", "id": "", "title": "", "subtitle": "", "authors": [],
        "narrators": [], "description": "", "year": "", "publisher": "",
        "series": "", "seriesPart": "", "genres": [], "runtimeMinutes": 0,
        "coverUrl": "", "link": "",
    }
    base.update(fields)
    return base


# ------------------------------------------------------------------ providers

def search_audible(title, author="", limit=5, region="com"):
    query = urllib.parse.urlencode({
        "title": title,
        "author": author,
        "num_results": max(1, min(limit, 10)),
        "products_sort_by": "Relevance",
        "response_groups": ",".join([
            "product_desc", "contributors", "media", "product_attrs",
            "product_extended_attrs", "series", "category_ladders",
        ]),
    })
    data = _get(f"https://api.audible.{region}/1.0/catalog/products?{query}")

    results = []
    for product in data.get("products", []):
        images = product.get("product_images") or {}
        cover = images.get("500") or images.get("500") or (list(images.values())[0] if images else "")
        series = (product.get("series") or [{}])[0]
        ladders = product.get("category_ladders") or []
        genres = []
        for ladder in ladders:
            for rung in ladder.get("ladder", []):
                if rung.get("name") and rung["name"] not in genres:
                    genres.append(rung["name"])
        results.append(_candidate(
            provider="audible",
            id=product.get("asin", ""),
            title=product.get("title", ""),
            subtitle=product.get("subtitle", ""),
            authors=[a.get("name", "") for a in product.get("authors", [])],
            narrators=[n.get("name", "") for n in product.get("narrators", [])],
            description=clean_text(product.get("merchandising_summary")
                                   or product.get("publisher_summary")),
            year=str(product.get("release_date", ""))[:4],
            publisher=product.get("publisher_name", ""),
            series=series.get("title", ""),
            seriesPart=str(series.get("sequence", "") or ""),
            genres=genres[:4],
            runtimeMinutes=int(product.get("runtime_length_min") or 0),
            coverUrl=cover,
            link=f"https://www.audible.{region}/pd/{product.get('asin', '')}" if product.get("asin") else "",
        ))
    return results


def search_itunes(title, author="", limit=5):
    query = urllib.parse.urlencode({
        "term": f"{title} {author}".strip(),
        "media": "audiobook",
        "limit": max(1, min(limit, 10)),
    })
    data = _get(f"https://itunes.apple.com/search?{query}")

    results = []
    for item in data.get("results", []):
        artwork = item.get("artworkUrl100") or ""
        results.append(_candidate(
            provider="itunes",
            id=str(item.get("collectionId", "")),
            title=item.get("collectionName", ""),
            authors=[item.get("artistName", "")] if item.get("artistName") else [],
            description=clean_text(item.get("description")),
            year=str(item.get("releaseDate", ""))[:4],
            genres=[item["primaryGenreName"]] if item.get("primaryGenreName") else [],
            # The 100px thumbnail URL upsizes by substitution.
            coverUrl=artwork.replace("100x100bb", "600x600bb"),
            link=item.get("collectionViewUrl", ""),
        ))
    return results


def search_google(title, author="", limit=5):
    terms = f'intitle:"{title}"'
    if author:
        terms += f' inauthor:"{author}"'
    query = urllib.parse.urlencode({"q": terms, "maxResults": max(1, min(limit, 10)),
                                    "printType": "books"})
    data = _get(f"https://www.googleapis.com/books/v1/volumes?{query}")

    results = []
    for item in data.get("items", []):
        info = item.get("volumeInfo", {})
        images = info.get("imageLinks") or {}
        cover = images.get("thumbnail") or images.get("smallThumbnail") or ""
        results.append(_candidate(
            provider="google",
            id=item.get("id", ""),
            title=info.get("title", ""),
            subtitle=info.get("subtitle", ""),
            authors=info.get("authors", []) or [],
            description=clean_text(info.get("description")),
            year=str(info.get("publishedDate", ""))[:4],
            publisher=info.get("publisher", ""),
            genres=(info.get("categories") or [])[:4],
            coverUrl=cover.replace("http://", "https://").replace("&edge=curl", ""),
            link=info.get("infoLink", ""),
        ))
    return results


_SEARCHERS = {"audible": search_audible, "itunes": search_itunes, "google": search_google}


def search(title, author="", providers=("audible", "itunes"), limit=5):
    """Query several providers. A provider that fails is reported, not fatal."""
    found, errors = [], {}
    for name in providers:
        searcher = _SEARCHERS.get(name)
        if not searcher:
            continue
        try:
            found.extend(searcher(title, author, limit=limit))
        except LookupError_ as exc:
            errors[name] = str(exc)
        except Exception as exc:                      # a provider changing shape on us
            errors[name] = f"unexpected response ({type(exc).__name__})"
    return rank(found, title, author), errors


# -------------------------------------------------------------------- ranking

def _normalise(text):
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()


def score(candidate, title, author):
    """Rough closeness of a candidate to what we asked for, 0–100.

    Word overlap on the title, plus a bonus for the author matching and for the
    provider carrying an actual description — which is the whole point here.
    """
    wanted, got = set(_normalise(title)), set(_normalise(candidate["title"]))
    if not wanted:
        return 0
    overlap = len(wanted & got) / len(wanted)
    points = overlap * 60

    if author:
        author_words = set(_normalise(author))
        candidate_words = set(_normalise(" ".join(candidate["authors"])))
        if author_words and candidate_words:
            points += (len(author_words & candidate_words) / len(author_words)) * 25
    if candidate["description"]:
        points += 10
    if candidate["coverUrl"]:
        points += 5
    return round(min(100, points))


def rank(candidates, title, author):
    for candidate in candidates:
        candidate["score"] = score(candidate, title, author)
    # Audible first among equals: it is the only one with narrator and runtime.
    order = {"audible": 0, "itunes": 1, "google": 2}
    candidates.sort(key=lambda c: (-c["score"], order.get(c["provider"], 9)))
    return candidates


# ------------------------------------------------------------- author bios
#
# Neither Audible nor Google hands back an author biography, so these come from
# elsewhere. Wikipedia has the better coverage and Open Library the better
# precision; both are searched and the user picks, because a name search will
# cheerfully return a completely different person.

AUTHOR_PROVIDERS = ("wikipedia", "openlibrary")


def search_authors_wikipedia(name, limit=3):
    query = urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": f"{name} author",
        "format": "json", "srlimit": max(1, min(limit, 5)),
    })
    hits = _get(f"https://en.wikipedia.org/w/api.php?{query}").get("query", {}).get("search", [])

    results = []
    for hit in hits:
        title = hit.get("title", "")
        try:
            summary = _get("https://en.wikipedia.org/api/rest_v1/page/summary/"
                           + urllib.parse.quote(title.replace(" ", "_")))
        except LookupError_:
            continue
        if summary.get("type") == "disambiguation":
            continue
        results.append({
            "provider": "wikipedia",
            "id": title,
            "name": summary.get("title", title),
            "summary": summary.get("description", ""),
            "bio": clean_text(summary.get("extract")),
            "born": "",
            "died": "",
            "topWork": "",
            "link": (summary.get("content_urls", {}).get("desktop", {}).get("page", "")),
        })
    return results


def search_authors_openlibrary(name, limit=3):
    query = urllib.parse.urlencode({"q": name, "limit": max(1, min(limit, 5))})
    docs = _get(f"https://openlibrary.org/search/authors.json?{query}").get("docs", [])

    results = []
    for doc in docs:
        key = doc.get("key", "")
        bio = ""
        if key:
            try:
                detail = _get(f"https://openlibrary.org/authors/{key}.json")
                raw = detail.get("bio")
                bio = clean_text(raw.get("value") if isinstance(raw, dict) else raw)
            except LookupError_:
                bio = ""
        works = doc.get("work_count") or 0
        results.append({
            "provider": "openlibrary",
            "id": key,
            "name": doc.get("name", ""),
            "summary": f"{works} work{'s' if works != 1 else ''} on Open Library",
            "bio": bio,
            "born": doc.get("birth_date", "") or "",
            "died": doc.get("death_date", "") or "",
            "topWork": doc.get("top_work", "") or "",
            "link": f"https://openlibrary.org/authors/{key}" if key else "",
        })
    return results


_AUTHOR_SEARCHERS = {
    "wikipedia": search_authors_wikipedia,
    "openlibrary": search_authors_openlibrary,
}


def search_authors(name, providers=AUTHOR_PROVIDERS, limit=3):
    """Candidate author pages. An entry with no biography is still returned so
    the absence is visible rather than looking like a failed search."""
    found, errors = [], {}
    for provider in providers:
        searcher = _AUTHOR_SEARCHERS.get(provider)
        if not searcher:
            continue
        try:
            found.extend(searcher(name, limit=limit))
        except LookupError_ as exc:
            errors[provider] = str(exc)
        except Exception as exc:
            errors[provider] = f"unexpected response ({type(exc).__name__})"

    wanted = set(_normalise(name))
    for candidate in found:
        got = set(_normalise(candidate["name"]))
        overlap = len(wanted & got) / len(wanted) if wanted else 0
        candidate["score"] = round(min(100, overlap * 80 + (20 if candidate["bio"] else 0)))
    found.sort(key=lambda c: -c["score"])
    return found, errors


def fetch_cover(url, limit=8 << 20):
    """Download a cover image. Returns (content_type, bytes) or None."""
    if not url.startswith(("http://", "https://")):
        return None
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            if not content_type.startswith("image/"):
                return None
            data = response.read(limit + 1)
            if not data or len(data) > limit:
                return None
            return content_type, data
    except Exception:
        return None
