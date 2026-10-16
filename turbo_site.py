"""
Rankade stegar för Dota 2: Turbo och Single Draft (prototyp 4).

Starta:   dubbelklicka på starta.bat (Windows)   eller   python3 turbo_site.py (Mac)
Öppna:    http://localhost:8000

Ligger i samma mapp som turbo_rank.py (den återanvänds för API-anropen).
Bara standardbibliotek. Data sparas i turbo.db (SQLite) bredvid filen.

Säsonger: varje kalendermånad (UTC) är en egen säsong där alla börjar om på 1500.
Topplistorna visar bara spelare som anmält sig. Man får vara med i båda stegarna.
Texten på sajten är på engelska, kommentarerna i koden på svenska.
"""
import calendar
import hashlib
import html
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from turbo_rank import (FÖRVÄNTAN_VIKT, GULD_BONUS, GULD_MAX, GULD_STREAK, K, KDA_MAX, KDA_NORMAL, SINGLE_DRAFT,
                        START, TURBO, guldbonus, hämta_profil, motståndarnivå, rank_namn, ApiFel, förklara, giltig,
                        hämta_json, hämta_matcher, inga_matcher_fel, månad_för,
                        månadsgränser, tolka_id, vann, STEAM64_BAS)

# TURBO_DB pekar ut databasfilen på en server (en mapp som sparas mellan omstarter).
# Hos Railway läggs den automatiskt på den anslutna disken (volymen).
_volym = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
DB = (os.environ.get("TURBO_DB") or (os.path.join(_volym, "turbo.db") if _volym else "")
      or os.path.join(os.path.dirname(os.path.abspath(__file__)), "turbo.db"))
# Värdtjänster anger porten i PORT; lokalt gäller 8000.
PORT = int(os.environ.get("TURBO_PORT") or os.environ.get("PORT") or "8000")
MIN_MATCHER = 10   # färre än så under månaden = "calibrating", hamnar sist
PAUS = 60          # sekunder mellan uppdateringar av samma spelare (sparar API-anrop)
# Sajtens adress utåt. Lokalt är det localhost; på nätet sätts TURBO_URL till
# t.ex. https://min-sajt.se. Steam skickar tillbaka spelaren hit efter inloggning.
# Hos Railway hämtas adressen automatiskt från den domän tjänsten har fått.
_domän = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
BAS = (os.environ.get("TURBO_URL") or (f"https://{_domän}" if _domän else "")
       or f"http://localhost:{PORT}").rstrip("/")
LOKALT = urlsplit(BAS).hostname in ("localhost", "127.0.0.1")
# Lokalt nås sajten bara från den egna datorn; på nätet måste den lyssna utåt.
VÄRD = os.environ.get("TURBO_HOST") or ("127.0.0.1" if LOKALT else "0.0.0.0")
STEAM_OPENID = os.environ.get("TURBO_STEAM_OPENID", "https://steamcommunity.com/openid/login")
SESSION_DAGAR = 30          # så länge gäller en inloggning
# Automatisk uppdatering: när någon öppnar en sida hämtas nya matcher i bakgrunden
# för de spelare sidan visar, om deras data är äldre än så här (sekunder).
FÄRSK_PROFIL = 10 * 60      # spelaren vars profil öppnas
FÄRSK_LISTA = 30 * 60       # anmälda spelare när menyn eller en topplista öppnas
MELLANRUM = 3               # sekunders paus mellan två spelare (OpenDota: ca 60 anrop/minut)
DYGNSBUDGET = 1500          # högst så många anrop per dygn i bakgrunden (gratisgräns 2000)
ANROP_PER_SPELARE = 3       # två spellägen + profilen
VILA_EFTER_FEL = 5 * 60     # paus i bakgrundsuppdateringen efter ett fel från OpenDota
# Bestämd första guldhjälte för en viss spelare en viss månad: (Steam32-ID, månad) -> hjälte-id.
# Resten av ordningen är slumpad som vanligt. 32 = Riki.
FÖRSTA_GULDHJÄLTE = {(168893053, "2026-10"): 32}
# Efter så här många förluster med samma guldhjälte hoppas den över automatiskt
# och nästa hjälte i ordningen blir guldhjälte.
GULD_HOPPA_FÖRLUSTER = 2
# Omval av guldhjälten (Turbo). Man börjar månaden med ett omval och kan aldrig ha fler än
# OMVAL_MAX. Ett nytt omval fås efter OMVAL_MATCHER Turbo-matcher medan samma hjälte är
# guldhjälte.
OMVAL_MAX = 1
OMVAL_MATCHER = 5
# Första pris som visas på topplistorna. Bilden är en kista från Dota 2 och hämtas
# från Steams bildserver. Byt text eller bild här.
PRIS_TEXT = "A Dota 2 treasure chest"
PRIS_BILD = ("https://community.cloudflare.steamstatic.com/economy/image/"
             "i0CoZ81Ui0m-9KwlBY1L_18myuGuq1wfhWSaZgMttydbPaERSR0Wqmu7LAoc"
             "GJG51EejH-LGxsuyIGmS2llzy4Px613YUxjylZnu9CxcoaX8PadodKSQXT6U"
             "mLsi4uM5TXjlkR9ytm3Xmdz6di_CPAIjW5t5TO4U8k7vczduM-k/256fx256f")
SAJTNAMN = "Ranked Ladders"
# Spellägen: adressdel -> (OpenDotas game_mode, namn, kort beskrivning på menyn)
LÄGEN = {"turbo": (TURBO, "Turbo", "Fast games, quick gold and experience."),
         "sd": (SINGLE_DRAFT, "Single Draft", "Pick from four random heroes.")}
BILDER = "https://cdn.cloudflare.steamstatic.com"   # här ligger Dotas hjältebilder
HJÄLTE_ÅLDER = 7 * 24 * 3600   # hämta om hjältelistan en gång i veckan (nya hjältar)
MÅNADER = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


# ---------- månader ----------
def nu_månad():
    return månad_för(time.time())


def tolka_månad(text):
    """Giltig 'ÅÅÅÅ-MM' som inte ligger i framtiden, annars innevarande månad."""
    if text and re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", text) and text <= nu_månad():
        return text
    return nu_månad()


def månadsnamn(månad):
    return f"{MÅNADER[int(månad[5:7]) - 1]} {månad[:4]}"


def granne(månad, steg):
    """Månaden före (steg=-1) eller efter (steg=+1)."""
    n = int(månad[:4]) * 12 + int(månad[5:7]) - 1 + steg
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def månadsväljare(månad, bas):
    """Knappar för att bläddra mellan månader. bas = sidans adress utan ?month."""
    fram = "<span class='step off' aria-hidden='true'>&rsaquo;</span>"
    nu = ""
    if månad < nu_månad():
        fram = (f"<a class='step' href='{bas}?month={granne(månad, 1)}' "
                f"aria-label='{månadsnamn(granne(månad, 1))}'>&rsaquo;</a>")
        nu = f"<a class='now' href='{bas}'>Back to this month</a>"
    return (f"<div class='season'><a class='step' href='{bas}?month={granne(månad, -1)}' "
            f"aria-label='{månadsnamn(granne(månad, -1))}'>&lsaquo;</a>"
            f"<span class='month'>{månadsnamn(månad)}</span>{fram}{nu}</div>")


