#!/usr/bin/env python3
"""Synchronizacia vysledkov zapasov a ligovej tabulky zo szfb.sk do administracie
kluboveho webu (eoscms).

Pouzitie:
    python3 sync_vysledky.py            # len ukaze, co by sa zmenilo (nic nezapisuje)
    python3 sync_vysledky.py --apply    # zapise zmeny do adminu

Prihlasovacie udaje sa citaju z premennych prostredia EOS_EMAIL a EOS_PASSWORD
(na GitHube z Secrets), alebo z macOS Keychain (sluzba "tsunami-eoscms"):
    security add-generic-password -s tsunami-eoscms -a "EMAIL" -w
"""

import argparse
import html
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

# --- Nastavenia --------------------------------------------------------------

SZFB_BASE = "https://www.szfb.sk"
SZFB_OUR_ABBR = "TSU"

ADMIN_BASE = "https://tsunamiflorbal.eoscms.cz"
ADMIN_DASHBOARD = ADMIN_BASE + "/dashboard"
ADMIN_SEASON_ID = 3   # 2026/2027


class Competition:
    """Sutaz na szfb.sk (TournamentID) a k nej prisluchajuci tim v admine.

    Ak je liga na szfb.sk rozdelena do divizii, kazda divizia je samostatna sutaz v admine:
    szfb_part = TournamentPartID divizie, admin_cid = ID sutaze v admine. Zoznam zapasov
    v admine sa podla sutaze filtrovat neda, preto zapasy synchronizuje len jedna z divizii
    (games=True) a ostatne len tabulku."""

    def __init__(self, name, szfb_id, szfb_slug, admin_team_id, szfb_part=None, admin_cid=None, games=True):
        self.name = name
        self.games = games
        self.results = f"{SZFB_BASE}/sk/stats/results/{szfb_id}/{szfb_slug}"
        self.standings = f"{SZFB_BASE}/sk/stats/standings/{szfb_id}/{szfb_slug}"
        if szfb_part:
            self.standings += f"?TournamentPartID={szfb_part}"
        self.filter = f"sid={ADMIN_SEASON_ID}&tid={admin_team_id}"
        if admin_cid:
            self.filter += f"&cid={admin_cid}"
        self.admin_games = f"{ADMIN_BASE}/games/?sid={ADMIN_SEASON_ID}&tid={admin_team_id}&dataview=games_all"
        self.admin_standings = f"{ADMIN_BASE}/games/standings/?{self.filter}"


COMPETITIONS = [
    Competition("Extraliga muzov", 1241, "florbalova-extraliga-muzov", 25),
    Competition("Liga juniorov", 1263, "liga-juniorov-bratislava", 24),
    # Tsunami ma v lige dva timy (A a B), oba su v admine pod druzstvom Dorastenci
    Competition("Liga dorastencov", 1266, "liga-dorastencov-bratislava", 23),
    # dve divizie, dve sutaze v admine; timy A (zelena) a B (cervena) pod druzstvom Starsi ziaci
    Competition("Liga starsich ziakov - Zelena divizia", 1280, "liga-starsich-ziakov-bratislava", 21,
                szfb_part=4861, admin_cid=39),
    Competition("Liga starsich ziakov - Cervena divizia", 1280, "liga-starsich-ziakov-bratislava", 21,
                szfb_part=4862, admin_cid=40, games=False),
    # styri divizie 1. kola; Heroes (zelena) a Surfers (zlta) pod druzstvom Mladsi ziaci.
    # Cervena a Biela maju tabulky vygenerovane z pomocnych skrytych zapasov (bez kodu, skript ich ignoruje).
    Competition("Liga mladsich ziakov - Zelena divizia", 1279, "liga-mladsich-ziakov-bratislava", 22,
                szfb_part=4857, admin_cid=41),
    Competition("Liga mladsich ziakov - Zlta divizia", 1279, "liga-mladsich-ziakov-bratislava", 22,
                szfb_part=4858, admin_cid=42, games=False),
    Competition("Liga mladsich ziakov - Cervena divizia", 1279, "liga-mladsich-ziakov-bratislava", 22,
                szfb_part=4859, admin_cid=43, games=False),
    Competition("Liga mladsich ziakov - Biela divizia", 1279, "liga-mladsich-ziakov-bratislava", 22,
                szfb_part=4860, admin_cid=44, games=False),
]

PLACES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miesta.txt")

