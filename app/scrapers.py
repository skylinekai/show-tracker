"""Exhibitor-list scrapers for the platforms most trade shows run on.

Each scraper takes a show URL and returns a list of Exhibitor dicts:
    {"name", "booth", "description", "website", "profile_url"}

Supported:
  * Map Your Show  - <show>.mapyourshow.com/8_0/...  (JSON search API, whole list in one call)
  * A2Z (Personify / Momentive) - *.a2zinc.net/.../Public/Exhibitors.aspx  (server-rendered table)
  * eShow (goeshow)  - .../exhibitor_list.cfm or exh_list.cfm  (server-rendered table)
  * Generic         - any page with an HTML table that has a company/exhibitor column
"""
from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 90


class ScrapeError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def clean(text) -> str:
    text = html.unescape(str(text or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


_SUFFIXES = re.compile(
    r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|gmbh|ag|sa|srl|plc|bv|pty|llp|lp)\b\.?",
    re.I,
)


def name_key(name: str) -> str:
    """Normalised key so 'Acme, Inc.' and 'ACME Inc' match across runs and shows."""
    n = clean(name).lower().replace("&", " and ")
    n = _SUFFIXES.sub(" ", n)
    n = re.sub(r"^the\s+", "", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


# Rows on floorplans that are features of the show, not companies.
_JUNK = re.compile(
    r"\b(theat(er|re)|stage|pavilion|lounge|caf[eé]|food court|registration|restrooms?|"
    r"ask an expert|presentations?|networking|guest nation|show office|press room|"
    r"charging station|innovation (zone|hub|showcase)|demo area|meeting rooms?|"
    r"available|tba|tbd|reserved|coming soon|entrance|exit|first aid|coat check)\b",
    re.I,
)


def looks_like_junk(name: str) -> bool:
    n = clean(name)
    if len(n) < 2 or not re.search(r"[A-Za-z]", n):
        return True
    return bool(_JUNK.search(n))


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    return s


def _row(name, booth="", description="", website="", profile_url="", booth_sqft=None, booth_dims="", hall="") -> dict:
    return {
        "name": clean(name)[:200],
        "booth": clean(booth)[:60],
        "description": clean(description)[:800],
        "website": clean(website)[:300],
        "profile_url": clean(profile_url)[:500],
        "booth_sqft": int(booth_sqft) if booth_sqft else None,
        "booth_dims": clean(booth_dims)[:40],
        "hall": clean(hall)[:120],
    }


# ---------------------------------------------------------------- platform detection
def detect_platform(url: str) -> str:
    u = urlparse(url)
    host, path = u.netloc.lower(), u.path.lower()
    if "mapyourshow.com" in host or "/8_0/" in path:
        return "mapyourshow"
    if "a2zinc.net" in host or "smallworldlabs.com" in host or "/public/exhibitors.aspx" in path or "/public/eventmap.aspx" in path:
        return "a2z"
    if "goeshow.com" in host or re.search(r"(exhibitor_exhibitor_list|exhibitor_list|exh_list)\.cfm", path):
        return "eshow"
    return "generic"


PLATFORM_LABELS = {
    "mapyourshow": "Map Your Show",
    "a2z": "A2Z",
    "eshow": "eShow",
    "generic": "Web page",
}


# ---------------------------------------------------------------- Map Your Show
def mys_base(url: str) -> str:
    """https://nab27.mapyourshow.com/8_0/explore/... -> https://nab27.mapyourshow.com/8_0"""
    u = urlparse(url if "://" in url else "https://" + url)
    m = re.search(r"/(\d+_\d+)/", u.path + "/")
    root = m.group(1) if m else "8_0"
    return f"{u.scheme or 'https'}://{u.netloc}/{root}"


def parse_mys_json(data: dict, base: str) -> list[dict]:
    try:
        hits = data["DATA"]["results"]["exhibitor"]["hit"]
    except (KeyError, TypeError):
        raise ScrapeError("Map Your Show answered, but not with an exhibitor list (the directory may be closed).")
    rows = []
    for hit in hits or []:
        f = hit.get("fields") or {}
        name = f.get("exhname_t")
        if not clean(name):
            continue
        booths = [clean(str(b).replace("randomstring", "")) for b in (f.get("boothsdisplay_la") or f.get("booths_la") or [])]
        exhid = f.get("exhid_l") or hit.get("id") or ""
        profile = f"{base}/exhibitor/exhibitor-details.cfm?exhid={exhid}" if exhid else ""
        website = f.get("exhurl_t") or f.get("website_t") or ""
        row = _row(name, ", ".join(b for b in booths if b), f.get("exhdesc_t") or "", website, profile)
        row["_exhid"] = str(exhid)
        rows.append(row)
    return rows


def parse_mys_booths(data: dict, hall: str) -> list[dict]:
    """One hall of the floorplan: [{exhid, booth, area, width_ft, depth_ft, hall}]."""
    cols = data.get("COLUMNS") or [] if isinstance(data, dict) else []
    out = []
    for raw in (data.get("DATA") or []) if isinstance(data, dict) else []:
        rec = dict(zip(cols, raw))
        if rec.get("OBJECTTYPE") != "booth" or not rec.get("EXHID"):
            continue
        props = rec.get("FEATUREPROPERTIES") or {}
        if isinstance(props, str):
            try:
                import json as _json
                props = _json.loads(props)
            except ValueError:
                props = {}
        props = props.get("properties") or {}
        try:
            area = float(props.get("area") or 0)
        except (TypeError, ValueError):
            area = 0.0
        w, d = props.get("boothWidth"), props.get("boothHeight")  # inches
        try:
            w, d = (float(w), float(d)) if w and d else (None, None)
        except (TypeError, ValueError):
            w, d = None, None
        if not area and w and d:
            area = w * d / 144.0
        out.append({"exhid": str(rec["EXHID"]), "booth": clean(rec.get("BOOTHDISPLAY") or rec.get("BOOTH") or ""),
                    "area": round(area), "width_ft": round(w / 12) if w else None,
                    "depth_ft": round(d / 12) if d else None, "hall": hall})
    return out


def join_mys_booths(rows: list[dict], booths: list[dict], hall_names: dict) -> None:
    """Adds booth_sqft / booth_dims / hall to gallery rows, in place."""
    by_exh: dict[str, list[dict]] = {}
    for b in booths:
        by_exh.setdefault(b["exhid"], []).append(b)
    for r in rows:
        mine = by_exh.get(r.get("_exhid", ""))
        if not mine:
            continue
        total = sum(b["area"] for b in mine)
        big = max(mine, key=lambda b: b["area"])
        dims = f"{big['width_ft']}×{big['depth_ft']} ft" if big["width_ft"] and big["depth_ft"] else ""
        if len(mine) > 1 and dims:
            dims += f" (+{len(mine) - 1})"
        r["booth_sqft"] = int(total) or None
        r["booth_dims"] = dims
        r["hall"] = ", ".join(sorted({hall_names.get(b["hall"], b["hall"]) for b in mine}))


def _mys_booth_sizes(s: requests.Session, base: str, rows: list[dict]) -> None:
    """Best effort: floorplan geometry gives exact booth sizes. Never fails the check."""
    origin = base.rsplit("/", 1)[0]
    xhr = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, */*"}
    try:
        halls_json = s.get(f"{base}/ajax/remote-proxy.cfm", timeout=TIMEOUT,
                           params={"action": "getsearchoptions", "function": "getBoothHalls"},
                           headers={**xhr, "Referer": f"{base}/explore/exhibitor-gallery.cfm"}).json()
        halls = {str(h.get("fieldvalue")): clean(h.get("fielddisplay") or h.get("fieldvalue"))
                 for h in (halls_json.get("DATA") or []) if h.get("fieldvalue")}
        fp_page = f"{base}/floorplan/"
        showid, versions = urlparse(origin).netloc.split(".")[0].upper(), ["02"]
        try:
            page = s.get(fp_page, timeout=TIMEOUT).text
            m = re.search(r'ShowID\s*=\s*"([^"]+)"', page)
            showid = m.group(1) if m else showid
            m = re.search(r"floorplan/(\d{2})/", page)
            if m and m.group(1) != "02":
                versions.append(m.group(1))
        except requests.RequestException:
            pass
        for ver in versions:
            booths = []
            for hall in halls or {"": ""}:
                try:
                    data = s.get(f"{base}/floorplan/{ver}/_remote-proxy.cfm", timeout=TIMEOUT,
                                 params={"showid": showid, "selectedbooth": "", "hallid": hall,
                                         "action": "GetBoothByHall", "method": "GetBoothByHall", "regid": 0},
                                 headers={**xhr, "Referer": fp_page}).json()
                    booths += parse_mys_booths(data, hall)
                except (requests.RequestException, ValueError):
                    continue
            if booths:
                join_mys_booths(rows, booths, halls)
                return
    except (requests.RequestException, ValueError, AttributeError):
        return


def scrape_mapyourshow(url: str, session: requests.Session | None = None) -> list[dict]:
    s = session or _session()
    base = mys_base(url)
    gallery = f"{base}/explore/exhibitor-gallery.cfm?featured=false"
    try:  # picks up the session cookie; without it the API often answers 403
        s.get(gallery, timeout=TIMEOUT)
    except requests.RequestException:
        pass
    r = s.get(
        f"{base}/ajax/remote-proxy.cfm",
        params={"action": "search", "searchtype": "exhibitorgallery", "searchsize": 20000, "start": 0},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": gallery, "Accept": "application/json, */*"},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise ScrapeError(f"Map Your Show returned HTTP {r.status_code}.")
    try:
        data = r.json()
    except ValueError:
        raise ScrapeError("Map Your Show returned a web page instead of data (directory closed or not yet live).")
    rows = parse_mys_json(data, base)
    if rows and session is None:
        _mys_booth_sizes(s, base, rows)
    return rows


def mys_next_edition_url(url: str) -> str | None:
    """nab27.mapyourshow.com -> nab28.mapyourshow.com (also handles 4-digit years)."""
    u = urlparse(url)
    m = re.match(r"^([a-z0-9-]*?)(20)?(\d{2})([a-z0-9-]*)\.mapyourshow\.com$", u.netloc.lower())
    if not m:
        return None
    prefix, century, yy, suffix = m.groups()
    nxt = int(yy) + 1
    host = f"{prefix}{century or ''}{nxt:02d}{suffix}.mapyourshow.com"
    return f"https://{host}/8_0/explore/exhibitor-gallery.cfm?featured=false"


# ---------------------------------------------------------------- A2Z
def a2z_list_url(url: str) -> str:
    u = urlparse(url)
    path = re.sub(r"/Public/[^/]*$", "/Public/Exhibitors.aspx", u.path, flags=re.I)
    if not re.search(r"/Public/Exhibitors\.aspx$", path, re.I):
        return url
    q = parse_qs(u.query)
    q.setdefault("Index", ["All"])
    return urlunparse(u._replace(path=path, query=urlencode(q, doseq=True)))


def parse_a2z_html(page: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(page, "lxml")
    rows, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"eBooth\.aspx", re.I)):
        name = clean(a.get_text(" "))
        if not name or name.lower() in {"view", "details", "more"}:
            continue
        tr = a.find_parent("tr")
        booth = ""
        if tr is not None:
            cells = tr.find_all("td", class_=re.compile(r"boothLabel", re.I))
            booth = ", ".join(c for c in (clean(td.get_text(" ")) for td in cells) if c)
        key = (name_key(name), booth)
        if key in seen:
            continue
        seen.add(key)
        rows.append(_row(name, booth, profile_url=urljoin(base_url, a["href"])))
    return rows


def scrape_a2z(url: str, session: requests.Session | None = None) -> list[dict]:
    s = session or _session()
    list_url = a2z_list_url(url)
    r = s.get(list_url, timeout=TIMEOUT * 2)
    if r.status_code != 200:
        raise ScrapeError(f"A2Z returned HTTP {r.status_code}.")
    rows = parse_a2z_html(r.text, list_url)
    if not rows:
        rows = parse_generic_html(r.text, list_url)
    return rows


# ---------------------------------------------------------------- eShow
def parse_eshow_html(page: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(page, "lxml")
    rows = []
    for tr in soup.find_all("tr"):
        if "ExhibitorPopup" not in str(tr):
            continue
        a = tr.find("a", href=re.compile(r"javascript", re.I)) or tr.find("a")
        name = clean(a.get_text(" ")) if a else ""
        if not name:
            continue
        tds = [clean(td.get_text(" ")) for td in tr.find_all("td")]
        booth = next((t for t in tds if t and t != name and re.fullmatch(r"[A-Z]?\d[\w\-/ ,]*", t)), "")
        rows.append(_row(name, booth))
    return rows


def scrape_eshow(url: str, session: requests.Session | None = None) -> list[dict]:
    s = session or _session()
    r = s.get(url, timeout=TIMEOUT)
    if r.status_code != 200:
        raise ScrapeError(f"eShow returned HTTP {r.status_code}.")
    rows = parse_eshow_html(r.text, url)
    return rows or parse_generic_html(r.text, url)


# ---------------------------------------------------------------- generic tables
def parse_generic_html(page: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(page, "lxml")
    best: list[dict] = []
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if len(trs) < 3:
            continue
        headers = [clean(c.get_text(" ")).lower() for c in trs[0].find_all(["th", "td"])]
        name_idx = next((i for i, h in enumerate(headers) if re.search(r"company|exhibitor|organi[sz]ation|name", h)), None)
        if name_idx is None:
            continue
        booth_idx = next((i for i, h in enumerate(headers) if re.search(r"booth|stand", h)), None)
        rows = []
        for tr in trs[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= name_idx:
                continue
            name = clean(cells[name_idx].get_text(" "))
            if not name:
                continue
            booth = clean(cells[booth_idx].get_text(" ")) if booth_idx is not None and len(cells) > booth_idx else ""
            link = tr.find("a", href=re.compile(r"^https?://", re.I))
            site = link["href"] if link and urlparse(link["href"]).netloc != urlparse(base_url).netloc else ""
            rows.append(_row(name, booth, website=site))
        if len(rows) > len(best):
            best = rows
    return best


def scrape_generic(url: str, session: requests.Session | None = None) -> list[dict]:
    s = session or _session()
    r = s.get(url, timeout=TIMEOUT)
    if r.status_code != 200:
        raise ScrapeError(f"The page returned HTTP {r.status_code}.")
    rows = parse_generic_html(r.text, url)
    if not rows:
        raise ScrapeError(
            "No exhibitor table found on this page. It may load exhibitors with JavaScript; "
            "try the show's Map Your Show / A2Z / eShow directory link instead."
        )
    return rows


SCRAPERS = {
    "mapyourshow": scrape_mapyourshow,
    "a2z": scrape_a2z,
    "eshow": scrape_eshow,
    "generic": scrape_generic,
}


def scrape(url: str, platform: str | None = None) -> list[dict]:
    platform = platform or detect_platform(url)
    try:
        rows = SCRAPERS[platform](url)
    except requests.RequestException as exc:
        raise ScrapeError(f"Couldn't reach the show site: {exc.__class__.__name__}.") from exc
    # de-duplicate by normalised name, merging booths
    merged: dict[str, dict] = {}
    for r in rows:
        k = name_key(r["name"])
        if not k:
            continue
        if k in merged:
            if r["booth"] and r["booth"] not in merged[k]["booth"]:
                merged[k]["booth"] = ", ".join(b for b in (merged[k]["booth"], r["booth"]) if b)
            for f in ("description", "website", "profile_url", "booth_dims", "hall"):
                merged[k][f] = merged[k].get(f) or r.get(f) or ""
            if r.get("booth_sqft"):
                merged[k]["booth_sqft"] = (merged[k].get("booth_sqft") or 0) + r["booth_sqft"]
        else:
            merged[k] = {key: v for key, v in r.items() if not key.startswith("_")}
    return list(merged.values())