# ---------- databas ----------
def db():
    c = sqlite3.connect(DB, timeout=10)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS players(
            id INTEGER PRIMARY KEY, name TEXT, updated INTEGER);
        CREATE TABLE IF NOT EXISTS matches(
            player_id INTEGER, match_id INTEGER, start_time INTEGER,
            win INTEGER, hero_id INTEGER, kills INTEGER, deaths INTEGER,
            assists INTEGER, PRIMARY KEY(player_id, match_id));
    """)
    c.execute("CREATE TABLE IF NOT EXISTS heroes(id INTEGER PRIMARY KEY, name TEXT, "
              "img TEXT, icon TEXT, fetched INTEGER)")
    # mode = OpenDotas game_mode. Äldre databaser har bara Turbo-matcher.
    if "mode" not in [r[1] for r in c.execute("PRAGMA table_info(matches)")]:
        c.execute(f"ALTER TABLE matches ADD COLUMN mode INTEGER NOT NULL DEFAULT {TURBO}")
        c.commit()
    # avg_rank = lobbyns snittrank enligt OpenDota, rank_tier = spelarens egen rank.
    if "avg_rank" not in [r[1] for r in c.execute("PRAGMA table_info(matches)")]:
        c.execute("ALTER TABLE matches ADD COLUMN avg_rank INTEGER")
        c.execute("ALTER TABLE players ADD COLUMN rank_tier INTEGER")
        c.commit()
    # gold_order = spelarens slumpade hjälteordning för månaden (Turbo), som JSON-lista.
    c.execute("CREATE TABLE IF NOT EXISTS gold_order(player_id INTEGER, month TEXT, "
              "heroes TEXT, PRIMARY KEY(player_id, month))")
    # gold_repick = när spelaren valde om sin guldhjälte (Turbo), en rad per omval.
    c.execute("CREATE TABLE IF NOT EXISTS gold_repick(player_id INTEGER, month TEXT, "
              "time INTEGER)")
    # sessions = inloggningar. Bara en hash av kakans värde sparas, aldrig värdet självt.
    c.execute("CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, "
              "player_id INTEGER, expires INTEGER)")
    # entries = anmälningar: en rad per spelare, månad och stege.
    # active = 0 betyder att spelaren har lämnat den stegens topplista.
    c.execute("CREATE TABLE IF NOT EXISTS entries(player_id INTEGER, month TEXT, "
              "mode INTEGER, active INTEGER NOT NULL DEFAULT 1, "
              "PRIMARY KEY(player_id, month, mode))")
    # Äldre databaser tillät bara en stege per månad. Bygg om tabellen så att
    # samma spelare kan vara anmäld till båda, och behåll alla anmälningar.
    if sum(1 for r in c.execute("PRAGMA table_info(entries)") if r[5]) < 3:
        c.executescript("""
            BEGIN;
            CREATE TABLE entries_ny(player_id INTEGER, month TEXT, mode INTEGER,
                active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(player_id, month, mode));
            INSERT OR IGNORE INTO entries_ny SELECT player_id,month,mode,active FROM entries;
            DROP TABLE entries;
            ALTER TABLE entries_ny RENAME TO entries;
            COMMIT;
        """)
    # listed användes förr (en topplista för alla lägen) och läses inte längre.
    # listed = 1 när spelaren har valt att synas på topplistan.
    # Äldre databaser saknar kolumnen, så den läggs till vid behov.
    if "listed" not in [r[1] for r in c.execute("PRAGMA table_info(players)")]:
        c.execute("ALTER TABLE players ADD COLUMN listed INTEGER NOT NULL DEFAULT 0")
        c.commit()
    return c


def uppdatera_hjältar(c):
    """Hämtar hjältarnas namn och bilder från OpenDota om listan saknas eller är gammal.
    Misslyckas det gör det inget: sajten visar då 'Hero 12' i stället."""
    ålder = c.execute("SELECT MIN(fetched) FROM heroes").fetchone()[0]
    if ålder and time.time() - ålder < HJÄLTE_ÅLDER:
        return
    try:
        data = hämta_json("/constants/heroes")
        rader = [(int(h["id"]), h.get("localized_name") or f"Hero {h['id']}",
                  h.get("img") or "", h.get("icon") or "", int(time.time()))
                 for h in data.values()]
    except (ApiFel, AttributeError, KeyError, TypeError, ValueError):
        return
    if rader:
        c.executemany("INSERT OR REPLACE INTO heroes VALUES(?,?,?,?,?)", rader)
        c.commit()


def hjältar(c):
    """{hero_id: (namn, bild, ikon)}"""
    return {i: (n, img, icon) for i, n, img, icon in
            c.execute("SELECT id,name,img,icon FROM heroes")}


def hjälte_html(hj, hero_id, med_namn=True):
    """Hjältens porträtt (och namn). Okänd hjälte visas som 'Hero 12'."""
    if hero_id is None:
        return "<span class='dim'>Unknown hero</span>"
    namn, img, _ = hj.get(hero_id, (f"Hero {hero_id}", "", ""))
    namn = html.escape(namn)
    bild = (f"<img class='hero' src='{BILDER}{html.escape(img)}' alt='' loading='lazy' "
            f"width='57' height='32' onerror=\"this.style.display='none'\">") if img else ""
    if med_namn:
        return f"<span class='herocell'>{bild}<span>{namn}</span></span>"
    return (f"<span title='{namn}'>{bild}</span>" if bild
            else f"<span class='dim'>{namn}</span>")


def lägesnyckel(game_mode):
    return next(k for k, v in LÄGEN.items() if v[0] == game_mode)


def anmälan(c, pid, månad, läge):
    """True om spelaren är anmäld till stegen den månaden, False om hen har lämnat
    den, None om hen aldrig anmält sig."""
    rad = c.execute("SELECT active FROM entries WHERE player_id=? AND month=? AND mode=?",
                    (pid, månad, LÄGEN[läge][0])).fetchone()
    return bool(rad[0]) if rad else None


def anmäl(c, pid, läge):
    """Anmäler spelaren till stegen för innevarande månad."""
    c.execute("INSERT INTO entries(player_id,month,mode,active) VALUES(?,?,?,1) "
              "ON CONFLICT(player_id,month,mode) DO UPDATE SET active=1",
              (pid, nu_månad(), LÄGEN[läge][0]))
    c.commit()


def lämna(c, pid, läge):
    """Tar bort spelaren från den stegens topplista den här månaden."""
    c.execute("UPDATE entries SET active=0 WHERE player_id=? AND month=? AND mode=?",
              (pid, nu_månad(), LÄGEN[läge][0]))
    c.commit()


def uppdatera_spelare(c, pid):
    """Hämtar matcher i alla spellägen och sparar nya. Returnerar antal nya matcher."""
    gammal = c.execute("SELECT name,updated FROM players WHERE id=?", (pid,)).fetchone()
    if gammal and gammal[1] and time.time() - gammal[1] < PAUS:
        kvar = int(PAUS - (time.time() - gammal[1])) + 1
        raise ApiFel(f"This player was just updated. Try again in {kvar} seconds.")

    rader = []
    for game_mode, _, _ in LÄGEN.values():          # ett anrop per spelläge
        rader += [(pid, m["match_id"], m["start_time"], int(vann(m)), m.get("hero_id"),
                   m.get("kills"), m.get("deaths"), m.get("assists"), game_mode,
                   m.get("average_rank"))
                  for m in hämta_matcher(pid, game_mode) if giltig(m)]
    if not rader:
        raise inga_matcher_fel(pid, " or ".join(n for _, n, _ in LÄGEN.values()))
    uppdatera_hjältar(c)

    # Namnet är bara kosmetika: går det inte att hämta behåller vi det gamla.
    try:
        namn, rank = hämta_profil(pid)
    except ApiFel:
        namn, rank = None, None
    namn = namn or (gammal[0] if gammal else None) or str(pid)

    före = c.execute("SELECT COUNT(*) FROM matches WHERE player_id=?", (pid,)).fetchone()[0]
    # Finns matchen redan fylls bara lobbyns snittrank på (den saknas i äldre databaser).
    c.executemany("INSERT INTO matches(player_id,match_id,start_time,win,hero_id,"
                  "kills,deaths,assists,mode,avg_rank) VALUES(?,?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(player_id,match_id) DO UPDATE SET "
                  "avg_rank=COALESCE(excluded.avg_rank, avg_rank)", rader)
    c.execute("INSERT INTO players(id,name,updated) VALUES(?,?,?) "
              "ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated=excluded.updated",
              (pid, namn, int(time.time())))
    if rank is not None:        # behåll den gamla ranken om OpenDota inte svarar med någon
        c.execute("UPDATE players SET rank_tier=? WHERE id=?", (rank, pid))
    c.commit()
    efter = c.execute("SELECT COUNT(*) FROM matches WHERE player_id=?", (pid,)).fetchone()[0]
    return efter - före


# ---------- inloggning med Steam (OpenID 2.0) ----------
# Spelaren loggar in på Steams egen sida. Vi får aldrig se lösenordet, bara ett
# svar som säger vilket Steam-ID som loggade in, och det svaret kontrolleras hos Steam.
_begäran = threading.local()        # vem som är inloggad i den förfrågan som hanteras nu
_nonce_lås = threading.Lock()
_använda_nonce = {}                 # svar från Steam som redan använts (mot återanvändning)
OPENID_NS = "http://specs.openid.net/auth/2.0"


def inloggad():
    """Steam32-ID för den som är inloggad i pågående förfrågan, annars None."""
    return getattr(_begäran, "pid", None)


def säker_väg(text):
    """Bara lokala adresser som '/turbo' godtas som mål efter inloggning."""
    return text if text and text.startswith("/") and not text.startswith("//") \
        and "\\" not in text else "/"


def steam_adress(nästa):
    """Adressen till Steams inloggningssida, som skickar tillbaka spelaren till oss."""
    return STEAM_OPENID + "?" + urlencode({
        "openid.ns": OPENID_NS, "openid.mode": "checkid_setup",
        "openid.return_to": f"{BAS}/login/return?next={quote(säker_väg(nästa), safe='/')}",
        "openid.realm": BAS,
        "openid.identity": OPENID_NS + "/identifier_select",
        "openid.claimed_id": OPENID_NS + "/identifier_select"})


def kontrollera_steam(q):
    """Kontrollerar Steams svar. Returnerar Steam32-ID, eller kastar ApiFel."""
    fel = ApiFel("Steam sign-in could not be verified. Please try again.")
    p = {k: v[0] for k, v in q.items() if k.startswith("openid.")}
    if p.get("openid.mode") == "cancel":
        raise ApiFel("Steam sign-in was cancelled.")
    m = re.fullmatch(r"https://steamcommunity\.com/openid/id/(\d{17})",
                     p.get("openid.claimed_id", ""))
    # Svaret måste gälla just den här sajten och komma från Steam.
    if (p.get("openid.mode") != "id_res" or not m
            or not p.get("openid.return_to", "").startswith(BAS + "/login/return")
            or p.get("openid.op_endpoint") != "https://steamcommunity.com/openid/login"):
        raise fel
    # Varje svar får användas en gång, och bara inom fem minuter.
    nonce = p.get("openid.response_nonce", "")
    try:
        ålder = time.time() - calendar.timegm(time.strptime(nonce[:20],
                                                              "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        raise fel
    with _nonce_lås:
        for gammal in [n for n, t in _använda_nonce.items() if time.time() - t > 600]:
            del _använda_nonce[gammal]
        if abs(ålder) > 300 or nonce in _använda_nonce:
            raise fel
        _använda_nonce[nonce] = time.time()
    # Fråga Steam om svaret är äkta. Utan detta kunde vem som helst förfalska en inloggning.
    p["openid.mode"] = "check_authentication"
    try:
        req = urllib.request.Request(STEAM_OPENID, data=urlencode(p).encode(),
                                     headers={"User-Agent": "turbo-rank-prototyp"})
        with urllib.request.urlopen(req, timeout=20) as r:
            svar = r.read(4000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        raise ApiFel("Could not reach Steam to verify the sign-in. Please try again.")
    if "is_valid:true" not in svar.split():
        raise fel
    return int(m.group(1)) - STEAM64_BAS


def skapa_session(c, pid):
    """Skapar en inloggning och returnerar kakans värde."""
    token = secrets.token_urlsafe(32)
    c.execute("DELETE FROM sessions WHERE expires<?", (int(time.time()),))
    c.execute("INSERT INTO sessions VALUES(?,?,?)",
              (hashlib.sha256(token.encode()).hexdigest(), pid,
               int(time.time()) + SESSION_DAGAR * 86400))
    c.commit()
    return token


def kaka(värde, sekunder):
    """Set-Cookie-raden. HttpOnly = skript på sidan kan inte läsa kakan."""
    return (f"session={värde}; Path=/; Max-Age={sekunder}; HttpOnly; SameSite=Lax"
            + ("; Secure" if BAS.startswith("https://") else ""))


def läs_session(c, kakrad):
    """Steam32-ID för kakan i förfrågan, eller None."""
    try:
        token = SimpleCookie(kakrad or "")["session"].value
    except Exception:      # ingen eller trasig kaka
        return None
    rad = c.execute("SELECT player_id FROM sessions WHERE token_hash=? AND expires>?",
                    (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
    return rad[0] if rad else None


# ---------- automatisk uppdatering i bakgrunden ----------
_lås = threading.Lock()
_kö = []                    # spelar-id som väntar på uppdatering
_tillstånd = {"arbetar": False, "vila_till": 0, "dag": "", "anrop": 0}


def väntar():
    """Antal spelare som väntar på (eller håller på med) uppdatering."""
    with _lås:
        return len(_kö)


def köa_uppdatering(c, pids, färsk):
    """Lägger spelare vars data är äldre än `färsk` sekunder i kön och startar
    bakgrundsarbetet om det inte redan går. Sidan som visas väntar aldrig på detta."""
    nu = time.time()
    gamla = [pid for pid in pids
             if nu - (c.execute("SELECT updated FROM players WHERE id=?",
                                (pid,)).fetchone() or [0])[0] > färsk]
    with _lås:
        if nu < _tillstånd["vila_till"]:
            return
        _kö.extend(pid for pid in gamla if pid not in _kö)
        if _kö and not _tillstånd["arbetar"]:
            _tillstånd["arbetar"] = True
            threading.Thread(target=_arbeta, daemon=True).start()


def _arbeta():
    """Bakgrundstråd: uppdaterar en spelare i taget tills kön är tom."""
    try:
        with closing(db()) as c:            # egen databasanslutning för tråden
            while True:
                with _lås:
                    dag = time.strftime("%Y-%m-%d", time.gmtime())
                    if _tillstånd["dag"] != dag:            # nytt dygn, ny budget
                        _tillstånd.update(dag=dag, anrop=0)
                    if not _kö or _tillstånd["anrop"] + ANROP_PER_SPELARE > DYGNSBUDGET:
                        _kö.clear()
                        return
                    pid = _kö[0]
                    _tillstånd["anrop"] += ANROP_PER_SPELARE
                try:
                    uppdatera_spelare(c, pid)
                    fel = False
                except ApiFel as e:
                    fel = "just updated" not in str(e)      # nyss uppdaterad är inget fel
                except sqlite3.Error:
                    fel = True
                with _lås:
                    if pid in _kö:
                        _kö.remove(pid)
                    if fel:                 # t.ex. HTTP 429 eller nätverksfel: vila en stund
                        _tillstånd["vila_till"] = time.time() + VILA_EFTER_FEL
                        _kö.clear()
                        return
                time.sleep(MELLANRUM)
    finally:
        with _lås:
            _tillstånd["arbetar"] = False


def anmälda(c, läge=None):
    """Id för spelare anmälda den här månaden (i ett visst spelläge eller alla)."""
    if läge:
        return [r[0] for r in c.execute(
            "SELECT player_id FROM entries WHERE month=? AND mode=? AND active=1",
            (nu_månad(), LÄGEN[läge][0]))]
    return [r[0] for r in c.execute(
        "SELECT DISTINCT player_id FROM entries WHERE month=? AND active=1", (nu_månad(),))]


def med_status(sidan):
    """Lägger till en rad och automatisk omladdning medan bakgrundsuppdatering pågår."""
    n = väntar()
    if not n:
        return sidan
    rad = (f"<p class='live' role='status'>Fetching new matches for {n} "
           f"player{'s' if n != 1 else ''} in the background. This page reloads by itself."
           f"</p>")
    return (sidan.replace("<head>", "<head><meta http-equiv='refresh' content='8'>", 1)
            .replace("</header>", "</header>" + rad, 1))


# ---------- rating ----------
def guldordning(c, pid, månad):
    """Spelarens slumpade hjälteordning för månaden. Skapas första gången och sparas,
    så att den inte ändras om Dota får en ny hjälte mitt i månaden. [] om hjältar saknas."""
    rad = c.execute("SELECT heroes FROM gold_order WHERE player_id=? AND month=?",
                    (pid, månad)).fetchone()
    if rad:
        ordning = json.loads(rad[0])
    else:
        ordning = [r[0] for r in c.execute("SELECT id FROM heroes ORDER BY id")]
        if not ordning:
            return []
        random.Random().shuffle(ordning)
    # En bestämd första guldhjälte flyttas längst fram, även i en redan sparad ordning.
    först = FÖRSTA_GULDHJÄLTE.get((pid, månad))
    ändrad = bool(först in ordning and ordning[0] != först)
    if ändrad:
        ordning.remove(först)
        ordning.insert(0, först)
    if ändrad or not rad:
        c.execute("INSERT OR REPLACE INTO gold_order VALUES(?,?,?)",
                  (pid, månad, json.dumps(ordning)))
        c.commit()
    return ordning


def spela_månad(c, pid, månad, läge):
    """Räknar igenom spelarens matcher i spelläget under månaden, äldsta först.
    Returnerar (matcher med MMR före/efter, nuvarande guldhjälte, guldvinster i rad,
    omval: {"kvar": omval att använda, "förluster"/"spelade": räknat för nuvarande guldhjälte}).
    Omval läggs in i tidsordning mellan matcherna. Ett omval utan omval kvar hoppas över."""
    start, slut = månadsgränser(månad)
    rader = c.execute("""SELECT match_id,start_time,win,hero_id,kills,deaths,assists,avg_rank
        FROM matches WHERE player_id=? AND mode=? AND start_time>=? AND start_time<?
        ORDER BY start_time, match_id""", (pid, LÄGEN[läge][0], start, slut)).fetchall()
    egen = c.execute("SELECT rank_tier FROM players WHERE id=?", (pid,)).fetchone()
    egen = egen[0] if egen else None
    ordning = guldordning(c, pid, månad) if läge == "turbo" else []
    byten = [t for (t,) in c.execute("SELECT time FROM gold_repick WHERE player_id=? AND "
                                     "month=? ORDER BY time", (pid, månad))] if ordning else []
    nr, streak = 0, 0                       # plats i hjälteordningen, guldvinster i rad
    omval = {"kvar": OMVAL_MAX, "förluster": 0, "spelade": 0}

    def välj_om(till):
        """Använder omval som gjordes före tiden `till`: nästa hjälte i ordningen blir guld."""
        nonlocal nr
        while byten and byten[0] <= till:
            byten.pop(0)
            if omval["kvar"]:
                nr += 1
                omval.update(kvar=omval["kvar"] - 1, förluster=0, spelade=0)

    r, ut = START, []                       # varje månad börjar om på START
    for m, t, w, hero, k, d, a, lobby in rader:
        välj_om(t)
        e = förklara(r, w, k, d, a, motståndarnivå(lobby, egen))
        e.update(lobby=lobby, egen=egen, guld=None, bonus=0.0, streak=0, hoppad=False)
        if ordning and hero == ordning[nr % len(ordning)]:
            e["guld"] = hero
            if w:                           # vinst med guldhjälten: bonus och ny guldhjälte
                streak += 1
                e["bonus"], e["streak"] = guldbonus(streak), streak
                e["ändring"] += e["bonus"]
                e["efter"] += e["bonus"]
                nr += 1
                omval.update(förluster=0, spelade=0)
            else:                           # förlust med guldhjälten bryter sviten
                streak = 0
                omval["förluster"] += 1
                if omval["förluster"] >= GULD_HOPPA_FÖRLUSTER:
                    e["hoppad"] = True      # för många förluster: nästa hjälte blir guld
                    nr += 1
                    omval.update(förluster=0, spelade=0)
        if ordning and not (e["guld"] is not None and (w or e["hoppad"])):
            omval["spelade"] += 1           # ännu en match med samma guldhjälte
            if omval["spelade"] % OMVAL_MATCHER == 0:
                omval["kvar"] = min(OMVAL_MAX, omval["kvar"] + 1)
        ut.append({"id": m, "tid": t, "win": w, "hero": hero, "k": k, "d": d, "a": a,
                   "e": e})
        r = e["efter"]
    välj_om(float("inf"))                   # omval efter månadens senaste match
    return ut, (ordning[nr % len(ordning)] if ordning else None), streak, omval


def månadens_matcher(c, pid, månad, läge):
    return spela_månad(c, pid, månad, läge)[0]


def spelarstat(c, pid, månad, läge):
    ms = månadens_matcher(c, pid, månad, läge)
    n, v = len(ms), sum(m["win"] for m in ms)
    return {"rating": round(ms[-1]["e"]["efter"]) if ms else START, "n": n, "v": v,
            "f": n - v, "wr": round(100 * v / n, 1) if n else 0, "matcher": ms}


# ---------- sidor ----------
CSS = """
:root{--bg:#0f1820;--panel:#16232d;--line:#243542;--text:#e7edf0;--dim:#8ea2ae;
--gold:#e5b450;--radiant:#74d195;--dire:#e96a55;--link:#9fd4e8}
*{box-sizing:border-box}
body{font-family:Barlow,"Segoe UI",system-ui,sans-serif;font-size:16px;line-height:1.5;
color:var(--text);margin:0;padding:0 16px 48px;
background:var(--bg) linear-gradient(115deg,#13303a 0,#0f1820 38%,#0f1820 62%,#2a1a1c 100%) fixed}
main{max-width:880px;margin:0 auto}
a{color:var(--link);text-decoration:none} a:hover{text-decoration:underline}
a:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid var(--gold);
outline-offset:2px}
.num,h1,h2,.month,.brand{font-family:"Barlow Condensed",Barlow,"Segoe UI",sans-serif}
.top{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;padding:22px 0 8px;
border-bottom:2px solid var(--line);margin-bottom:22px}
.brand{font-size:30px;font-weight:700;font-style:italic;letter-spacing:.01em;color:var(--text)}
.brand:hover{text-decoration:none;color:var(--gold)}
.modes{display:flex;gap:6px;margin-left:auto}
.acct{display:flex;align-items:center;gap:12px;font-size:15px}
.modes+.acct{margin-left:8px} .brand+.acct{margin-left:auto}
.acct button{padding:5px 12px}
.steam{padding:6px 14px;border-radius:4px;background:#1b2838;border:1px solid #3b5a78;
color:#e7edf0;font-weight:600}
.steam:hover{text-decoration:none;border-color:var(--link)}
.steam.big{display:inline-block;padding:11px 20px;font-size:17px}
.modes a{padding:5px 14px;border:1px solid var(--line);border-radius:4px;color:var(--dim);
font-weight:500}
.modes a.on{border-color:var(--gold);color:var(--gold)}
.modes a:hover{text-decoration:none;color:var(--text)}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:8px}
.tile{aspect-ratio:1/1;display:flex;flex-direction:column;justify-content:flex-end;
padding:28px;border:2px solid var(--line);border-radius:8px;color:var(--text);
background:var(--panel);transition:border-color .15s,transform .15s}
.tile.turbo{background:linear-gradient(160deg,#3a2c12 0,var(--panel) 60%)}
.tile.sd{background:linear-gradient(160deg,#12343a 0,var(--panel) 60%)}
.tile:hover{text-decoration:none;transform:translateY(-3px)}
.tile.turbo:hover,.tile.turbo:focus-visible{border-color:var(--gold)}
.tile.sd:hover,.tile.sd:focus-visible{border-color:var(--link)}
.tile .name{font-family:"Barlow Condensed",sans-serif;font-size:clamp(34px,7vw,64px);
font-weight:700;font-style:italic;line-height:1}
.tile.turbo .name{color:var(--gold)}.tile.sd .name{color:var(--link)}
.tile .what{margin-top:10px;font-size:17px}
.tile .count{margin-top:6px;color:var(--dim);font-size:14px}
@media(prefers-reduced-motion:reduce){.tile{transition:none}.tile:hover{transform:none}}
h1{font-size:44px;line-height:1.05;font-weight:700;font-style:italic;margin:0 0 6px}
h2{font-size:24px;font-weight:600;margin:0 0 8px}
.sub{color:var(--dim);margin:0 0 18px}
.back{display:inline-block;margin-bottom:10px;font-size:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;
padding:18px;margin:16px 0}
.wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:0}
th{color:var(--dim);font-weight:500;font-size:13px}
td.r,th.r{text-align:right}
td.wide{white-space:normal}
.num{font-size:21px;font-weight:600;font-variant-numeric:tabular-nums}
.season{display:flex;align-items:center;gap:10px;margin:0 0 12px}
.month{font-size:26px;font-weight:600;min-width:9.5ch;text-align:center}
.step{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;
border:1px solid var(--line);border-radius:4px;font-size:22px;line-height:1;color:var(--text)}
a.step:hover{border-color:var(--gold);color:var(--gold);text-decoration:none}
.step.off{opacity:.25}.now{font-size:14px;margin-left:6px}
/* topplistan: platsen och MMR bär sidan */
.ladder td{padding:11px 8px}
.ladder .pos{font-family:"Barlow Condensed",sans-serif;font-size:30px;font-weight:700;
font-style:italic;width:54px;color:var(--dim)}
.ladder .mmr{font-family:"Barlow Condensed",sans-serif;font-size:32px;font-weight:700;
font-variant-numeric:tabular-nums}
.ladder .who a{font-size:19px;font-weight:600;color:var(--text)}
.ladder tr.p1 .pos,.ladder tr.p1 .mmr{color:var(--gold)}
.ladder tr.p1{background:linear-gradient(90deg,rgba(229,180,80,.14),rgba(229,180,80,0) 75%)}
.ladder tr.p2 .pos,.ladder tr.p3 .pos{color:var(--text)}
.ladder tr.cal .mmr,.ladder tr.cal .pos{color:var(--dim);font-weight:500}
.tag{font-size:12px;color:var(--dim);border:1px solid var(--line);border-radius:3px;
padding:1px 6px;margin-left:8px;vertical-align:middle}
.hero{width:57px;height:32px;border-radius:3px;object-fit:cover;vertical-align:middle;
background:#0b1218}
.heroes .hero{width:43px;height:24px;margin-right:3px}
.herocell{display:inline-flex;align-items:center;gap:10px}
.stats{display:flex;gap:28px;flex-wrap:wrap;margin:0 0 18px}
.stats div{display:flex;flex-direction:column}
.stats b{font-family:"Barlow Condensed",sans-serif;font-size:40px;line-height:1;font-weight:700;
font-variant-numeric:tabular-nums}
.stats .lead b,.stats .goldstat b{color:var(--gold)}
.stats span{color:var(--dim);font-size:13px;margin-top:3px}
input,button{font:inherit;padding:9px 13px;border-radius:4px;border:1px solid var(--line);
background:#0c141b;color:inherit}
input[type=text]{min-width:230px}
button{background:var(--gold);border-color:var(--gold);color:#1a1405;font-weight:600;
cursor:pointer}
button:hover{filter:brightness(1.1)}
button.quiet{background:transparent;color:var(--text);border-color:var(--line)}
input[type=checkbox]{width:17px;height:17px;padding:0;margin:0 8px 0 0;vertical-align:-3px;
accent-color:var(--gold)}
label{display:block;margin:12px 0 0}
form.row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.w{color:var(--radiant)}.l{color:var(--dire)}.dim{color:var(--dim)}.err{color:var(--dire)}
.ok{color:var(--radiant)}
.card.gold{border-color:var(--gold);background:linear-gradient(100deg,rgba(229,180,80,.16),
var(--panel) 55%)}
.card.gold h2{color:var(--gold)} .card.gold .herocell{font-size:22px;font-weight:600}
.card.gold .hero{width:86px;height:48px}
.goldtag{font-size:12px;font-weight:600;color:#1a1405;background:var(--gold);border-radius:3px;
padding:1px 6px;margin-left:10px;vertical-align:middle}
.live{margin:-8px 0 18px;padding:8px 12px;border:1px solid var(--line);border-radius:4px;
color:var(--dim);font-size:14px}
.prize{display:flex;align-items:center;gap:20px;margin:16px 0;padding:14px 18px;
border:1px solid var(--gold);border-radius:6px;
background:linear-gradient(100deg,rgba(229,180,80,.18),var(--panel) 60%)}
.prize img{width:96px;height:96px;object-fit:contain;flex:none}
.prize h2{color:var(--gold);margin:0} .prize .what{margin:2px 0 0;font-size:19px;font-weight:600}
.prize .note{margin-top:6px}
.note{color:var(--dim);font-size:14px;margin:12px 0 0;max-width:70ch}
svg.curve{width:100%;height:auto;display:block}
@media(max-width:560px){h1{font-size:34px}.ladder .mmr{font-size:26px}.hide-s{display:none}
.stats{gap:18px}.stats b{font-size:32px}.tiles{gap:12px}.tile{padding:16px}
.tile .what{font-size:14px}}
"""
FONTER = ("<link rel='preconnect' href='https://fonts.googleapis.com'>"
          "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
          "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?"
          "family=Barlow:wght@400;500;600&family=Barlow+Condensed:ital,wght@0,600;0,700;1,700"
          "&display=swap'>")


def sida(titel, body, läge=None, väg="/"):
    växel = ""
    if läge:
        växel = "<nav class='modes' aria-label='Game mode'>" + "".join(
            f"<a class='{'on' if k == läge else ''}' href='/{k}'>{v[1]}</a>"
            for k, v in LÄGEN.items()) + "</nav>"
    pid = inloggad()
    if pid:
        konto = (f"<form class='acct' method='post' action='/logout'>"
                 f"<a href='/{läge or 'turbo'}/player/{pid}'>My profile</a>"
                 f"<button class='quiet'>Sign out</button></form>")
    else:
        konto = (f"<div class='acct'><a class='steam' href='/login?next="
                 f"{quote(säker_väg(väg), safe='/')}'>Sign in through Steam</a></div>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(titel)}</title>{FONTER}<style>{CSS}</style></head><body>"
            f"<main><header class='top'><a class='brand' href='/'>{SAJTNAMN}</a>{växel}"
            f"{konto}</header>{body}</main></body></html>")


def sida_meny(c):
    """Första sidan: välj spelläge med två stora knappar."""
    månad = nu_månad()
    knappar = ""
    for k, (game_mode, namn, text) in LÄGEN.items():
        n = c.execute("SELECT COUNT(*) FROM entries WHERE month=? AND mode=? AND active=1",
                      (månad, game_mode)).fetchone()[0]
        spelare = ("Nobody registered yet" if not n
                   else f"{n} player{'s' if n != 1 else ''} registered this month")
        knappar += (f"<a class='tile {k}' href='/{k}'><span class='name'>{namn}</span>"
                    f"<span class='what'>{text}</span><span class='count'>{spelare}</span></a>")
    return sida(SAJTNAMN, f"""
<h1>Choose a ladder</h1>
<p class='sub'>Monthly Dota 2 ladders. Everyone starts at {START} MMR on the 1st of every
month, and you can join both ladders.</p>
<div class='tiles'>{knappar}</div>""")


def ruta(text, klass):
    return f"<p class='{klass}'>{html.escape(text)}</p>" if text else ""


def tal(x):
    return "?" if x is None else x


def en(x, d=1):
    """Tal med riktigt minustecken."""
    return f"{x:.{d}f}".replace("-", "−")


def mest_spelade(ms, antal=3):
    """Hjälte-id för de mest spelade hjältarna bland matcherna."""
    n = {}
    for m in ms:
        if m["hero"] is not None:
            n[m["hero"]] = n.get(m["hero"], 0) + 1
    return sorted(n, key=lambda h: -n[h])[:antal]


def sida_hem(c, månad, läge, fel=""):
    lägesnamn = LÄGEN[läge][1]
    hj = hjältar(c)
    # Bara spelare som anmält sig till den här stegen den månaden, och som spelat något.
    listade = c.execute("""SELECT p.id,p.name FROM players p JOIN entries e
        ON e.player_id=p.id WHERE e.month=? AND e.mode=? AND e.active=1""",
                        (månad, LÄGEN[läge][0])).fetchall()
    stat = [(pid, namn, spelarstat(c, pid, månad, läge)) for pid, namn in listade]
    # Alla anmälda syns. Ordning: rankade efter MMR, sedan kalibrering, sist utan matcher.
    stat.sort(key=lambda x: (x[2]["n"] >= MIN_MATCHER, x[2]["n"] > 0, x[2]["rating"]),
              reverse=True)
    fråga = "" if månad == nu_månad() else f"?month={månad}"
    rader = ""
    for i, (pid, namn, s) in enumerate(stat, 1):
        rankad = s["n"] >= MIN_MATCHER
        klass = f"p{i}" if rankad and i <= 3 else ("" if rankad else "cal")
        kal = ("" if rankad else "<span class='tag'>calibrating</span>" if s["n"]
               else "<span class='tag'>no matches yet</span>")
        ikoner = "".join(hjälte_html(hj, h, med_namn=False) for h in mest_spelade(s["matcher"]))
        rader += (f"<tr class='{klass}'><td class='pos'>{i}</td>"
                  f"<td class='who'><a href='/{läge}/player/{pid}{fråga}'>{html.escape(namn)}</a>{kal}</td>"
                  f"<td class='heroes hide-s'>{ikoner}</td>"
                  f"<td class='mmr r'>{s['rating']}</td><td class='r'>{s['n']}</td>"
                  f"<td class='r'>{s['v']}-{s['f']}</td><td class='r hide-s'>{s['wr']}%</td></tr>")
    if not rader:
        rader = (f"<tr><td colspan=7 class='dim wide'>Nobody has registered in the "
                 f"{lägesnamn} ladder for {månadsnamn(månad)} yet.</td></tr>")

    # Lokalt hjälpmedel: spelare som följs men inte valt topplistan.
    med = {pid for pid, _ in listade}
    övriga = [r for r in c.execute("SELECT id,name FROM players ORDER BY name") if r[0] not in med]
    övriga_html = ""
    if övriga and LOKALT:
        länkar = ", ".join(f"<a href='/{läge}/player/{pid}'>{html.escape(namn)}</a>"
                           for pid, namn in övriga)
        övriga_html = f"<p class='note'>Tracked but not in this ladder: {länkar}</p>"

    # Gå med: logga in med Steam och anmäl dig på din profil.
    jag = inloggad()
    if jag:
        anslut = (f"<div class='card'><h2>Join the {lägesnamn} ladder</h2><p class='note'>"
                  f"Open <a href='/{läge}/player/{jag}'>your profile</a> to join or leave "
                  f"the ladder for {månadsnamn(nu_månad())}.</p>{ruta(fel, 'err')}</div>")
    else:
        anslut = (f"<div class='card'><h2>Join the {lägesnamn} ladder</h2>"
                  f"<p><a class='steam big' href='/login?next=/{läge}'>Sign in through Steam"
                  f"</a></p>{ruta(fel, 'err')}<p class='note'>You sign in on Steam's own "
                  f"site. This site never sees your password and only learns your Steam ID. "
                  f"Turn on \"Expose Public Match Data\" in Dota first, then join the ladder "
                  f"from your profile. You can join both ladders.</p></div>")
    if LOKALT:      # bara när sajten körs på den egna datorn: lägg till vem som helst för test
        anslut += (f"<div class='card'><h2>Local testing: track a player by ID</h2>"
                   f"<form class='row' method='post' action='/{läge}/add'>"
                   f"<input type='text' name='id' placeholder='Steam32 account ID' "
                   f"inputmode='numeric' aria-label='Steam32 account ID' required>"
                   f"<button class='quiet'>Track player</button></form>"
                   f"<p class='note'>Shown only on localhost. Fetches the player's matches "
                   f"without registering them in a ladder; only the player can do that, "
                   f"signed in.</p>{övriga_html}</div>")

    ledare = (f"Right now that is <b>{html.escape(stat[0][1])}</b>."
              if stat and stat[0][2]["n"] >= MIN_MATCHER else
              f"Nobody is ranked yet: it takes {MIN_MATCHER} matches this month.")
    pris = (f"<div class='prize'><img src='{PRIS_BILD}' alt='' width='96' height='96' "
            f"onerror=\"this.style.display='none'\"><div><h2>1st prize</h2>"
            f"<p class='what'>{html.escape(PRIS_TEXT)}</p><p class='note'>"
            + (f"Whoever is first on the {lägesnamn} ladder when {månadsnamn(månad)} ends "
               f"wins it. {ledare}" if månad == nu_månad() else
               f"It went to whoever finished first on the {lägesnamn} ladder in "
               f"{månadsnamn(månad)}.") + "</p></div></div>")

    return sida(f"{lägesnamn} leaderboard", f"""
<h1>{lägesnamn} leaderboard</h1>
{månadsväljare(månad, f'/{läge}')}
{pris}
<div class='card wrap'>
<table class='ladder'><tr><th>#</th><th>Player</th><th class='hide-s'>Most played</th>
<th class='r'>MMR</th><th class='r'>Matches</th><th class='r'>W-L</th>
<th class='r hide-s'>Win rate</th></tr>{rader}</table>
<p class='note'>Everyone registered in this ladder for the month is shown. Fewer than {MIN_MATCHER} matches counts as
calibrating. Months change at midnight UTC.</p></div>
{anslut}""", läge, f"/{läge}")


def sida_spelare(c, pid, månad, läge, fel="", info=""):
    p = c.execute("SELECT name,updated FROM players WHERE id=?", (pid,)).fetchone()
    if not p:
        return None
    namn, uppdaterad = p
    lägesnamn = LÄGEN[läge][1]
    hj = hjältar(c)
    s = spelarstat(c, pid, månad, läge)
    ms = s["matcher"]
    fråga = "" if månad == nu_månad() else f"?month={månad}"

    # MMR-kurva som inbyggd SVG, från månadens start
    h = [START] + [m["e"]["efter"] for m in ms]
    kurva = "<p class='dim'>Play two matches this month to get an MMR chart.</p>"
    if len(h) > 2:
        lo, hi = min(h), max(h)
        span = (hi - lo) or 1
        pts = " ".join(f"{i * 600 / (len(h) - 1):.1f},{110 - (v - lo) * 100 / span:.1f}"
                       for i, v in enumerate(h))
        y0 = 110 - (START - lo) * 100 / span      # linjen för månadens startvärde
        kurva = (f"<svg class='curve' viewBox='0 0 600 120' role='img' "
                 f"aria-label='MMR during {månadsnamn(månad)}'>"
                 f"<line x1='0' x2='600' y1='{y0:.1f}' y2='{y0:.1f}' stroke='#243542' "
                 f"stroke-dasharray='4 4'/><polyline fill='none' stroke='#e5b450' "
                 f"stroke-width='2.5' stroke-linejoin='round' points='{pts}'/></svg>"
                 f"<p class='note'>MMR match by match. Lowest {round(lo)}, highest "
                 f"{round(hi)}. The dashed line is the starting {START}.</p>")

    rader = "".join(
        f"<tr><td>{hjälte_html(hj, m['hero'])}"
        f"{'<span class=goldtag>gold +' + en(m['e']['bonus'], 0) + '</span>' if m['e']['bonus'] else ''}</td>"
        f"<td class='{'w' if m['win'] else 'l'}'>{'Win' if m['win'] else 'Loss'}</td>"
        f"<td>{tal(m['k'])}/{tal(m['d'])}/{tal(m['a'])}</td>"
        f"<td class='r'><a class='num {'w' if m['win'] else 'l'}' "
        f"href='/{läge}/player/{pid}/match/{m['id']}' title='See how this was calculated'>"
        f"{m['e']['ändring']:+.1f}</a></td>"
        f"<td class='dim hide-s'>{time.strftime('%b %d', time.gmtime(m['tid']))}</td>"
        f"<td class='hide-s'><a href='/{läge}/player/{pid}/match/{m['id']}'>Explain</a></td></tr>"
        for m in reversed(ms[-100:]))
    if not rader:
        rader = f"<tr><td colspan=6 class='dim'>No {lägesnamn} matches this month.</td></tr>"

    # Tidigare säsonger: en rad per månad som spelaren har matcher i
    säsonger = sorted({månad_för(t) for (t,) in c.execute(
        "SELECT start_time FROM matches WHERE player_id=? AND mode=?",
        (pid, LÄGEN[läge][0]))}, reverse=True)
    säsongsrader = ""
    for m in säsonger[:24]:
        x = spelarstat(c, pid, m, läge)
        markerad = "<span class='tag'>shown</span>" if m == månad else ""
        säsongsrader += (f"<tr><td><a href='/{läge}/player/{pid}?month={m}'>{månadsnamn(m)}</a>"
                         f"{markerad}</td><td class='num r'>{x['rating']}</td><td class='r'>{x['n']}</td>"
                         f"<td class='r'>{x['v']}-{x['f']}</td><td class='r'>{x['wr']}%</td></tr>")

    tid = time.strftime("%Y-%m-%d %H:%M", time.localtime(uppdaterad)) if uppdaterad else "never"
    # Anmälan gäller alltid innevarande månad, även när en äldre månad visas.
    nu = anmälan(c, pid, nu_månad(), läge)
    denna = månadsnamn(nu_månad())
    annat = next(k for k in LÄGEN if k != läge)
    även = (f" Also in the <a href='/{annat}/player/{pid}'>{LÄGEN[annat][1]}</a> ladder."
            if anmälan(c, pid, nu_månad(), annat) else "")
    post = f"<form class='row' method='post' action='/{läge}/list/{pid}'>"
    if nu:
        lista = (f"{post}<input type='hidden' name='listed' value='0'><span class='ok'>"
                 f"Registered in the {lägesnamn} ladder for {denna}.{även}</span> "
                 f"<button class='quiet'>Leave leaderboard</button></form>")
    elif nu is False:
        lista = (f"{post}<input type='hidden' name='listed' value='1'><span class='dim'>"
                 f"You left the {lägesnamn} leaderboard for {denna}.{även}</span> "
                 f"<button>Rejoin</button></form>")
    else:
        lista = (f"{post}<input type='hidden' name='listed' value='1'><span class='dim'>"
                 f"Not registered in the {lägesnamn} ladder for {denna}.{även}</span> "
                 f"<button>Join the {lägesnamn} ladder</button></form>")
    min_sida = inloggad() == pid
    if not min_sida:        # andra ser bara läget, inga knappar
        if nu:
            lista = (f"<span class='ok'>Registered in the {lägesnamn} ladder for "
                     f"{denna}.{även}</span>")
        else:
            lista = (f"<span class='dim'>Not in the {lägesnamn} ladder for {denna}.{även} "
                     f"Is this you? "
                     f"<a href='/login?next=/{läge}/player/{pid}'>Sign in through Steam</a> "
                     f"to join.</span>")
    if min_sida:
        uppdatera = (f"<form class='row' method='post' action='/{läge}/refresh/{pid}' "
                     f"style='margin-top:14px'><button>Update matches</button>"
                     f"<span class='dim'>Last updated {tid}. New matches are also fetched "
                     f"automatically when someone opens the page.</span></form>")
    else:
        uppdatera = (f"<p class='note'>Last updated {tid}. New matches are fetched "
                     f"automatically when someone opens the page.</p>")
    # Vinster i rad: räknas bakifrån bland månadens matcher i den här stegen.
    vinstsvit = 0
    for m in reversed(ms):
        if not m["win"]:
            break
        vinstsvit += 1
    sviter = (f"<div><b>{vinstsvit}</b><span>win streak</span></div>")
    guldkort = ""
    if läge == "turbo":
        _, guldhjälte, svit, omval = spela_månad(c, pid, månad, läge)
        if guldhjälte is not None:      # guldsviten visas bara där guldhjältar finns
            sviter += (f"<div class='goldstat'><b>{svit}</b><span>gold hero streak</span>"
                       f"</div>")
        if guldhjälte is not None and månad == nu_månad():
            nästa = guldbonus(svit + 1)
            svittext = (f"Gold hero streak: {svit}. " if svit else "")
            guldkort = (f"<div class='card gold'><h2>Gold hero</h2><div class='row'>"
                        f"{hjälte_html(hj, guldhjälte)}</div><p class='note'>Win a Turbo "
                        f"match with this hero for <b>+{en(nästa)} MMR</b> on top of the "
                        f"normal change. {svittext}Each gold win in a row raises the bonus "
                        f"by {en(GULD_BONUS * GULD_STREAK, 0)}, up to +{GULD_MAX}; a loss on your gold "
                        f"hero resets it. After a win you get a new random gold hero, and after "
                        f"{GULD_HOPPA_FÖRLUSTER} losses with it, it is skipped automatically. "
                        f"Losses with this gold hero: {omval['förluster']} of "
                        f"{GULD_HOPPA_FÖRLUSTER}.</p>"
                        f"{omval_html(pid, omval, min_sida)}</div>")
    favoriter = "".join(hjälte_html(hj, h, med_namn=False) for h in mest_spelade(ms, 5))
    return sida(f"{namn} in {lägesnamn}", f"""
<a class='back' href='/{läge}{fråga}'>Back to the {lägesnamn} leaderboard</a>
<h1>{html.escape(namn)}</h1>
{månadsväljare(månad, f'/{läge}/player/{pid}')}
<div class='stats'><div class='lead'><b>{s['rating']}</b><span>MMR</span></div>
<div><b>{s['n']}</b><span>matches</span></div>
<div><b>{s['v']}-{s['f']}</b><span>wins and losses</span></div>
<div><b>{s['wr']}%</b><span>win rate</span></div>
{sviter}
<div class='heroes'><b style='font-size:0;line-height:40px'>{favoriter or "&nbsp;"}</b>
<span>most played</span></div></div>
<div class='card'>{kurva}
{uppdatera}
{ruta(fel, 'err')}{ruta(info, 'ok')}</div>
<div class='card'>{lista}</div>
{guldkort}
<div class='card wrap'><h2>{lägesnamn} matches in {månadsnamn(månad)}</h2>
<table><tr><th>Hero</th><th>Result</th><th>K/D/A</th><th class='r'>MMR</th>
<th class='hide-s'>Date</th><th class='hide-s'></th></tr>{rader}</table>
<p class='note'>Select an MMR change to see how it was calculated.</p></div>
<div class='card wrap'><h2>{lägesnamn} seasons</h2>
<table><tr><th>Month</th><th class='r'>Final MMR</th><th class='r'>Matches</th>
<th class='r'>W-L</th><th class='r'>Win rate</th></tr>
{säsongsrader or "<tr><td colspan=5 class='dim'>No seasons played yet.</td></tr>"}
</table></div>""", läge, f"/{läge}/player/{pid}")


def omval_html(pid, omval, min_sida):
    """Raden om omval av guldhjälten, med knapp för spelaren själv."""
    if omval["kvar"]:
        text = "You have a repick: swap this gold hero for the next random one."
        if min_sida:
            return (f"<form class='row' method='post' action='/turbo/repick/{pid}' "
                    f"style='margin-top:14px'><button>Repick gold hero</button>"
                    f"<span class='dim'>{text}</span></form>")
        return f"<p class='note'>{text}</p>"
    matcher = OMVAL_MATCHER - omval["spelade"] % OMVAL_MATCHER
    return (f"<p class='note'>No repick left. You get one after {matcher} more "
            f"Turbo match{'es' if matcher != 1 else ''} while this stays your gold hero. "
            f"You can hold at most {OMVAL_MAX}.</p>")


def välj_om_guld(c, pid):
    """Byter spelarens guldhjälte om ett omval finns kvar. Returnerar True om det gick."""
    _, guldhjälte, _, omval = spela_månad(c, pid, nu_månad(), "turbo")
    if guldhjälte is None or not omval["kvar"]:
        return False
    c.execute("INSERT INTO gold_repick VALUES(?,?,?)", (pid, nu_månad(), int(time.time())))
    c.commit()
    return True


def sida_match(c, pid, mid, läge):
    p = c.execute("SELECT name FROM players WHERE id=?", (pid,)).fetchone()
    rad = c.execute("SELECT start_time FROM matches WHERE player_id=? AND match_id=? "
                    "AND mode=?", (pid, mid, LÄGEN[läge][0])).fetchone()
    if not p or not rad:
        return None
    månad = månad_för(rad[0])
    hj = hjältar(c)
    ms = månadens_matcher(c, pid, månad, läge)
    nr = [m["id"] for m in ms].index(mid)
    m = ms[nr]
    e, w, k, d, a = m["e"], m["win"], m["k"], m["d"], m["a"]

    ord_ = "Win" if w else "Loss"
    chans = en(100 * e["förväntat"], 1)
    före_text = (f"First match of {månadsnamn(månad)}. Everyone starts each month at {START}."
                 if nr == 0 else
                 f"Result of the {nr} earlier {LÄGEN[läge][1]} matches in {månadsnamn(månad)}. "
                 f"Everyone starts each month at {START}.")
    if e["motståndare"] is None:
        saknas = ("you have no visible Dota rank" if e["egen"] is None
                  else "OpenDota has no rank data for this match")
        mot_steg = ("Opponent level", str(START),
                    f"Counted as an average lobby, because {saknas}.")
    else:
        skillnad = e["motståndare"] - START
        jämfört = ("the same level as" if skillnad == 0 else
                   f"about {abs(skillnad)} MMR {'above' if skillnad > 0 else 'below'}")
        mot_steg = ("Opponent level", str(e["motståndare"]),
                    f"The lobby averaged {rank_namn(e['lobby'])}, {jämfört} your own rank "
                    f"({rank_namn(e['egen'])}). {START} is a lobby at your level.")
    steg = [("MMR before the match", en(e["före"]), före_text),
            mot_steg,
            ("Expected win chance", f"{chans}%",
             f"Your MMR against the opponent level gives {en(100 * e['rå'], 0)}%. Only "
             f"{en(100 * FÖRVÄNTAN_VIKT, 0)}% of its distance from 50% is counted, so it "
             f"nudges the result without deciding it."),
            (f"Base change for a {ord_.lower()}", en(e["grund"]),
             (f"{K} × (100% − {chans}%). An unexpected win gives more."
              if w else f"−{K} × {chans}%. An expected loss costs less."))]
    if e["kda"] is None:
        steg.append(("K/D/A", "missing", "OpenDota has no numbers for this match, "
                     "so it is counted without adjustment."))
    else:
        pct = 100 * e["faktor"]
        if abs(pct) < 0.5:
            dom = "An average match, no real adjustment."
        elif (pct > 0) == bool(w):
            dom = ("A strong K/D/A boosts the win." if w
                   else "A weak K/D/A makes the loss cost more.")
        else:
            dom = ("Many deaths reduce the win." if w
                   else "A strong K/D/A softens the loss.")
        steg.append(("K/D/A score", en(e["kda"], 2),
                     f"({k} kills + {a} assists ÷ 3) ÷ {max(d, 1)} deaths. "
                     f"An average match is {en(KDA_NORMAL, 0)}."))
        # vid förlust vänds tecknet: bra K/D/A ska minska avdraget
        verkan = pct if w else -pct
        steg.append(("K/D/A adjustment",
                     f"{'+' if verkan >= 0 else ''}{en(verkan, 0)}%",
                     f"{dom} At most ±{en(100 * KDA_MAX, 0)}%, reached at double or "
                     f"half the score of an average match."))
    if e["bonus"]:
        hur = ("First gold win in a row." if e["streak"] == 1 else
               f"Gold win number {e['streak']} in a row, capped at the maximum +{GULD_MAX}."
               if e["bonus"] >= GULD_MAX else
               f"Gold win number {e['streak']} in a row: {GULD_BONUS} × "
               f"{en(1 + GULD_STREAK * (e['streak'] - 1))}.")
        steg.append(("Gold hero bonus", f"+{en(e['bonus'])}",
                     f"Won with the gold hero. {hur} A new gold hero was drawn."))
    elif e["guld"] is not None:
        hoppad = (f" This was loss number {GULD_HOPPA_FÖRLUSTER} with it, so it was skipped "
                  f"and the next hero became the gold hero." if e["hoppad"] else "")
        steg.append(("Gold hero bonus", "0", "Played the gold hero but lost. No bonus, "
                     f"and the gold win streak was reset.{hoppad}"))
    tecken = "+" if e["ändring"] >= 0 else ""
    steg.append(("MMR change", f"<b class='{'w' if w else 'l'}'>{tecken}{en(e['ändring'])}</b>",
                 "The base change after the K/D/A adjustment"
                 + (", plus the gold hero bonus." if e["bonus"] else ".")))
    steg.append(("MMR after the match", f"<b>{en(e['efter'])}</b>", ""))
    rader = "".join(f"<tr><td>{namn}</td><td class='num r'>{värde}</td>"
                    f"<td class='dim wide'>{text}</td></tr>"
                    for namn, värde, text in steg)
    hjältenamn = html.escape(hj.get(m["hero"], ("an unknown hero",))[0])
    return sida(f"Match {mid}", f"""
<a class='back' href='/{läge}/player/{pid}?month={månad}'>Back to {html.escape(p[0])}</a>
<h1><span class='{'w' if w else 'l'}'>{ord_}</span> as {hjältenamn}</h1>
<p class='sub'>{hjälte_html(hj, m['hero'], med_namn=False)}
&nbsp; K/D/A {tal(k)}/{tal(d)}/{tal(a)}, played
{time.strftime('%B %d, %Y at %H:%M', time.gmtime(m['tid']))} UTC.
<a href='https://www.opendota.com/matches/{mid}'>View match {mid} on OpenDota</a></p>
<div class='card wrap'><h2>How the MMR change was calculated</h2>
<table><tr><th>Step</th><th class='r'>Value</th><th>Explanation</th></tr>
{rader}</table></div>""", läge, f"/{läge}/player/{pid}/match/{mid}")


# ---------- server ----------
class Handler(BaseHTTPRequestHandler):
    def _skicka(self, kod, body, typ="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8")
        self.send_response(kod)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Frame-Options", "DENY")          # sidan får inte bäddas in
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, plats, extra=None):
        self._skicka(303, "", extra={"Location": plats, **(extra or {})})

    def _404(self):
        self._skicka(404, sida("Not found", "<h1>Page not found</h1><p class='sub'>"
                                            "<a href='/'>Choose a ladder</a></p>"))

    def _nekad(self):
        self._skicka(403, sida("Not allowed", "<h1>Not allowed</h1><p class='sub'>Sign in "
                               "through Steam as this player to do that.</p>"))

    def do_GET(self):
        väg, _, fråga = self.path.partition("?")
        q = parse_qs(fråga)
        månad = tolka_månad((q.get("month") or [""])[0])
        delar = väg.rstrip("/").split("/")[1:]       # "/turbo/player/5" -> [turbo, player, 5]
        with closing(db()) as c:
            _begäran.pid = läs_session(c, self.headers.get("Cookie"))
            if delar == ["login"]:
                return self._redirect(steam_adress((q.get("next") or ["/"])[0]))
            if delar == ["login", "return"]:
                return self._inloggning_klar(c, q)
            if not delar:
                köa_uppdatering(c, anmälda(c), FÄRSK_LISTA)
                return self._skicka(200, med_status(sida_meny(c)))
            läge, rest = delar[0], delar[1:]
            if läge in LÄGEN:
                if not rest:
                    köa_uppdatering(c, anmälda(c, läge), FÄRSK_LISTA)
                    return self._skicka(200, med_status(sida_hem(c, månad, läge)))
                if (len(rest) == 4 and rest[0] == "player" and rest[2] == "match"
                        and rest[1].isdigit() and rest[3].isdigit()):
                    s = sida_match(c, int(rest[1]), int(rest[3]), läge)
                    if s:
                        return self._skicka(200, s)
                if len(rest) == 2 and rest[0] == "player" and rest[1].isdigit():
                    nya = (q.get("new") or [""])[0]
                    info = ""
                    if nya.isdigit():
                        info = (f"{nya} new matches fetched." if int(nya)
                                else "No new matches since last time.")
                    s = sida_spelare(c, int(rest[1]), månad, läge, info=info)
                    if s:
                        köa_uppdatering(c, [int(rest[1])], FÄRSK_PROFIL)
                        # hämta om sidan utan ?new=..., annars står meddelandet kvar
                        return self._skicka(200, med_status(s) if not nya else s)
        self._404()

    def _inloggning_klar(self, c, q):
        """Steam har skickat tillbaka spelaren: kontrollera svaret och logga in."""
        nästa = säker_väg((q.get("next") or ["/"])[0])
        läge = nästa.split("/")[1] if nästa.split("/")[1] in LÄGEN else "turbo"
        try:
            pid = kontrollera_steam(q)
        except ApiFel as e:
            return self._skicka(200, sida_hem(c, nu_månad(), läge, str(e)))
        _begäran.pid = pid
        kakor = {"Set-Cookie": kaka(skapa_session(c, pid), SESSION_DAGAR * 86400)}
        try:                # hämta spelarens matcher direkt, så att profilen finns
            uppdatera_spelare(c, pid)
        except ApiFel as e:
            if not c.execute("SELECT 1 FROM players WHERE id=?", (pid,)).fetchone():
                # inloggad, men inga matcher gick att hämta (t.ex. privat profil)
                return self._skicka(200, sida_hem(c, nu_månad(), läge, str(e)), extra=kakor)
        if nästa in ("/", "/turbo", "/sd"):     # kom från en topplista: visa egna profilen
            nästa = f"/{läge}/player/{pid}"
        self._redirect(nästa, kakor)

    def do_POST(self):
        try:
            längd = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            längd = 0
        form = parse_qs(self.rfile.read(min(längd, 10000)).decode("utf-8", "replace"))
        vill_listas = (form.get("listed") or [""])[0] == "1"
        delar = self.path.partition("?")[0].rstrip("/").split("/")[1:]
        with closing(db()) as c:
            _begäran.pid = läs_session(c, self.headers.get("Cookie"))
            # Formulär får bara skickas från sajten själv, inte från andra webbplatser.
            ursprung = self.headers.get("Origin")
            if ursprung and ursprung.rstrip("/") != BAS:
                return self._nekad()
            if delar == ["logout"]:
                try:
                    token = SimpleCookie(self.headers.get("Cookie") or "")["session"].value
                    c.execute("DELETE FROM sessions WHERE token_hash=?",
                              (hashlib.sha256(token.encode()).hexdigest(),))
                    c.commit()
                except Exception:      # ingen eller trasig kaka
                    pass
                _begäran.pid = None
                return self._redirect("/", {"Set-Cookie": kaka("", 0)})
            if not delar or delar[0] not in LÄGEN:
                return self._404()
            läge, rest = delar[0], delar[1:]
            if rest == ["add"] and LOKALT:      # testhjälp, finns bara på den egna datorn
                try:
                    pid = tolka_id((form.get("id") or [""])[0])
                    nya = uppdatera_spelare(c, pid)
                except ApiFel as e:
                    return self._skicka(200, sida_hem(c, nu_månad(), läge, str(e)))
                return self._redirect(f"/{läge}/player/{pid}?new={nya}")
            if len(rest) == 2 and rest[1].isdigit() and rest[0] in ("refresh", "list", "repick"):
                pid = int(rest[1])
                if inloggad() != pid:           # bara spelaren själv
                    return self._nekad()
                if rest[0] == "refresh":
                    try:
                        nya = uppdatera_spelare(c, pid)
                    except ApiFel as e:
                        # visa felet på spelarens egen sida om den finns
                        s = (sida_spelare(c, pid, nu_månad(), läge, fel=str(e))
                             or sida_hem(c, nu_månad(), läge, str(e)))
                        return self._skicka(200, s)
                    return self._redirect(f"/{läge}/player/{pid}?new={nya}")
                if rest[0] == "repick":
                    if läge != "turbo" or not välj_om_guld(c, pid):
                        s = sida_spelare(c, pid, nu_månad(), "turbo",
                                         fel="No repick available right now.")
                        return self._skicka(200, s) if s else self._404()
                    return self._redirect(f"/{läge}/player/{pid}")
                if c.execute("SELECT 1 FROM players WHERE id=?", (pid,)).fetchone():
                    if vill_listas:
                        anmäl(c, pid, läge)
                    else:
                        lämna(c, pid, läge)
                    return self._redirect(f"/{läge}/player/{pid}")
        self._404()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    db().close()                    # skapar databasen om den saknas

    def _hjältar_i_bakgrunden():    # hjältenamn och bilder, hämtas en gång i veckan
        with closing(db()) as c:
            uppdatera_hjältar(c)
    # I bakgrunden, så att sajten startar direkt även om OpenDota svarar långsamt.
    threading.Thread(target=_hjältar_i_bakgrunden, daemon=True).start()
    try:
        server = ThreadingHTTPServer((VÄRD, PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} är upptagen. Körs sajten redan i ett annat fönster? "
                 "Stäng det och försök igen.")
    print(f"Kör på {BAS}  (Ctrl+C för att stoppa)")
    print(f"Databas: {DB}")
    if os.environ.get("RAILWAY_ENVIRONMENT") and not _volym and not os.environ.get("TURBO_DB"):
        print("VARNING: ingen volym är ansluten. Spelare, anmälningar och inloggningar "
              "försvinner vid nästa omstart. Lägg till en volym med mount path /data.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStoppad.")