KEYCHAIN_SERVICE = "tsunami-eoscms"
STATE_DIR = os.path.expanduser("~/Library/Application Support/tsunami-vysledky")
COOKIE_FILE = os.path.join(STATE_DIR, "session.cookies")

USER_AGENT = "Mozilla/5.0 (Macintosh) tsunami-vysledky/1.0"

# szfb oznacenie -> hodnota "Hraci doba" v admine
RESULT_TYPE = {"": "", "pp": "p", "sn": "pn"}
# "Stav utkani" v admine
STATUS_ENTERED = "0"     # zadano (bez casu)
STATUS_SCHEDULED = "1"   # naplanovano
STATUS_PLAYED = "2"      # odehrano
STATUS_FORFEIT = "5"     # zkontumovano

# stlpce tabulky: poradie na szfb.sk (Z, V, Vp, R, Pp, P) -> polia v admine
STANDINGS_FIELDS = ["gp", "gw", "gwot", "gt", "glot", "gl"]


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --- Prihlasovacie udaje -----------------------------------------------------

def credentials():
    """Vrati (email, heslo) z premennych prostredia (GitHub Secrets), inak z macOS Keychain."""
    email, password = os.environ.get("EOS_EMAIL"), os.environ.get("EOS_PASSWORD")
    if email and password:
        return email, password
    try:
        attrs = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True, text=True, check=True).stdout
        password = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, check=True).stdout.rstrip("\n")
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("Chybaju prihlasovacie udaje: nastav EOS_EMAIL a EOS_PASSWORD, "
                 f"alebo na Macu pridaj polozku '{KEYCHAIN_SERVICE}' do Keychain:\n"
                 f'  security add-generic-password -s {KEYCHAIN_SERVICE} -a "EMAIL" -w')
    m = re.search(r'"acct"<blob>="([^"]*)"', attrs)
    if not m:
        sys.exit("V Keychain polozke chyba ucet (email).")
    return m.group(1), password


# --- HTTP --------------------------------------------------------------------

def make_opener(jar):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener


