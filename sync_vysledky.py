#!/usr/bin/env python3
"""Synchronizácia výsledkov zápasov zo szfb.sk do administrácie klubového webu (eoscms).

Použitie:
    python3 sync_vysledky.py            # len ukáže, čo by sa zmenilo (nič nezapisuje)
    python3 sync_vysledky.py --apply    # zapíše zmeny do adminu

Prihlasovacie údaje sa čítajú z premenných prostredia EOS_EMAIL a EOS_PASSWORD
(na GitHube z Secrets), alebo z macOS Keychain (služba "tsunami-eoscms"):
    security add-generic-password -s tsunami-eoscms -a "EMAIL" -w
"""

import argparse
import html
import http.cookiejar
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
# Florbalová extraliga mužov 2026/2027 (TournamentID na szfb.sk)
SZFB_RESULTS = SZFB_BASE + "/sk/stats/results/1241/florbalova-extraliga-muzov"
SZFB_OUR_ABBR = "TSU"

ADMIN_BASE = "https://tsunamiflorbal.eoscms.cz"
ADMIN_SEASON_ID = 3   # 2026/2027
ADMIN_TEAM_ID = 25    # Muži

KEYCHAIN_SERVICE = "tsunami-eoscms"
STATE_DIR = os.path.expanduser("~/Library/Application Support/tsunami-vysledky")
COOKIE_FILE = os.path.join(STATE_DIR, "session.cookies")

USER_AGENT = "Mozilla/5.0 (Macintosh) tsunami-vysledky/1.0"

# szfb označenie -> hodnota "Hrací doba" v admine
RESULT_TYPE = {"": "", "pp": "p", "sn": "pn"}
STATUS_PLAYED = "2"


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --- Keychain ----------------------------------------------------------------

def credentials():
    """Vráti (email, heslo) z premenných prostredia (GitHub Secrets), inak z macOS Keychain."""
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
        sys.exit("Chýbajú prihlasovacie údaje: nastav EOS_EMAIL a EOS_PASSWORD, "
                 f"alebo na Macu pridaj položku '{KEYCHAIN_SERVICE}' do Keychain:\n"
                 f'  security add-generic-password -s {KEYCHAIN_SERVICE} -a "EMAIL" -w')
    m = re.search(r'"acct"<blob>="([^"]*)"', attrs)
    if not m:
        sys.exit("V Keychain položke chýba účet (email).")
    return m.group(1), password


# --- HTTP --------------------------------------------------------------------

def make_opener(jar):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener


def fetch(opener, url, data=None):
    """Vráti (finálna URL po presmerovaní, text)."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    with opener.open(url, body, timeout=30) as resp:
        return resp.geturl(), resp.read().decode("utf-8", errors="replace")


# --- szfb.sk -----------------------------------------------------------------

def text_of(fragment):
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split()


def szfb_matches(opener):
    """Vráti {kód zápasu: údaje} pre odohrané zápasy nášho tímu."""
    _, page = fetch(opener, SZFB_RESULTS)
    result = {}
    for row in page.split('<tr class="fn-tap-row')[1:]:
        code = re.search(r'<td class="hidden-xs ">\s*(\d+)\s*</td>', row)
        abbrs = re.findall(r'<span class="visible-xs competitor-img">.*?>\s*([^<\s]+)\s*</span>', row, re.S)
        score_box = re.search(r'<div class="game-clash-result text-center hidden-xs">(.*?)</div>', row, re.S)
        if not (code and len(abbrs) == 2 and score_box) or SZFB_OUR_ABBR not in abbrs:
            continue
        goals = re.findall(r'<span class="matchresult">(\d+)</span>', score_box.group(1))
        if len(goals) != 2:
            continue  # ešte neodohraný
        marker = re.search(r'font-small">(pp|sn)</span>', score_box.group(1))
        link = re.search(r'href="(/sk/stats/matches/[^"]+/match/\d+)"', score_box.group(1))
        result[code.group(1)] = {
            "home_abbr": abbrs[0],
            "away_abbr": abbrs[1],
            "score_home": goals[0],
            "score_away": goals[1],
            "result_type": RESULT_TYPE[marker.group(1) if marker else ""],
            "url": SZFB_BASE + link.group(1) if link else None,
        }
    return result


def szfb_periods(opener, match_url):
    _, page = fetch(opener, match_url + "/overview")
    m = re.search(r'data-match-text="scorePeriods">([^<]*)<', page)
    return html.unescape(m.group(1)).strip() if m else ""


# --- eoscms admin ------------------------------------------------------------

class FormParser(HTMLParser):
    """Zozbiera polia formulára na úpravu zápasu tak, ako by ich odoslal prehliadač."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.action = None
        self.fields = []          # [(name, value)] v poradí
        self._in_form = False
        self._select = None       # [name, first_value, selected_value]
        self._option_value = None
        self._textarea = None     # [name, text]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and "game_update.php" in (a.get("action") or ""):
            self._in_form = True
            self.action = a["action"]
        if not self._in_form or not a.get("name") and tag not in ("option",):
            return
        if tag == "input":
            itype = (a.get("type") or "text").lower()
            if itype in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields.append((a["name"], a.get("value", "on")))
            elif itype not in ("submit", "button", "file", "image", "reset"):
                self.fields.append((a["name"], a.get("value", "")))
        elif tag == "select":
            self._select = [a["name"], None, None]
        elif tag == "option" and self._select is not None:
            value = a.get("value")
            self._option_value = value
            if value is not None:
                if self._select[1] is None:
                    self._select[1] = value
                if "selected" in a:
                    self._select[2] = value
        elif tag == "textarea":
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


