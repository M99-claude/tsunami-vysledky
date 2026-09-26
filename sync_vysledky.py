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
# Florbalova extraliga muzov 2026/2027 (TournamentID na szfb.sk)
SZFB_RESULTS = SZFB_BASE + "/sk/stats/results/1241/florbalova-extraliga-muzov"
SZFB_STANDINGS = SZFB_BASE + "/sk/stats/standings/1241/florbalova-extraliga-muzov"
SZFB_OUR_ABBR = "TSU"

ADMIN_BASE = "https://tsunamiflorbal.eoscms.cz"
ADMIN_SEASON_ID = 3   # 2026/2027
ADMIN_TEAM_ID = 25    # Muzi
ADMIN_FILTER = f"sid={ADMIN_SEASON_ID}&tid={ADMIN_TEAM_ID}"
ADMIN_GAMES = f"{ADMIN_BASE}/games/?{ADMIN_FILTER}&dataview=games_all"
ADMIN_STANDINGS = f"{ADMIN_BASE}/games/standings/?{ADMIN_FILTER}"

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


def szfb_standings(opener, abbrs):
    """Vrati tabulku ako zoznam riadkov v poradi: {abbr, name, gp, ..., points}."""
    _, page = fetch(opener, SZFB_STANDINGS)
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


def admin_ajax(opener, name, params):
    if not _csrf["token"]:
        admin_request(opener, ADMIN_STANDINGS)
    page = admin_request(opener, f"{ADMIN_BASE}/ajax/{name}.php",
                         dict(params, csrf_token=_csrf["token"]))
    try:
        result = json.loads(page)
    except ValueError:
        sys.exit(f"Neocakavana odpoved z ajax/{name}.php")
    if not result.get("ok"):
        sys.exit(f"Chyba z ajax/{name}.php: {result.get('error')}")
    return result


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


def sync_games(opener, results_page, apply):
    szfb = szfb_matches(results_page)
    played = sum(1 for m in szfb.values() if m["score_home"] is not None)
    log(f"szfb.sk: {len(szfb)} zapasov Tsunami, z toho {played} odohranych.")
    ids = list(dict.fromkeys(re.findall(r'/games/\?id=(\d+)"', admin_request(opener, ADMIN_GAMES))))
    log(f"Admin: {len(ids)} zapasov.")
    if not ids:
        sys.exit("CHYBA: v admine sa nenasli ziadne zapasy.")

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

        admin_request(opener, form.action, with_changes(f, diff) + [("submit_with_return", "")])
        check = admin_game_form(opener, game_id)
        bad = [k for k, v in diff.items() if not check or game_value(check.fields, k) != v]
        log(f"CHYBA {label}: po ulozeni nesedi {', '.join(bad)}" if bad
            else f"Aktualizovane {label}: {desc}")
    return changed


# --- Tabulka -----------------------------------------------------------------

def admin_standings(opener):
    """Vrati riadky tabulky v admine v aktualnom poradi: [{id, abbr}]."""
    page = admin_request(opener, ADMIN_STANDINGS)
    rows = []
    for row_id, body in re.findall(r'<tr id="row-standings-(\d+)"(.*?)</tr>', page, re.S):
        team = re.search(r'team_name">.*?<div class="text-xs[^"]*">([^<]*)</div>', body, re.S)
        abbr = html.unescape(team.group(1)).rsplit(" - ", 1)[-1].strip() if team else None
        rows.append({"id": row_id, "abbr": abbr})
    return rows


def admin_standings_form(opener, row_id):
    result = admin_ajax(opener, "get_modal", {
        "module": "games", "entity": "standings", "id": row_id,
        "payload": "", "url_back_params": ADMIN_FILTER})
    return parse_form(result.get("data", ""), is_standings_form)


def sync_standings(opener, results_page, apply):
    source = szfb_standings(opener, szfb_team_abbrs(results_page))
    admin = admin_standings(opener)
    log(f"Tabulka: szfb.sk {len(source)} timov, admin {len(admin)} timov.")
    if not source or not admin:
        sys.exit("CHYBA: tabulka je prazdna na szfb.sk alebo v admine.")
    by_abbr = {r["abbr"]: r for r in admin}

    unmatched = [s["name"] for s in source if s["abbr"] not in by_abbr]
    if unmatched or len(source) != len(admin):
        log(f"PRESKAKUJEM tabulku: nepodarilo sa sparovat timy {unmatched or ''} "
            f"(szfb.sk {len(source)}, admin {len(admin)}).")
        return 0

    changed = 0
    for src in source:
        row = by_abbr[src["abbr"]]
        form = admin_standings_form(opener, row["id"])
        if not form:
            log(f"CHYBA tabulka {src['abbr']}: nenasiel sa formular.")
            continue
        wanted = {k: src[k] for k in STANDINGS_FIELDS + ["score_h", "score_a", "points"]}
        diff = {k: v for k, v in wanted.items() if (field(form.fields, k) or "") != v}
        if not diff:
            continue
        changed += 1
        desc = ", ".join(f"{k}: {field(form.fields, k)} -> {v}" for k, v in diff.items())
        if not apply:
            log(f"[nahlad] tabulka {src['abbr']}: {desc}")
            continue
        admin_request(opener, ADMIN_STANDINGS, with_changes(form.fields, diff))
        check = admin_standings_form(opener, row["id"])
        bad = [k for k, v in diff.items() if not check or (field(check.fields, k) or "") != v]
        log(f"CHYBA tabulka {src['abbr']}: po ulozeni nesedi {', '.join(bad)}" if bad
            else f"Aktualizovana tabulka {src['abbr']}: {desc}")

    wanted_order = [by_abbr[s["abbr"]]["id"] for s in source]
    if wanted_order != [r["id"] for r in admin]:
        changed += 1
        order_desc = " ".join(s["abbr"] for s in source)
        if not apply:
            log(f"[nahlad] poradie tabulky: {order_desc}")
        else:
            admin_ajax(opener, "sortable_save", {
                "module": "games", "entity": "standings", "view": "standings",
                "items": json.dumps(wanted_order)})
            now = [r["id"] for r in admin_standings(opener)]
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
        _, results_page = fetch(opener, SZFB_RESULTS)
        changed = sync_games(opener, results_page, args.apply)
        changed += sync_standings(opener, results_page, args.apply)
        if changed == 0:
            log("Vsetko je aktualne.")
        elif not args.apply:
            log(f"{changed} zmien na zapis. Spusti s --apply pre zapis.")
    finally:
        jar.save(ignore_discard=True)
        os.chmod(COOKIE_FILE, 0o600)


if __name__ == "__main__":
    main()