def fetch(opener, url, data=None):
    """Vrati (finalna URL po presmerovani, text). `data` je dict alebo zoznam dvojic."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    with opener.open(url, body, timeout=30) as resp:
        return resp.geturl(), resp.read().decode("utf-8", errors="replace")


def cells(row):
    """Textovy obsah buniek riadku tabulky."""
    return [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split())
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]


# --- szfb.sk -----------------------------------------------------------------

def load_place_names():
    """Nacita miesta.txt: {nazov na szfb.sk: skrateny nazov na webe}."""
    names = {}
    if os.path.exists(PLACES_FILE):
        with open(PLACES_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    full, short = line.split("=", 1)
                    names[full.strip().casefold()] = short.strip()
    return names


PLACE_NAMES = load_place_names()


def short_place(full):
    return PLACE_NAMES.get(full.casefold(), full)


def clean_place(text):
    """'Tsunami Arena  "kategoria B"' -> 'Tsunami Arena'; 't.b.a.' -> ''."""
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
    text = re.sub(r'\s*"kateg[^"]*"\s*$', "", text).strip()
    return "" if text.lower() in ("t.b.a.", "tba", "") else text


def szfb_matches(results_page):
    """Vrati {kod zapasu: udaje} pre vsetky zapasy nasho timu.
    Pri neodohranych zapasoch je score_home/score_away None, neznamy cas/miesto je ''."""
    result = {}
    for row in results_page.split('<tr class="fn-tap-row')[1:]:
        code = re.search(r'<td class="hidden-xs ">\s*(\d+)\s*</td>', row)
        abbrs = re.findall(r'<span class="visible-xs competitor-img">.*?>\s*([^<\s]+)\s*</span>', row, re.S)
        score_box = re.search(r'<div class="game-clash-result text-center hidden-xs">(.*?)</div>', row, re.S)
        if not (code and len(abbrs) == 2 and score_box) or SZFB_OUR_ABBR not in abbrs:
            continue
        date = re.search(r'hover-hide-sm-down[^"]*">\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*<', row)
        time = re.search(r'<div class="hidden-xs hover-hide[^"]*">\s*(\d{1,2}):(\d{2})\s*</div>', row)
        place = re.search(r'<div class="td-box hidden-xs hidden-sm hover-hide[^"]*">(.*?)</div>', row, re.S)
        goals = re.findall(r'<span class="matchresult">(\d+)</span>', score_box.group(1))
        marker = re.search(r'font-small">(pp|sn)</span>', score_box.group(1))
        link = re.search(r'href="(/sk/stats/matches/[^"]+/match/\d+)"', score_box.group(1))
        played = len(goals) == 2
        result[code.group(1)] = {
            "home_abbr": abbrs[0],
            "away_abbr": abbrs[1],
            "date": f"{date.group(3)}-{int(date.group(2)):02d}-{int(date.group(1)):02d}" if date else "",
            # 0:00 = cas este nie je zverejneny
            "time": f"{int(time.group(1)):02d}:{time.group(2)}"
                    if time and (int(time.group(1)), time.group(2)) != (0, "00") else "",
            "place_full": clean_place(place.group(1)) if place else "",
            "score_home": goals[0] if played else None,
            "score_away": goals[1] if played else None,
            "result_type": RESULT_TYPE[marker.group(1) if marker else ""],
            "url": SZFB_BASE + link.group(1) if link and played else None,
        }
    return result


def szfb_periods(opener, match_url):
    _, page = fetch(opener, match_url + "/overview")
    m = re.search(r'data-match-text="scorePeriods">([^<]*)<', page)
    return html.unescape(m.group(1)).strip() if m else ""


def szfb_team_abbrs(results_page):
    """Vrati {ID timu na szfb.sk: skratka} zo stranky vysledkov."""
    return dict(re.findall(
        r'<span class="visible-xs competitor-img">\s*<img src="[^"]*/Competitor/(\d+)/[^>]*>\s*([^<\s]+)\s*</span>',
        results_page))


def szfb_standings(opener, comp, abbrs):
    """Vrati tabulku ako zoznam riadkov v poradi: {abbr, name, gp, ..., points}."""
    _, page = fetch(opener, comp.standings)
    table = re.search(r"<table.*?</table>", page, re.S)
    rows = []
    for row in re.findall(r"<tr>.*?</tr>", table.group(0) if table else "", re.S):
        team = re.search(r"/team/(\d+)/", row)
        c = cells(row)
        if not team or len(c) != 10:
            continue
        scored, conceded = c[8].split(":")
        entry = {"abbr": abbrs.get(team.group(1)), "name": c[1],
                 "score_h": scored.strip(), "score_a": conceded.strip(), "points": c[9]}
        entry.update(zip(STANDINGS_FIELDS, c[2:8]))
        rows.append(entry)
    return rows


# --- eoscms admin ------------------------------------------------------------

class FormParser(HTMLParser):
    """Zozbiera polia prveho formulara, ktory vyhovuje `match(attrs)`,
    tak, ako by ich odoslal prehliadac."""

    def __init__(self, match):
        super().__init__(convert_charrefs=True)
        self.match = match
        self.found = False
        self.action = None
        self.fields = []          # [(name, value)] v poradi
        self._in_form = False
        self._select = None       # [name, first_value, selected_value]
        self._textarea = None     # [name, text]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and not self.found and self.match(a):
            self._in_form = self.found = True
            self.action = a.get("action")
            return
        if not self._in_form:
            return
        if tag == "input" and a.get("name"):
            itype = (a.get("type") or "text").lower()
            if itype in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields.append((a["name"], a.get("value", "on")))
            elif itype not in ("submit", "button", "file", "image", "reset"):
                self.fields.append((a["name"], a.get("value", "")))
        elif tag == "select" and a.get("name"):
            self._select = [a["name"], None, None]
        elif tag == "option" and self._select is not None:
            value = a.get("value")
            if value is not None:
                if self._select[1] is None:
                    self._select[1] = value
                if "selected" in a:
                    self._select[2] = value
        elif tag == "textarea" and a.get("name"):
            self._textarea = [a["name"], ""]

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea[1] += data

    def handle_endtag(self, tag):
        if tag == "form" and self._in_form:
            self._in_form = False
        elif tag == "select" and self._select is not None:
            name, first, selected = self._select
            self.fields.append((name, selected if selected is not None else (first or "")))
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            self.fields.append(tuple(self._textarea))
            self._textarea = None


def parse_form(page, match):
    parser = FormParser(match)
    parser.feed(page)
    return parser if parser.found else None


def is_game_form(attrs):
    return "game_update.php" in (attrs.get("action") or "")


def is_standings_form(attrs):
    return attrs.get("id") == "games-standings"


def field(fields, name):
    for n, v in fields:
        if n == name:
            return v
    return None


def with_changes(fields, changes):
    return [(n, changes.get(n, v)) for n, v in fields]


def is_login_page(url, page=""):
    """Admin pri neprihlasenom pouzivatelovi nepresmeruje cez HTTP, ale vrati stranku
    s JavaScript presmerovanim na "/?uri=...", pripadne prihlasovaci formular."""
    return (urllib.parse.urlparse(url).path.rstrip("/") == "/login"
            or re.search(r'<form[^>]*action="[^"]*/login"', page) is not None
            or re.search(r"location\.href\s*=\s*'[^']*\?uri=", page) is not None)


def admin_login(opener):
    email, password = credentials()
    _, page = fetch(opener, ADMIN_BASE + "/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)
    if not token:
        sys.exit("Na prihlasovacej stranke sa nenasiel CSRF token.")
    url, page = fetch(opener, ADMIN_BASE + "/login",
                      {"csrf_token": token.group(1), "email": email, "password": password})
    if is_login_page(url, page):
        sys.exit("Prihlasenie do adminu zlyhalo (skontroluj email/heslo).")
    log("Prihlaseny do adminu.")


_csrf = {"token": None}  # token z <meta name="csrf-token">, potrebny pre ajax volania


def admin_request(opener, url, data=None):
    """Poziadavka do adminu; ak session vyprsala, prihlasi sa znova a zopakuje ju."""
    final_url, page = fetch(opener, url, data)
    if is_login_page(final_url, page):
        log("Session vyprsala alebo neexistuje - prihlasujem sa.")
        admin_login(opener)
        final_url, page = fetch(opener, url, data)
        if is_login_page(final_url, page):
            sys.exit("Ani po prihlaseni sa nepodarilo otvorit admin.")
    meta = re.search(r'<meta name="csrf-token" content="([^"]+)"', page)
    if meta:
        _csrf["token"] = meta.group(1)
    return page


def admin_post_form(opener, url, fields):
    """Odosle formular ako prehliadac. CSRF token doplna do formularov az JavaScript
    zo <meta name="csrf-token">, preto ho pridame sami. Vrati chybovu hlasku alebo None."""
    if not any(n == "csrf_token" for n, _ in fields):
        fields = [("csrf_token", _csrf["token"])] + list(fields)
    final_url, _ = fetch(opener, url, fields)
    if is_login_page(final_url):
        sys.exit("Session vyprsala pocas zapisu - spusti znova.")
    error = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query).get("error")
    return error[0] if error else None


def admin_ajax(opener, name, params, check_ok=True):
    if not _csrf["token"]:
        admin_request(opener, ADMIN_DASHBOARD)
    page = admin_request(opener, f"{ADMIN_BASE}/ajax/{name}.php",
                         dict(params, csrf_token=_csrf["token"]))
    try:
        result = json.loads(page)
    except ValueError:
        sys.exit(f"Neocakavana odpoved z ajax/{name}.php")
    if check_ok and not result.get("ok"):
        sys.exit(f"Chyba z ajax/{name}.php: {result.get('error')}")
    return result


def clear_web_cache(opener):
    """To iste ako tlacidlo 'Promazat webovou cache' v admine."""
    page = admin_request(opener, ADMIN_DASHBOARD)
    btn = page.find('id="clearWebCacheBtn"')
    hashes = re.findall(r"hash:\s*'([^']+)'", page[:btn] if btn >= 0 else "")
    if not hashes:
        log("CHYBA: v admine sa nenaslo tlacidlo na premazanie cache webu.")
        return
    result = admin_ajax(opener, "clear_web_cache", {"hash": hashes[-1]}, check_ok=False)
    for url in result.get("cleared") or []:
        log(f"Cache webu premazana: {url}")
    for err in result.get("errors") or []:
        log(f"CHYBA pri mazani cache {err.get('url')}: {err.get('error')}")


# --- Zapasy ------------------------------------------------------------------

def admin_game_form(opener, game_id):
    return parse_form(admin_request(opener, f"{ADMIN_BASE}/games/?id={game_id}"), is_game_form)


def game_value(fields, name):
    """Hodnota pola zapasu v admine, cas zjednoteny na HH:MM."""
    value = field(fields, name) or ""
    if name == "cas":
        value = value[:5]
    return value


def same_place(admin_place, szfb_place):
    """Admin casto pouziva skrateny nazov haly ('MSH Trencin' vs 'MSH Trencin - centralna hala')."""
    a, s = admin_place.casefold().strip(), szfb_place.casefold().strip()
    return bool(a) and (s.startswith(a) or a.startswith(s))


def schedule_changes(fields, src):
    """Datum, cas, miesto a stav podla szfb.sk. Nezverejnene udaje (t.b.a.) v admine nemaze."""
    wanted = {}
    if src["date"]:
        wanted["datum"] = src["date"]
    if src["time"]:
        wanted["cas"] = src["time"]
    full = src["place_full"]
    admin_place = field(fields, "misto") or ""
    if full and admin_place != short_place(full) and not same_place(admin_place, full):
        wanted["misto"] = short_place(full)
        if full.casefold() not in PLACE_NAMES:
            log(f"Info: hala '{full}' nie je v miesta.txt, zapisuje sa nazov zo szfb.sk.")
    status = field(fields, "game_status")
    if status == STATUS_ENTERED and src["time"]:
        wanted["game_status"] = STATUS_SCHEDULED
    return wanted


def sync_games(opener, comp, results_page, apply):
    szfb = szfb_matches(results_page)
    played = sum(1 for m in szfb.values() if m["score_home"] is not None)
    ids = list(dict.fromkeys(re.findall(r'/games/\?id=(\d+)"', admin_request(opener, comp.admin_games))))
    log(f"Zapasy: szfb.sk {len(szfb)} (odohranych {played}), admin {len(ids)}.")
    if not ids:
        log("Zapasy: v admine zatial nie su ziadne, preskakujem.")
        return 0

    changed = 0
    for game_id in ids:
        form = admin_game_form(opener, game_id)
        if not form:
            continue
        f = form.fields
        src = szfb.get(field(f, "kod"))
        if not src:
            continue
        label = f"zapas #{field(f, 'kod')} {field(f, 'domaci_kratky')} - {field(f, 'hoste_kratky')}"

        # kontrola, ze domaci/hostia su v oboch systemoch rovnako
        our_side = field(f, "nas_tym")
        expected = src["home_abbr"] if our_side == "d" else src["away_abbr"] if our_side == "h" else None
        if expected is not None and expected != SZFB_OUR_ABBR:
            log(f"PRESKAKUJEM {label}: nesedi domaci/hostia medzi szfb.sk a adminom.")
            continue

        wanted = schedule_changes(f, src)
        if src["score_home"] is not None:
            wanted.update({
                "score_home": src["score_home"],
                "score_away": src["score_away"],
                "vysledek_typ": src["result_type"],
            })
            if field(f, "game_status") != STATUS_FORFEIT:
                wanted["game_status"] = STATUS_PLAYED
            periods = szfb_periods(opener, src["url"]) if src["url"] else ""
            if periods:
                wanted["tretiny"] = periods
        diff = {k: v for k, v in wanted.items() if game_value(f, k) != v}
        if not diff:
            continue

        changed += 1
        desc = ", ".join(f"{k}: '{game_value(f, k)}' -> '{v}'" for k, v in diff.items())
        if not apply:
            log(f"[nahlad] {label}: {desc}")
            continue

        error = admin_post_form(opener, form.action, with_changes(f, diff) + [("submit_with_return", "")])
        if error:
            log(f"CHYBA {label}: admin odmietol ulozenie: {error}")
            continue
        check = admin_game_form(opener, game_id)
        bad = [k for k, v in diff.items() if not check or game_value(check.fields, k) != v]
        log(f"CHYBA {label}: po ulozeni nesedi {', '.join(bad)}" if bad
            else f"Aktualizovane {label}: {desc}")
    return changed


# --- Tabulka -----------------------------------------------------------------

def norm_name(name):
    return " ".join((name or "").split()).casefold()


def admin_standings(opener, comp):
    """Vrati riadky tabulky v admine v aktualnom poradi: [{id, name, abbr}]."""
    page = admin_request(opener, comp.admin_standings)
    rows = []
    for row_id, body in re.findall(r'<tr id="row-standings-(\d+)"(.*?)</tr>', page, re.S):
        name = re.search(r'team_name">\s*<div class="font-semibold">([^<]*)</div>', body)
        sub = re.search(r'team_name">.*?<div class="text-xs[^"]*">([^<]*)</div>', body, re.S)
        abbr = html.unescape(sub.group(1)).rsplit(" - ", 1)[-1].strip() if sub else None
        rows.append({"id": row_id, "name": html.unescape(name.group(1)) if name else "", "abbr": abbr})
    return rows


def match_standings(source, admin):
    """Sparuje riadky szfb.sk s riadkami v admine podla nazvu timu, inak podla skratky."""
    by_name = {norm_name(r["name"]): r for r in admin}
    by_abbr = {r["abbr"]: r for r in admin}
    return {i: by_name.get(norm_name(s["name"])) or by_abbr.get(s["abbr"]) for i, s in enumerate(source)}


def admin_standings_form(opener, comp, row_id):
    result = admin_ajax(opener, "get_modal", {
        "module": "games", "entity": "standings", "id": row_id,
        "payload": "", "url_back_params": comp.filter})
    return parse_form(result.get("data", ""), is_standings_form)


def sync_standings(opener, comp, results_page, apply):
    source = szfb_standings(opener, comp, szfb_team_abbrs(results_page))
    admin = admin_standings(opener, comp)
    log(f"Tabulka: szfb.sk {len(source)} timov, admin {len(admin)} timov.")
    if not source or not admin:
        log("Tabulka: na szfb.sk alebo v admine zatial nie je, preskakujem.")
        return 0

    matched = match_standings(source, admin)
    unmatched = [s["name"] for i, s in enumerate(source) if not matched[i]]
    if unmatched:
        log(f"Tabulka: tieto timy v admine chybaju, preskakujem ich: {', '.join(unmatched)}")

    changed = 0
    for i, src in enumerate(source):
        row = matched[i]
        if not row:
            continue
        form = admin_standings_form(opener, comp, row["id"])
        if not form:
            log(f"CHYBA tabulka {src['name']}: nenasiel sa formular.")
            continue
        wanted = {k: src[k] for k in STANDINGS_FIELDS + ["score_h", "score_a", "points"]}
        diff = {k: v for k, v in wanted.items() if (field(form.fields, k) or "") != v}
        if not diff:
            continue
        changed += 1
        desc = ", ".join(f"{k}: {field(form.fields, k)} -> {v}" for k, v in diff.items())
        if not apply:
            log(f"[nahlad] tabulka {src['name']}: {desc}")
            continue
        error = admin_post_form(opener, comp.admin_standings, with_changes(form.fields, diff))
        if error:
            log(f"CHYBA tabulka {src['name']}: admin odmietol ulozenie: {error}")
            continue
        check = admin_standings_form(opener, comp, row["id"])
        bad = [k for k, v in diff.items() if not check or (field(check.fields, k) or "") != v]
        log(f"CHYBA tabulka {src['name']}: po ulozeni nesedi {', '.join(bad)}" if bad
            else f"Aktualizovana tabulka {src['name']}: {desc}")

    # poradie sa nastavuje, len ked su v admine presne tie iste timy ako na szfb.sk
    if unmatched or len(source) != len(admin):
        if len(source) != len(admin):
            log(f"Tabulka: pocet timov nesedi (szfb.sk {len(source)}, admin {len(admin)}), poradie nemenim.")
        return changed
    wanted_order = [matched[i]["id"] for i in range(len(source))]
    if wanted_order != [r["id"] for r in admin]:
        changed += 1
        order_desc = ", ".join(s["name"] for s in source)
        if not apply:
            log(f"[nahlad] poradie tabulky: {order_desc}")
        else:
            admin_ajax(opener, "sortable_save", {
                "module": "games", "entity": "standings", "view": "standings",
                "items": json.dumps(wanted_order)})
            now = [r["id"] for r in admin_standings(opener, comp)]
            log(f"Aktualizovane poradie tabulky: {order_desc}" if now == wanted_order
                else "CHYBA: poradie tabulky sa po ulozeni nezmenilo.")
    return changed


# --- Hlavna logika -----------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="naozaj zapisat zmeny do adminu")
    args = ap.parse_args()

    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    jar = http.cookiejar.LWPCookieJar(COOKIE_FILE)
    if os.path.exists(COOKIE_FILE):
        jar.load(ignore_discard=True)
    opener = make_opener(jar)

    try:
        changed = 0
        for comp in COMPETITIONS:
            log(f"=== {comp.name} ===")
            _, results_page = fetch(opener, comp.results)
            if comp.games:
                changed += sync_games(opener, comp, results_page, args.apply)
            changed += sync_standings(opener, comp, results_page, args.apply)
        if changed == 0:
            log("Vsetko je aktualne.")
        elif args.apply:
            clear_web_cache(opener)
        else:
            log(f"{changed} zmien na zapis. Spusti s --apply pre zapis.")
    finally:
        jar.save(ignore_discard=True)
        os.chmod(COOKIE_FILE, 0o600)


if __name__ == "__main__":
    main()