def is_login_page(url):
    return urllib.parse.urlparse(url).path.rstrip("/") == "/login"


def admin_login(opener):
    email, password = credentials()
    _, page = fetch(opener, ADMIN_BASE + "/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)
    if not token:
        sys.exit("Na prihlasovacej stránke sa nenašiel CSRF token.")
    url, _ = fetch(opener, ADMIN_BASE + "/login",
                   {"csrf_token": token.group(1), "email": email, "password": password})
    if is_login_page(url):
        sys.exit("Prihlásenie do adminu zlyhalo (skontroluj email/heslo v Keychain).")
    log("Prihlásený do adminu.")


def admin_get(opener, url):
    """GET v admine; ak session vypršala, prihlási sa znova a zopakuje požiadavku."""
    final_url, page = fetch(opener, url)
    if is_login_page(final_url):
        log("Session vypršala alebo neexistuje – prihlasujem sa.")
        admin_login(opener)
        final_url, page = fetch(opener, url)
        if is_login_page(final_url):
            sys.exit("Ani po prihlásení sa nepodarilo otvoriť admin.")
    return page


def admin_games(opener):
    list_url = (f"{ADMIN_BASE}/games/?sid={ADMIN_SEASON_ID}&tid={ADMIN_TEAM_ID}"
                "&dataview=games_all")
    page = admin_get(opener, list_url)
    ids = list(dict.fromkeys(re.findall(r'/games/\?id=(\d+)"', page)))
    games = []
    for game_id in ids:
        parser = FormParser()
        parser.feed(admin_get(opener, f"{ADMIN_BASE}/games/?id={game_id}"))
        if parser.action:
            games.append({"id": game_id, "action": parser.action, "fields": parser.fields})
    return games


def field(fields, name):
    for n, v in fields:
        if n == name:
            return v
    return None


def with_changes(fields, changes):
    return [(n, changes.get(n, v)) for n, v in fields]


# --- Hlavná logika -----------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="naozaj zapísať zmeny do adminu")
    args = ap.parse_args()

    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    jar = http.cookiejar.LWPCookieJar(COOKIE_FILE)
    if os.path.exists(COOKIE_FILE):
        jar.load(ignore_discard=True)
    opener = make_opener(jar)

    try:
        szfb = szfb_matches(opener)
        log(f"szfb.sk: {len(szfb)} odohraných zápasov Tsunami.")
        games = admin_games(opener)
        log(f"Admin: {len(games)} zápasov.")

        changed = 0
        for game in games:
            f = game["fields"]
            code = field(f, "kod")
            src = szfb.get(code)
            if not src:
                continue
            label = f"#{code} {field(f, 'domaci_kratky')} – {field(f, 'hoste_kratky')}"

            # kontrola, že domáci/hostia sú v oboch systémoch rovnako
            our_side = field(f, "nas_tym")
            expected = src["home_abbr"] if our_side == "d" else src["away_abbr"] if our_side == "h" else None
            if expected is not None and expected != SZFB_OUR_ABBR:
                log(f"PRESKAKUJEM {label}: nesedí domáci/hostia medzi szfb.sk a adminom.")
                continue

            src["periods"] = szfb_periods(opener, src["url"]) if src["url"] else ""
            wanted = {
                "score_home": src["score_home"],
                "score_away": src["score_away"],
                "vysledek_typ": src["result_type"],
                "game_status": STATUS_PLAYED,
            }
            if src["periods"]:
                wanted["tretiny"] = src["periods"]
            diff = {k: v for k, v in wanted.items() if (field(f, k) or "") != v}
            if not diff:
                continue

            changed += 1
            desc = ", ".join(f"{k}: '{field(f, k) or ''}' → '{v}'" for k, v in diff.items())
            if not args.apply:
                log(f"[náhľad] {label}: {desc}")
                continue

            post = with_changes(f, diff) + [("submit_with_return", "")]
            body = urllib.parse.urlencode(post).encode()
            with opener.open(game["action"], body, timeout=30) as resp:
                if is_login_page(resp.geturl()):
                    sys.exit("Session vypršala počas zápisu – spusti znova.")
            # overenie
            check = FormParser()
            check.feed(admin_get(opener, f"{ADMIN_BASE}/games/?id={game['id']}"))
            bad = [k for k, v in diff.items() if (field(check.fields, k) or "") != v]
            if bad:
                log(f"CHYBA {label}: po uložení nesedí {', '.join(bad)}")
            else:
                log(f"Aktualizované {label}: {desc}")

        if changed == 0:
            log("Všetko je aktuálne.")
        elif not args.apply:
            log(f"{changed} zápas(ov) na aktualizáciu. Spusti s --apply pre zápis.")
    finally:
        jar.save(ignore_discard=True)
        os.chmod(COOKIE_FILE, 0o600)


if __name__ == "__main__":
    main()
