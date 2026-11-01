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

from turbo_rank import (ASSIST_VIKT, FÖRVÄNTAN_VIKT, GULD_BONUS, GULD_MAX, GULD_STREAK, K, KDA_MAX, KDA_NORMAL, SINGLE_DRAFT,
                        START, TURBO, guldbonus, hämta_profil, motståndarnivå, rank_namn, ApiFel, förklara, giltig,
                        hämta_json, hämta_alla_matcher, anrop_idag, inga_matcher_fel, månad_för,
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
FÄRSK_LISTA = 30 * 60       # anmälda spelare när menyn eller en topplista öppnas (minst)
MELLANRUM = 3               # sekunders paus mellan två spelare (OpenDota: ca 60 anrop/minut)
DYGNSBUDGET = 1500          # högst så många anrop per dygn i bakgrunden (gratisgräns 2000)
ANROP_PER_SPELARE = 2       # högst: matcherna (ett anrop för båda lägena) + ibland profilen
PROFIL_ÅLDER = 24 * 3600    # namn och rank hämtas om högst en gång per dygn
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
# Stängd beta: högst så många spelare kan logga in. Spelare som redan har en plats kommer
# alltid in. Den som loggar in när det är fullt hamnar på väntelistan. Ändras med
# miljövariabeln TURBO_MAX_PLAYERS (0 = inget tak).
MAX_SPELARE = int(os.environ.get("TURBO_MAX_PLAYERS") or "80")
# Spellägen: adressdel -> (OpenDotas game_mode, namn, kort beskrivning på menyn)
LÄGEN = {"turbo": (TURBO, "Turbo", "Fast games, quick gold and experience."),
         "sd": (SINGLE_DRAFT, "Single Draft", "Pick from four random heroes.")}
BILDER = "https://cdn.cloudflare.steamstatic.com"   # här ligger Dotas hjältebilder
HJÄLTE_ÅLDER = 7 * 24 * 3600   # hämta om hjältelistan en gång i veckan (nya hjältar)
# ---------- Ladder Pass (ett gratis månadspass, likt Dotas gamla Battle Pass) ----------
# XP kommer från alla matcher i båda stegarna och från månadens uppdrag. Allt räknas om
# från matcherna i databasen, precis som MMR, så inget behöver sparas utom anmälan.
PASS = -1                   # entries.mode för passet (inget riktigt spelläge)
PASS_NAMN = "Ladder Pass"
XP_VINST = 100              # XP per vunnen match (Turbo eller Single Draft)
XP_FÖRLUST = 40             # XP per förlorad match
NIVÅ_XP = 500               # XP per nivå
PASS_UPPDRAG = 8            # antal uppdrag per månad (det utvalda + sju slumpade)
PASS_UTVALDA = 3            # antal utvalda hjältar per månad
# Veckouppdrag: VECKO_ANTAL nya uppdrag varje måndag 00:00 UTC. Texten lånas från UPPDRAG.
# Bara matcher under veckan (och inom månaden) räknas. Veckor som delas mellan två månader
# räknas för sig i varje månad, eftersom passet nollställs den 1:a.
# Veckouppdragen räknas inte in i "alla uppdrag" för Champion track.
VECKO_ANTAL = 4
VECKO_UPPDRAG = {           # nyckel -> (XP, möjliga mål)
    "utvald": (300, (1, 1, 2)), "spela": (200, (6, 8, 10)), "turbo": (200, (3, 4, 5)),
    "sd": (250, (1, 2, 2)), "kills": (200, (12, 14, 16)), "assists": (200, (15, 18, 20)),
    "rena": (250, (1, 2, 2)), "hjältar": (250, (3, 4, 5)), "svit": (250, (2, 3, 3)),
    "guld": (250, (1, 1, 2)), "kda": (200, (1, 2, 3)),
}
# Uppdrag: nyckel -> (XP, möjliga mål, text). Vilka uppdrag och mål en månad får slumpas
# med månaden som frö, så alla får samma uppdrag och de ändras inte under månaden.
# Ändras listan här ändras också innevarande månads uppdrag.
UPPDRAG = {
    "utvald": (500, (2, 3, 4), "Win {n} matches with a featured hero"),
    "spela": (300, (20, 25, 30), "Play {n} matches"),
    "turbo": (300, (8, 10, 12), "Win {n} Turbo matches"),
    "sd": (350, (3, 4, 5), "Win {n} Single Draft matches"),
    "kills": (300, (15, 18, 20), "Get {n} kills in one match"),
    "assists": (300, (20, 25, 30), "Get {n} assists in one match"),
    "rena": (350, (3, 4, 5), "Win {n} matches with 2 deaths or fewer"),
    "hjältar": (350, (6, 8, 10), "Win with {n} different heroes"),
    "svit": (400, (3, 4, 5), "Win {n} matches in a row"),
    "guld": (400, (2, 3, 4), "Win {n} Turbo matches with your gold hero"),
    "kda": (300, (3, 4, 5), "Finish {n} matches with a K/D/A score of 4 or more"),
}
# Belöningar på sajten: (nivå, sort, namn, beskrivning). Sorten styr bilden på
# belöningsbanan. Märket syns bredvid namnet på stegarna.
BELÖNINGAR = [(1, "bronze", "Bronze badge", "Next to your name on the ladders"),
              (5, "silver", "Silver badge", "Upgrades your ladder badge"),
              (10, "gold", "Gold badge", "Upgrades your ladder badge"),
              (15, "title", "Seasoned", "Title on your pass profile"),
              (20, "mythic", "Mythic badge", "The top ladder badge"),
              (25, "glow", "Glowing name", "Your name glows on the ladders"),
              (30, "title", "Relentless", "Title on your pass profile")]
PASS_MAXNIVÅ = 30           # så många nivåer visas på belöningsbanan
GLÖD_NIVÅ = 25              # från den här nivån lyser namnet på stegarna
# Champion track: en andra rad på belöningsbanan som låses upp när man har klarat
# alla månadens uppdrag. Belöningarna gäller sedan från respektive nivå.
MÄSTAR_BELÖNINGAR = [(3, "crown", "Champion crown", "A crown next to your name on the ladders"),
                     (8, "frame", "Champion frame", "A golden ring around your level emblem"),
                     (14, "title", "Questmaster", "Champion title on your pass profile"),
                     (20, "shine", "Shining badge", "Your ladder badge shimmers"),
                     (30, "title", "Pass Legend", "The rarest title on the site")]
KRONA_NIVÅ, RAM_NIVÅ, GLANS_NIVÅ = 3, 8, 20     # när champion-effekterna slår till
MÄRKEN = [(20, "mythic"), (10, "gold"), (5, "silver"), (1, "bronze")]   # (från nivå, klass)
# Titlar, den första som spelaren uppfyller visas: (nivå, kräver champion, titel).
TITLAR = [(30, True, "Pass Legend"), (30, False, "Relentless"), (14, True, "Questmaster"),
          (15, False, "Seasoned")]
# Pris till den som har mest XP när månaden slutar. Byt text eller bild här.
PASS_PRIS_TEXT = PRIS_TEXT
PASS_PRIS_BILD = PRIS_BILD
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
    # pass_heroes = passets utvalda hjältar för månaden, som JSON-lista.
    c.execute("CREATE TABLE IF NOT EXISTS pass_heroes(month TEXT PRIMARY KEY, heroes TEXT)")
    # pass_seen = vilken nivå spelaren senast såg på sin passida (för nivå-upp-animationen).
    c.execute("CREATE TABLE IF NOT EXISTS pass_seen(player_id INTEGER, month TEXT, "
              "level INTEGER, champion INTEGER, PRIMARY KEY(player_id, month))")
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
    # profile_fetched = när namn och rank senast hämtades (de hämtas om en gång per dygn).
    if "profile_fetched" not in [r[1] for r in c.execute("PRAGMA table_info(players)")]:
        c.execute("ALTER TABLE players ADD COLUMN profile_fetched INTEGER")
        c.commit()
    if "listed" not in [r[1] for r in c.execute("PRAGMA table_info(players)")]:
        c.execute("ALTER TABLE players ADD COLUMN listed INTEGER NOT NULL DEFAULT 0")
        c.commit()
    # members = spelare med en plats i betan, waitlist = de som försökte när det var fullt.
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='members'").fetchone():
        c.execute("CREATE TABLE members(player_id INTEGER PRIMARY KEY, joined INTEGER)")
        # Första gången: alla som redan har loggat in (anmält sig eller har en inloggning)
        # får behålla sin plats.
        c.execute("INSERT OR IGNORE INTO members SELECT player_id, strftime('%s','now') FROM "
                  "(SELECT player_id FROM entries UNION SELECT player_id FROM sessions)")
        c.commit()
    c.execute("CREATE TABLE IF NOT EXISTS waitlist(player_id INTEGER PRIMARY KEY, "
              "joined INTEGER)")
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

    # Har spelaren redan matcher räcker det att hämta dagarna sedan förra uppdateringen
    # (plus marginal). Första gången hämtas hela historiken. Ett anrop för båda lägena.
    har_matcher = c.execute("SELECT 1 FROM matches WHERE player_id=? LIMIT 1",
                            (pid,)).fetchone()
    dagar = (int((time.time() - gammal[1]) // 86400) + 3
             if har_matcher and gammal and gammal[1] else None)
    rader = [(pid, m["match_id"], m["start_time"], int(vann(m)), m.get("hero_id"),
              m.get("kills"), m.get("deaths"), m.get("assists"), m["game_mode"],
              m.get("average_rank"))
             for m in hämta_alla_matcher(pid, [v[0] for v in LÄGEN.values()], dagar)
             if giltig(m)]
    if not rader and not har_matcher:
        raise inga_matcher_fel(pid, " or ".join(n for _, n, _ in LÄGEN.values()))
    uppdatera_hjältar(c)

    # Namn och rank hämtas högst en gång per dygn. Namnet är bara kosmetika: går det inte
    # att hämta behåller vi det gamla.
    namn, rank = None, None
    senast = c.execute("SELECT profile_fetched FROM players WHERE id=?", (pid,)).fetchone()
    if not senast or not senast[0] or time.time() - senast[0] > PROFIL_ÅLDER:
        try:
            namn, rank = hämta_profil(pid)
            profil_hämtad = True
        except ApiFel:
            profil_hämtad = False
    else:
        profil_hämtad = False
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
    if profil_hämtad:
        c.execute("UPDATE players SET profile_fetched=? WHERE id=?", (int(time.time()), pid))
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
_tillstånd = {"arbetar": False, "vila_till": 0}


def färsk_lista(antal):
    """Hur gammal en anmäld spelares data får vara innan den hämtas om, när en topplista
    öppnas. Med många spelare sprids dygnets anrop jämnt så att budgeten räcker hela
    dygnet i stället för att ta slut på förmiddagen. 80 spelare ger ungefär 80 minuter."""
    return max(FÄRSK_LISTA, int(antal * 1.1 * 86400 / DYGNSBUDGET))


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
                    # Alla anrop i dag räknas, även knapptryck och inloggningar.
                    if not _kö or anrop_idag() + ANROP_PER_SPELARE > DYGNSBUDGET:
                        _kö.clear()
                        return
                    pid = _kö[0]
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


# ---------- Ladder Pass ----------
def pass_anmälan(c, pid, månad):
    """True om spelaren är med i passet den månaden, False om hen har lämnat, None om aldrig."""
    rad = c.execute("SELECT active FROM entries WHERE player_id=? AND month=? AND mode=?",
                    (pid, månad, PASS)).fetchone()
    return bool(rad[0]) if rad else None


def pass_anmäl(c, pid, med):
    """Går med i (med=True) eller lämnar passet för innevarande månad."""
    c.execute("INSERT INTO entries(player_id,month,mode,active) VALUES(?,?,?,?) "
              "ON CONFLICT(player_id,month,mode) DO UPDATE SET active=excluded.active",
              (pid, nu_månad(), PASS, int(med)))
    c.commit()


def pass_anmälda(c, månad=None):
    return [r[0] for r in c.execute("SELECT player_id FROM entries WHERE month=? AND mode=? "
                                    "AND active=1", (månad or nu_månad(), PASS))]


def pass_hjältar(c, månad):
    """Månadens utvalda hjältar. Sparas första gången, så att de inte byts om Dota får
    en ny hjälte mitt i månaden. [] om hjältelistan inte har hämtats än."""
    rad = c.execute("SELECT heroes FROM pass_heroes WHERE month=?", (månad,)).fetchone()
    if rad:
        return json.loads(rad[0])
    alla = [r[0] for r in c.execute("SELECT id FROM heroes ORDER BY id")]
    if not alla:
        return []
    valda = random.Random(f"utvalda-{månad}").sample(alla, min(PASS_UTVALDA, len(alla)))
    c.execute("INSERT OR IGNORE INTO pass_heroes VALUES(?,?)", (månad, json.dumps(valda)))
    c.commit()
    return valda


def pass_veckor(månad):
    """Veckorna som överlappar månaden: [(veckans måndag, start, slut)], där start och
    slut är beskurna till månaden. Måndagen används som frö för veckans uppdrag."""
    start, slut = månadsgränser(månad)
    måndag = start - time.gmtime(start).tm_wday * 86400
    ut = []
    while måndag < slut:
        ut.append((måndag, max(måndag, start), min(måndag + 7 * 86400, slut)))
        måndag += 7 * 86400
    # En stump på under tre dagar i början eller slutet av månaden slås ihop med
    # grannveckan, så att ingen vecka blir för kort för sina uppdrag.
    if len(ut) > 1 and ut[0][2] - ut[0][1] < 3 * 86400:
        ut[:2] = [(ut[1][0], ut[0][1], ut[1][2])]
    if len(ut) > 1 and ut[-1][2] - ut[-1][1] < 3 * 86400:
        ut[-2:] = [(ut[-2][0], ut[-2][1], ut[-1][2])]
    return ut


def vecko_uppdrag(måndag):
    """Veckans uppdrag, samma för alla. Fröet är veckans måndag (UTC)."""
    rng = random.Random("vecka-" + time.strftime("%Y-%m-%d", time.gmtime(måndag)))
    return [{"typ": k, "n": rng.choice(VECKO_UPPDRAG[k][1]), "xp": VECKO_UPPDRAG[k][0]}
            for k in rng.sample(list(VECKO_UPPDRAG), VECKO_ANTAL)]


def pass_uppdrag(månad):
    """Månadens uppdrag: det utvalda först, sedan sju slumpade. Samma för alla."""
    rng = random.Random(f"uppdrag-{månad}")
    övriga = rng.sample([k for k in UPPDRAG if k != "utvald"], PASS_UPPDRAG - 1)
    return [{"typ": k, "n": rng.choice(UPPDRAG[k][1]), "xp": UPPDRAG[k][0]}
            for k in ["utvald"] + övriga]


def pass_matcher(c, pid, månad):
    """Spelarens matcher i alla stegar under månaden, äldsta först."""
    ut = []
    for läge in LÄGEN:
        for m in månadens_matcher(c, pid, månad, läge):
            ut.append({**m, "läge": läge, "guldvinst": bool(m["e"]["bonus"])})
    ut.sort(key=lambda m: (m["tid"], m["id"]))
    return ut


def framsteg(typ, ms, utvalda):
    """Hur långt spelaren har kommit på ett uppdrag (antal, eller bästa värde)."""
    vinster = [m for m in ms if m["win"]]
    if typ == "utvald":
        return sum(1 for m in vinster if m["hero"] in utvalda)
    if typ == "spela":
        return len(ms)
    if typ in ("turbo", "sd"):
        return sum(1 for m in vinster if m["läge"] == typ)
    if typ == "kills":
        return max((m["k"] or 0 for m in ms), default=0)
    if typ == "assists":
        return max((m["a"] or 0 for m in ms), default=0)
    if typ == "rena":
        return sum(1 for m in vinster if m["d"] is not None and m["d"] <= 2)
    if typ == "hjältar":
        return len({m["hero"] for m in vinster if m["hero"] is not None})
    if typ == "svit":
        bäst = nu = 0
        for m in ms:
            nu = nu + 1 if m["win"] else 0
            bäst = max(bäst, nu)
        return bäst
    if typ == "guld":
        return sum(1 for m in ms if m["guldvinst"])
    if typ == "kda":
        return sum(1 for m in ms if None not in (m["k"], m["d"], m["a"])
                   and (m["k"] + m["a"] * ASSIST_VIKT) / max(m["d"], 1) >= 4)
    return 0


def pass_status(c, pid, månad):
    """XP, nivå och uppdragens framsteg för en spelare en månad."""
    ms = pass_matcher(c, pid, månad)
    utvalda = pass_hjältar(c, månad)
    uppdrag = []
    for u in pass_uppdrag(månad):
        f = framsteg(u["typ"], ms, utvalda)
        uppdrag.append({**u, "framsteg": min(f, u["n"]), "klar": f >= u["n"]})
    veckor = []
    for måndag, a, b in pass_veckor(månad):
        veckans = [m for m in ms if a <= m["tid"] < b]
        lista = []
        for u in vecko_uppdrag(måndag):
            f = framsteg(u["typ"], veckans, utvalda)
            lista.append({**u, "framsteg": min(f, u["n"]), "klar": f >= u["n"]})
        veckor.append({"måndag": måndag, "start": a, "slut": b, "uppdrag": lista,
                       "nu": a <= time.time() < b})
    match_xp = sum(XP_VINST if m["win"] else XP_FÖRLUST for m in ms)
    uppdrag_xp = sum(u["xp"] for u in uppdrag if u["klar"])
    vecko_xp = sum(u["xp"] for v in veckor for u in v["uppdrag"] if u["klar"])
    xp = match_xp + uppdrag_xp + vecko_xp
    klara = sum(u["klar"] for u in uppdrag)
    return {"xp": xp, "nivå": xp // NIVÅ_XP, "match_xp": match_xp, "uppdrag_xp": uppdrag_xp,
            "vecko_xp": vecko_xp, "veckor": veckor,
            "uppdrag": uppdrag, "klara": klara, "n": len(ms),
            "v": sum(m["win"] for m in ms), "mästare": klara == len(uppdrag)}


def märke_html(nivå, glans=False):
    """Passmärket för en nivå, eller '' under nivå 1. glans = champion-märket som skimrar."""
    klass = next((k for från, k in MÄRKEN if nivå >= från), None)
    if not klass:
        return ""
    return (f"<span class='badge {klass}{' shine' if glans else ''}' "
            f"title='{PASS_NAMN} level {nivå}'>Lv {nivå}</span>")


def titel(nivå, mästare=False):
    return next((t for från, krav, t in TITLAR if nivå >= från and (mästare or not krav)), "")


def nivåklass(nivå):
    """'bronze', 'silver' ... för nivån, eller 'none' under nivå 1."""
    return next((k for från, k in MÄRKEN if nivå >= från), "none")


def har(s, nivå):
    """Har spelaren låst upp champion-belöningen på den nivån?"""
    return bool(s and s["mästare"] and s["nivå"] >= nivå)


def krona_html():
    return ("<svg class='crown' viewBox='0 0 24 24' fill='currentColor' role='img' "
            "aria-label='Champion'><title>Champion: completed every quest</title>"
            "<path d='M3 8l4.5 4L12 5l4.5 7L21 8l-2 11H5z'/></svg>")


def namn_html(s, namn, href):
    """Spelarens namn med passets utsmyckning: lysande namn, märke och krona.
    s = pass_status, eller None om spelaren inte är med i passet."""
    if s is None:
        return f"<a href='{href}'>{html.escape(namn)}</a>"
    glöd = " class='glow'" if s["nivå"] >= GLÖD_NIVÅ else ""
    return (f"<a{glöd} href='{href}'>{html.escape(namn)}</a>"
            f"{krona_html() if har(s, KRONA_NIVÅ) else ''}"
            f"{märke_html(s['nivå'], har(s, GLANS_NIVÅ))}")


def pass_info(c, pid, månad):
    """pass_status för spelaren, eller None om hen inte är med i passet den månaden."""
    if not pass_anmälan(c, pid, månad):
        return None
    return pass_status(c, pid, månad)


def pass_märke(c, pid, månad):
    """Märket (och kronan) som visas bredvid namnet, bara för spelare med i passet."""
    s = pass_info(c, pid, månad)
    if s is None:
        return ""
    return (f"{krona_html() if har(s, KRONA_NIVÅ) else ''}"
            f"{märke_html(s['nivå'], har(s, GLANS_NIVÅ))}")


# ---------- sidor ----------
CSS = """
:root{--bg:#0f1820;--panel:#16232d;--line:#243542;--text:#e7edf0;--dim:#8ea2ae;
--gold:#e5b450;--radiant:#74d195;--dire:#e96a55;--link:#9fd4e8;--pass:#b9a2f5}
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
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;margin-top:8px}
.tile{aspect-ratio:1/1;display:flex;flex-direction:column;justify-content:flex-end;
padding:28px;border:2px solid var(--line);border-radius:8px;color:var(--text);
background:var(--panel);transition:border-color .15s,transform .15s}
.tile.turbo{background:linear-gradient(160deg,#3a2c12 0,var(--panel) 60%)}
.tile.sd{background:linear-gradient(160deg,#12343a 0,var(--panel) 60%)}
.tile:hover{text-decoration:none;transform:translateY(-3px)}
.tile.turbo:hover,.tile.turbo:focus-visible{border-color:var(--gold)}
.tile.sd:hover,.tile.sd:focus-visible{border-color:var(--link)}
.tile.pass{background:radial-gradient(90% 90% at 100% 0,rgba(255,210,120,.22),transparent 60%),repeating-linear-gradient(115deg,rgba(255,255,255,.03) 0 2px,transparent 2px 16px),linear-gradient(160deg,#3a2470 0,#1c1533 45%,var(--panel) 80%);border-color:rgba(185,162,245,.45);box-shadow:0 18px 50px -24px rgba(130,80,240,.9)}
.tile.pass:hover,.tile.pass:focus-visible{border-color:var(--pass)}
.tile .name{font-family:"Barlow Condensed",sans-serif;font-size:clamp(30px,5vw,48px);
font-weight:700;font-style:italic;line-height:1}
.tile.turbo .name{color:var(--gold)}.tile.sd .name{color:var(--link)}
.tile.pass .name{color:var(--pass)}
.modes a.pass.on{border-color:var(--pass);color:var(--pass)}
/* Ladder Pass */
.badge{font-size:12px;font-weight:700;border-radius:3px;padding:1px 6px;margin-left:8px;
vertical-align:middle;white-space:nowrap}
.badge.bronze{background:#c98a52;color:#1a1005}.badge.silver{background:#c9d3da;color:#11181d}
.badge.gold{background:var(--gold);color:#1a1405}
.badge.mythic{background:linear-gradient(90deg,#8d6cf0,#e08cf0);color:#160d26}
h1 .badge{font-size:16px;padding:3px 9px;margin-left:12px}
a.glow{color:#fff;text-shadow:0 0 10px rgba(215,160,255,.9),0 0 22px rgba(160,110,255,.6)}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;
margin-bottom:12px}.head h2{margin:0}.head .dim,.head .ok{font-size:14px}
/* emblem: sexkant i nivåns färg */
.emblem{--e1:#5b6b77;--e2:#2a3742;--ec:#9fb0bb;--eg:rgba(0,0,0,0);width:120px;aspect-ratio:1;
flex:none;clip-path:polygon(50% 0,94% 25%,94% 75%,50% 100%,6% 75%,6% 25%);
background:linear-gradient(160deg,var(--e1),var(--e2));display:grid;place-items:center}
.emblem .in{width:84%;height:84%;clip-path:inherit;display:flex;flex-direction:column;
align-items:center;justify-content:center;
background:radial-gradient(circle at 50% 30%,#2a2142,#0d0b18 75%)}
.emblem b{font-family:"Barlow Condensed",sans-serif;font-size:52px;line-height:.9;
font-weight:700;color:var(--ec)}
.emblem span{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim)}
.emblem.t-bronze{--e1:#f0b47a;--e2:#7a4520;--ec:#f2b27a;--eg:rgba(240,160,90,.45)}
.emblem.t-silver{--e1:#f2f6f8;--e2:#6f7f8a;--ec:#e4ecf0;--eg:rgba(220,235,245,.4)}
.emblem.t-gold{--e1:#ffe08a;--e2:#9a6a12;--ec:#ffd36a;--eg:rgba(255,200,80,.55)}
.emblem.t-mythic{--e1:#f0a8ff;--e2:#5530b8;--ec:#e6b8ff;--eg:rgba(200,130,255,.65)}
.bp-art{filter:drop-shadow(0 0 22px var(--eg,rgba(0,0,0,0)))}
.emblem.big{width:150px}.emblem.big b{font-size:64px}
.emblem.mini{width:38px}.emblem.mini b{font-size:17px}.emblem.mini span{display:none}
/* banner */
.bp-hero{position:relative;overflow:hidden;display:flex;gap:28px;align-items:center;
padding:30px 32px;margin:6px 0 16px;border-radius:14px;border:1px solid rgba(185,162,245,.5);
background:radial-gradient(110% 140% at 0% 0%,rgba(140,95,245,.55),transparent 55%),
radial-gradient(80% 120% at 100% 110%,rgba(229,180,80,.35),transparent 60%),
linear-gradient(135deg,#21143d,#101a24 72%);
box-shadow:0 24px 70px -28px rgba(130,80,240,.8),inset 0 1px 0 rgba(255,255,255,.08)}
.bp-hero::before{content:"";position:absolute;inset:0;pointer-events:none;
background:repeating-linear-gradient(115deg,rgba(255,255,255,.035) 0 2px,transparent 2px 16px)}
.bp-hero::after{content:"";position:absolute;width:420px;height:420px;right:-140px;top:-200px;
pointer-events:none;border-radius:50%;
background:radial-gradient(circle,rgba(255,215,130,.22),transparent 65%)}
.bp-hero>*{position:relative;z-index:1}
.bp-art{--eg:rgba(185,140,255,.55)}
.bp-art:has(.t-bronze){--eg:rgba(240,160,90,.5)}.bp-art:has(.t-silver){--eg:rgba(220,235,245,.45)}
.bp-art:has(.t-gold){--eg:rgba(255,200,80,.6)}.bp-art:has(.t-mythic){--eg:rgba(210,140,255,.75)}
.bp-main{flex:1;min-width:0}
.eyebrow{margin:0 0 4px;font-size:13px;font-weight:600;letter-spacing:.14em;
text-transform:uppercase;color:#cbbcf7}.eyebrow .left{color:var(--gold)}
.bp-title{margin:0;font-size:clamp(40px,7vw,66px);line-height:.95;letter-spacing:.01em;
background:linear-gradient(180deg,#fff7e0 10%,#f0c766 55%,#b9822a 100%);
-webkit-background-clip:text;background-clip:text;color:transparent;
filter:drop-shadow(0 2px 0 rgba(0,0,0,.35))}
.bp-name{margin:6px 0 0;color:#e6b8ff;font-style:italic;font-weight:600;letter-spacing:.04em}
.bp-lead{margin:10px 0 16px;color:#d9d3ec;max-width:52ch}
.xpbar{position:relative;height:20px;margin-top:18px;border-radius:10px;overflow:hidden;
background:#0a0814;border:1px solid rgba(185,162,245,.35);box-shadow:inset 0 2px 6px #000}
.xpbar i{display:block;height:100%;border-radius:10px;
background:linear-gradient(90deg,#7a52f0,#c97cf0 55%,#ffd36a);
box-shadow:0 0 18px rgba(201,124,240,.8)}
.xpbar::after{content:"";position:absolute;inset:0;
background:repeating-linear-gradient(90deg,transparent 0 calc(10% - 2px),rgba(0,0,0,.5) calc(10% - 2px) 10%)}
.xprow{display:flex;justify-content:space-between;margin-top:6px;font-size:14px;color:#cbbcf7}
.xprow b{color:#fff}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 0}
.chips span{padding:5px 11px;border-radius:999px;background:rgba(10,8,20,.55);
border:1px solid rgba(185,162,245,.25);font-size:14px;color:#cbbcf7}.chips b{color:#fff}
.join{margin-top:16px}
.cta{display:inline-block;padding:12px 22px;border-radius:8px;font-weight:700;font-size:17px;
color:#1a1405;border:0;cursor:pointer;background:linear-gradient(180deg,#ffe08a,#e0a83a);
box-shadow:0 6px 22px -6px rgba(255,200,80,.8),inset 0 1px 0 rgba(255,255,255,.6)}
.cta:hover{text-decoration:none;filter:brightness(1.08)}
.ic{width:20px;height:20px;vertical-align:-4px}.ic.sm{width:16px;height:16px}
/* belöningsbana: en kolumn per nivå, gratis rad överst och champion-rad under */
.bp-track{padding-bottom:12px}
.lane{list-style:none;margin:0;padding:14px 4px 14px 0;display:flex;gap:8px;overflow-x:auto;
scroll-snap-type:x proximity;scrollbar-width:thin;
-webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 28px),transparent);
mask-image:linear-gradient(90deg,#000 calc(100% - 28px),transparent)}
.lane:focus-visible{outline:2px solid var(--gold)}
.labels{position:sticky;left:0;z-index:3;flex:none;width:30px;display:flex;flex-direction:column;
gap:8px;padding-top:30px;background:var(--panel)}
.labels span{flex:1;writing-mode:vertical-rl;transform:rotate(180deg);text-align:center;
font-size:11px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;border-radius:6px}
.labels .lf{color:#cbbcf7;background:rgba(185,162,245,.12);height:150px;flex:none}
.labels .lc{color:var(--gold);background:rgba(229,180,80,.14);height:150px;flex:none}
.col{position:relative;flex:none;width:62px;display:flex;flex-direction:column;gap:8px;
padding-top:30px;scroll-snap-align:center}
.col.big{width:148px}
.col .lvl{position:absolute;top:0;left:0;right:0;height:24px;display:grid;place-items:center;
font-family:"Barlow Condensed",sans-serif;font-size:17px;font-weight:700;color:var(--dim);
border-radius:6px;background:#0c141b}
.col.got .lvl{color:#fff;background:linear-gradient(90deg,#5b3cc4,#8d5ee0)}
.col.now .lvl{color:#1a1405;background:var(--gold);box-shadow:0 0 16px -2px rgba(229,180,80,.9)}
.cell{position:relative;height:150px;display:flex;flex-direction:column;align-items:center;
justify-content:center;gap:6px;padding:10px 8px;border-radius:10px;text-align:center;
border:1px solid var(--line);background:linear-gradient(180deg,#141f29,#0e161d)}
.col.big .cell{justify-content:flex-start}.col.big .cell.empty{justify-content:center}
.cell b{font-size:14px;line-height:1.15}
.cell small{font-size:11.5px;line-height:1.25;color:var(--dim)}
.cell .gem{width:16px;height:16px;transform:rotate(45deg);border-radius:3px;
background:#2a3642;border:1px solid #3a4a58}
.cell .st{position:absolute;right:6px;bottom:6px;width:13px;height:13px;color:#55687a}
.col.big .cell .st{top:7px;bottom:auto}
.cell .st.ok{color:var(--radiant)}
.cell .art{width:88px;height:62px;border-radius:8px;display:grid;place-items:center;flex:none;
background:radial-gradient(circle at 50% 35%,#2b2346,#120f1e 75%);
border:1px solid rgba(185,162,245,.25)}
.cell .art .big{width:36px;height:36px;color:#e6b8ff}
.cell .art.a-glow .big{color:#fff;filter:drop-shadow(0 0 8px #d7a0ff)}
.cell .pill{font-family:"Barlow Condensed",sans-serif;font-weight:700;font-size:19px;
padding:3px 11px;border-radius:5px;transform:rotate(-6deg);box-shadow:0 4px 14px rgba(0,0,0,.5)}
.pill.bronze{background:linear-gradient(180deg,#f0b47a,#a86a35);color:#1a1005}
.pill.silver{background:linear-gradient(180deg,#f6f9fb,#9aa9b4);color:#11181d}
.pill.gold{background:linear-gradient(180deg,#ffe58f,#d09a2c);color:#1a1405}
.pill.mythic{background:linear-gradient(135deg,#a77af5,#f0a0f0);color:#160d26}
.cell:not(.got) .art,.cell:not(.got) .gem{filter:grayscale(.85) brightness(.7)}
.cell.free.got{border-color:rgba(185,162,245,.5);background:linear-gradient(180deg,#241a3e,#120f1e)}
.cell.free.got .gem{background:linear-gradient(135deg,#b9a2f5,#7a52f0);border-color:#d6c8ff;
box-shadow:0 0 10px rgba(185,162,245,.8)}
.cell.champ{border-color:rgba(229,180,80,.22);
background:linear-gradient(180deg,rgba(229,180,80,.07),#0e161d)}
.cell.champ .art{border-color:rgba(229,180,80,.3);
background:radial-gradient(circle at 50% 35%,#3d2f12,#151008 75%)}
.cell.champ .art .big{color:#ffd36a}
.cell.champ:not(.got) .st{color:#8a6a2c}
.cell.champ.got{border-color:var(--gold);
background:linear-gradient(180deg,rgba(229,180,80,.26),#1a140a);
box-shadow:inset 0 0 18px rgba(229,180,80,.18)}
.cell.champ.got .gem{background:linear-gradient(135deg,#ffe08a,#c98b20);border-color:#ffe9a8;
box-shadow:0 0 10px rgba(255,200,80,.8)}
.col .fill{height:5px;border-radius:3px;background:#1c2731}
.col.got .fill{background:linear-gradient(90deg,#7a52f0,#c97cf0)}
.col.now{filter:drop-shadow(0 0 12px rgba(229,180,80,.35))}
.champbar{display:flex;gap:10px;align-items:center;padding:10px 14px;margin:0 0 4px;
border-radius:8px;border:1px dashed rgba(229,180,80,.45);color:#e8d6a8;font-size:14px;
background:rgba(229,180,80,.06)}
.champbar .ic,.champbar .crown{flex:none;color:var(--gold)}
.champbar.open{border-style:solid;border-color:var(--gold);
background:linear-gradient(90deg,rgba(229,180,80,.22),rgba(229,180,80,.04))}
/* champion: krona, skimrande märke, guldram */
.crown{width:18px;height:18px;margin-left:7px;vertical-align:-3px;color:var(--gold);
filter:drop-shadow(0 0 6px rgba(255,200,80,.7))}
.champbar .crown{margin:0;width:22px;height:22px}
.chips .champ{border-color:var(--gold);color:var(--gold);font-weight:700}
.chips .champ .crown{margin:0 2px 0 0;width:15px;height:15px}
.badge.shine{position:relative;overflow:hidden}
.badge.shine::after{content:"";position:absolute;inset:0;transform:translateX(-120%);
background:linear-gradient(105deg,transparent 30%,rgba(255,255,255,.85) 50%,transparent 70%);
animation:glans 2.6s ease-in-out infinite}
@keyframes glans{60%,100%{transform:translateX(120%)}}
.hexring{position:relative;flex:none;display:grid;place-items:center;padding:10px;overflow:hidden;
clip-path:polygon(50% 0,94% 25%,94% 75%,50% 100%,6% 75%,6% 25%)}
.hexring::before{content:"";position:absolute;inset:-40%;
background:conic-gradient(#fff2c0,#e0a83a,#7a4a10,#ffe08a,#fff2c0);animation:snurr 4s linear infinite}
.hexring::after{content:"";position:absolute;inset:5px;background:#0b0912;
clip-path:polygon(50% 0,94% 25%,94% 75%,50% 100%,6% 75%,6% 25%)}
.hexring .emblem{position:relative;z-index:1}
.hexring.mini{padding:5px}.hexring.mini::after{inset:2.5px}
@keyframes snurr{to{transform:rotate(1turn)}}
.bp-hero.is-champ{border-color:rgba(229,180,80,.7)}
/* nivå-upp */
.lvlup{position:fixed;inset:0;z-index:50;display:grid;place-items:center;overflow:hidden;
background:radial-gradient(circle at 50% 45%,rgba(60,30,120,.97),rgba(5,6,12,.99) 70%);
animation:tona .35s ease-out}
.lvlup.bye{animation:tonaut .35s ease-in forwards}
@keyframes tona{from{opacity:0}}@keyframes tonaut{to{opacity:0}}
.lvlup .rays{position:absolute;width:160vmax;height:160vmax;left:50%;top:45%;
margin:-80vmax 0 0 -80vmax;opacity:.35;animation:snurr 24s linear infinite;
background:repeating-conic-gradient(rgba(255,215,130,.5) 0 6deg,transparent 6deg 18deg);
-webkit-mask-image:radial-gradient(circle,#000 0,transparent 45%);
mask-image:radial-gradient(circle,#000 0,transparent 45%)}
.lvlup .box{position:relative;z-index:2;display:flex;flex-direction:column;align-items:center;
text-align:center;padding:24px;max-width:420px}
.lvlup .box>.emblem,.lvlup .box>.hexring{animation:popp .9s cubic-bezier(.2,1.6,.4,1) .15s both;
filter:drop-shadow(0 0 40px rgba(255,200,90,.7))}
@keyframes popp{from{transform:scale(.2) rotate(-25deg);opacity:0}}
.lvlup h2{margin:18px 0 0;font-family:"Barlow Condensed",sans-serif;font-size:62px;
font-style:italic;font-weight:700;line-height:1;color:transparent;
background:linear-gradient(180deg,#fff7e0 10%,#f0c766 55%,#b9822a);
-webkit-background-clip:text;background-clip:text;animation:upp .6s ease-out .55s both}
.lvlup .box>p,.lvlup ul,.lvlup .cta{animation:upp .6s ease-out .75s both}
@keyframes upp{from{opacity:0;transform:translateY(14px)}}
.lvlup p{margin:6px 0 0;color:#ddd3f5;font-size:18px}
.lvlup .unl{margin-top:18px;font-size:12px;font-weight:700;letter-spacing:.16em;
text-transform:uppercase;color:var(--gold)}
.lvlup ul{list-style:none;padding:0;margin:8px 0 22px;display:flex;flex-direction:column;gap:6px}
.lvlup li{padding:7px 14px;border-radius:8px;background:rgba(229,180,80,.12);
border:1px solid rgba(229,180,80,.4);font-weight:600}
.lvlup li .ic{color:var(--radiant)}
.lvlup .cta{margin-top:18px}
@media(max-width:560px){.lvlup h2{font-size:48px}}.lvlup ul+.cta{margin-top:0}
.sparks{position:absolute;left:50%;top:42%;z-index:1}
.sparks i{position:absolute;width:var(--s);height:var(--s);border-radius:2px;
background:linear-gradient(135deg,#fff3c4,#e0a83a);transform:rotate(45deg);opacity:0;
animation:gnista 1.6s ease-out var(--d) forwards}
.sparks i:nth-child(3n){background:linear-gradient(135deg,#f0d0ff,#9a6cf0)}
@keyframes gnista{0%{opacity:1;transform:translate(0,0) rotate(45deg) scale(.4)}
80%{opacity:1}100%{opacity:0;transform:translate(var(--x),var(--y)) rotate(225deg) scale(1)}}
@media(prefers-reduced-motion:reduce){.lvlup *,.lvlup,.hexring::before,.badge.shine::after{
animation:none!important}.sparks{display:none}}
/* utvalda hjältar */
.feats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.feat{position:relative;aspect-ratio:16/9;border-radius:10px;overflow:hidden;
border:1px solid rgba(229,180,80,.45);
background:radial-gradient(circle at 30% 20%,#3a2c52,#121a22 70%)}
.feat img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.feat::after{content:"";position:absolute;inset:0;
background:linear-gradient(180deg,transparent 40%,rgba(5,8,12,.92))}
.feat b{position:absolute;left:12px;bottom:9px;z-index:1;font-family:"Barlow Condensed",sans-serif;
font-size:22px;font-weight:700;font-style:italic;text-shadow:0 2px 6px #000}
.feat .rib{position:absolute;top:8px;left:8px;z-index:1;font-size:11px;font-weight:700;
letter-spacing:.08em;text-transform:uppercase;padding:2px 8px;border-radius:4px;
background:var(--gold);color:#1a1405}
/* uppdrag */
.qgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.qcard{position:relative;display:flex;gap:14px;align-items:flex-start;padding:14px;
border-radius:10px;border:1px solid var(--line);background:linear-gradient(180deg,#17242f,#111b23)}
.qic{flex:none;width:44px;height:44px;border-radius:10px;display:grid;place-items:center;
background:linear-gradient(135deg,#3b2d66,#1d1736);color:#d9c8ff;
box-shadow:inset 0 1px 0 rgba(255,255,255,.12)}
.qic .ic{width:24px;height:24px}
.q-utvald .qic,.q-guld .qic{background:linear-gradient(135deg,#6b4a12,#2c1f08);color:#ffd36a}
.q-svit .qic,.q-kills .qic{background:linear-gradient(135deg,#6a2618,#2a110c);color:#ff9a7a}
.q-rena .qic,.q-assists .qic{background:linear-gradient(135deg,#17504a,#0b2522);color:#8be6c9}
.qbody{flex:1;min-width:0}
.qtop{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.qt{font-weight:600;line-height:1.3}
.xp{flex:none;font-size:13px;font-weight:700;padding:2px 8px;border-radius:999px;
background:rgba(185,162,245,.16);color:#d9c8ff}
.qbar{height:8px;margin-top:10px;border-radius:4px;background:#0a0f14;overflow:hidden}
.qbar i{display:block;height:100%;border-radius:4px;background:linear-gradient(90deg,#7a52f0,#c97cf0)}
.qnum{margin-top:4px;font-size:13px;color:var(--dim)}
.qcard.done{border-color:rgba(229,180,80,.6);
background:linear-gradient(180deg,rgba(229,180,80,.12),#111b23 70%)}
.qcard.done .qbar i{background:linear-gradient(90deg,#e0a83a,#ffe08a)}
.qcard.done .xp{background:var(--gold);color:#1a1405}
.stamp{position:absolute;right:12px;bottom:10px;font-size:11px;font-weight:800;
letter-spacing:.12em;text-transform:uppercase;color:var(--gold);border:2px solid var(--gold);
border-radius:4px;padding:0 6px;transform:rotate(-8deg);opacity:.9}
/* stängd beta */
.beta{margin:6px 0 18px;padding:14px 18px;border-radius:10px;border:1px solid rgba(229,180,80,.5);
background:linear-gradient(100deg,rgba(229,180,80,.16),var(--panel) 65%)}
.beta p{margin:6px 0 0}
.betatag{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
padding:2px 9px;border-radius:4px;background:var(--gold);color:#1a1405}
.betabar{height:8px;margin-top:10px;border-radius:4px;background:#0a0f14;overflow:hidden}
.betabar i{display:block;height:100%;background:linear-gradient(90deg,#e0a83a,#ffe08a)}
/* veckouppdrag */
.card.weekly{border-color:rgba(120,200,255,.35);
background:linear-gradient(180deg,rgba(90,170,230,.10),var(--panel) 45%)}
.card.weekly h2{display:flex;align-items:center;gap:10px}
.wkcount{font-size:13px;font-weight:700;padding:2px 9px;border-radius:999px;
background:rgba(159,212,232,.15);color:var(--link)}
.wkcount.all{background:var(--radiant);color:#0b1a12}
.reset{font-size:14px;font-weight:600;color:var(--link)}
.chips.weeks{margin-top:6px}.chips.weeks span.all{border-color:var(--radiant);color:var(--radiant)}
/* topplista */
.whocell{display:inline-flex;align-items:center;gap:12px}
.ttl{display:block;font-size:12px;color:#e6b8ff;font-style:italic}
.ladder .xpcol{font-family:"Barlow Condensed",sans-serif;font-size:28px;font-weight:700;
color:#d9c8ff;font-variant-numeric:tabular-nums}
.ladder tr.p1 .xpcol{color:var(--gold)}
@media(max-width:560px){.bp-hero{flex-direction:column;align-items:flex-start;gap:16px;
padding:22px 18px}.emblem.big{width:110px}.emblem.big b{font-size:46px}
.feats{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.feat b{font-size:15px;left:7px}
.feat .rib{display:none}.qgrid{grid-template-columns:1fr}.ladder .xpcol{font-size:22px}}
@media(prefers-reduced-motion:no-preference){.xpbar i{animation:fyll 1.1s ease-out}
@keyframes fyll{from{width:0}}}
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
.stats{gap:18px}.stats b{font-size:32px}.tiles{gap:12px;grid-template-columns:repeat(2,minmax(0,1fr))}
.tile{padding:16px}.tile .what{font-size:14px}
.tile.pass{grid-column:1/-1;aspect-ratio:auto;min-height:150px}.ladder .lvl{font-size:26px}}
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
            for k, v in LÄGEN.items()) + (
            f"<a class='pass {'on' if läge == 'pass' else ''}' href='/pass'>Pass</a></nav>")
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
    n = len(pass_anmälda(c, månad))
    spelare = ("Nobody has joined yet" if not n
               else f"{n} player{'s' if n != 1 else ''} joined this month")
    knappar += (f"<a class='tile pass' href='/pass'><span class='name'>{PASS_NAMN}</span>"
                f"<span class='what'>Free monthly pass: quests, levels and badges.</span>"
                f"<span class='count'>{spelare}</span></a>")
    return sida(SAJTNAMN, f"""
<h1>Choose a ladder</h1>
{beta_html(c) if not inloggad() else ''}
<p class='sub'>Monthly Dota 2 ladders. Everyone starts at {START} MMR on the 1st of every
month, and you can join both ladders. The {PASS_NAMN} gives XP for every match in
either ladder.</p>
<div class='tiles'>{knappar}</div>""")


def platser_kvar(c):
    """Lediga platser i betan, eller None om det inte finns något tak."""
    if not MAX_SPELARE:
        return None
    return max(0, MAX_SPELARE - c.execute("SELECT COUNT(*) FROM members").fetchone()[0])


def ge_plats(c, pid):
    """True om spelaren har (eller nu fick) en plats. Annars hamnar hen på väntelistan."""
    if c.execute("SELECT 1 FROM members WHERE player_id=?", (pid,)).fetchone():
        return True
    if MAX_SPELARE and platser_kvar(c) == 0:
        c.execute("INSERT OR IGNORE INTO waitlist VALUES(?,?)", (pid, int(time.time())))
        c.commit()
        return False
    c.execute("INSERT OR IGNORE INTO members VALUES(?,?)", (pid, int(time.time())))
    c.execute("DELETE FROM waitlist WHERE player_id=?", (pid,))
    c.commit()
    return True


def beta_html(c):
    """Rutan om den stängda betan på startsidan."""
    kvar = platser_kvar(c)
    if kvar is None:
        return ""
    if kvar:
        text = (f"<b>{kvar} of {MAX_SPELARE} spots left.</b> Sign in through Steam to grab "
                f"one. Once you are in, your spot is yours.")
    else:
        text = ("<b>All spots are taken.</b> Sign in through Steam anyway to join the waitlist; "
                "we let more players in as the beta grows.")
    fyllt = round(100 * (MAX_SPELARE - kvar) / MAX_SPELARE)
    return (f"<div class='beta'><span class='betatag'>Closed beta</span><p>{text}</p>"
            f"<div class='betabar' role='progressbar' aria-valuemin='0' "
            f"aria-valuemax='{MAX_SPELARE}' aria-valuenow='{MAX_SPELARE - kvar}' "
            f"aria-label='Beta spots taken'><i style='width:{fyllt}%'></i></div></div>")


def sida_väntelista(c, pid):
    """Sidan för den som loggade in när betan var full."""
    plats = c.execute("SELECT COUNT(*) FROM waitlist WHERE joined <= (SELECT joined FROM "
                      "waitlist WHERE player_id=?)", (pid,)).fetchone()[0]
    return sida("Waitlist", f"""
<h1>The beta is full</h1>
<p class='sub'>All {MAX_SPELARE} spots in the closed beta are taken. You are on the waitlist
as number <b>{plats}</b>.</p>
<div class='card'><p>When more spots open, sign in through Steam again and you will get in
automatically. Until then you can still look at the ladders and the
<a href='/pass'>{PASS_NAMN}</a>.</p></div>""")


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
        namn_länk = namn_html(pass_info(c, pid, månad), namn, f"/{läge}/player/{pid}{fråga}")
        rader += (f"<tr class='{klass}'><td class='pos'>{i}</td>"
                  f"<td class='who'>{namn_länk}{kal}</td>"
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
    märke = pass_märke(c, pid, månad)       # passmärket leder till spelarens passida
    if märke:
        märke = f"<a href='/pass/player/{pid}{fråga}'>{märke}</a>"
    return sida(f"{namn} in {lägesnamn}", f"""
<a class='back' href='/{läge}{fråga}'>Back to the {lägesnamn} leaderboard</a>
<h1>{html.escape(namn)}{märke}</h1>
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


# ---------- Ladder Pass-sidor ----------
# Enkla ikoner (SVG, 24x24) till uppdragen och belöningarna. Färgen kommer från CSS.
_IKON = {
    "utvald": "<path d='M3 8l4.5 4L12 5l4.5 7L21 8l-2 11H5z'/>",
    "spela": "<path d='M7 4.5v15l12.5-7.5z'/>",
    "turbo": "<path d='M13.5 2L4 14h7l-1.5 8L20 10h-7z'/>",
    "sd": ("<rect x='3.5' y='3.5' width='17' height='17' rx='3.5' fill='none' "
           "stroke='currentColor' stroke-width='2'/><circle cx='8.5' cy='8.5' r='1.7'/>"
           "<circle cx='15.5' cy='15.5' r='1.7'/><circle cx='15.5' cy='8.5' r='1.7'/>"
           "<circle cx='8.5' cy='15.5' r='1.7'/>"),
    "kills": ("<path d='M12 2.5c-4.7 0-8 3.2-8 7.6 0 2.6 1.2 4.4 3 5.5V19a1.5 1.5 0 0 0 1.5 "
              "1.5h7A1.5 1.5 0 0 0 17 19v-3.4c1.8-1.1 3-2.9 3-5.5 0-4.4-3.3-7.6-8-7.6z"
              "M8.8 9a2 2 0 1 1 0 4 2 2 0 0 1 0-4zm6.4 0a2 2 0 1 1 0 4 2 2 0 0 1 0-4z'/>"),
    "assists": ("<circle cx='8' cy='8' r='3.2'/><circle cx='16.5' cy='9' r='2.7'/>"
                "<path d='M2 19.5c0-3.6 2.7-6 6-6s6 2.4 6 6zM13.6 19.5c.1-2.3-.6-4-1.8-5.2 "
                "1.2-.8 2.6-1.2 4.7-1.2 3 0 5.5 2.1 5.5 6.4z'/>"),
    "rena": "<path d='M12 2l8 3v6.2c0 5-3.4 9-8 10.8-4.6-1.8-8-5.8-8-10.8V5z'/>",
    "hjältar": ("<rect x='3' y='3' width='8' height='8' rx='2'/><rect x='13' y='3' width='8' "
                "height='8' rx='2'/><rect x='3' y='13' width='8' height='8' rx='2'/>"
                "<rect x='13' y='13' width='8' height='8' rx='2'/>"),
    "svit": ("<path d='M12 2c1 3.6 5.5 5.7 5.5 11a5.5 5.5 0 0 1-11 0c0-2.4 1-4.2 2.4-5.6.2 "
             "1.8 1 3 2.3 3.6C10.6 8.3 11 5 12 2z'/>"),
    "guld": ("<circle cx='12' cy='12' r='9.5'/><circle cx='12' cy='12' r='6' fill='none' "
             "stroke='rgba(0,0,0,.35)' stroke-width='1.6'/>"),
    "kda": "<path d='M12 2.5l2.9 6 6.6.8-4.9 4.6 1.3 6.6L12 17.2l-5.9 3.3 1.3-6.6-4.9-4.6 6.6-.8z'/>",
    "title": ("<path d='M6 3h12a2 2 0 0 1 2 2v13l-3-1.8L14 18l-2-1.6L10 18l-3-1.8L4 18V5a2 2 0 "
              "0 1 2-2z'/><path d='M8 8h8M8 11.5h6' stroke='rgba(0,0,0,.4)' stroke-width='1.6'/>"),
    "glow": ("<path d='M12 1.5l1.8 6.2 6.2 1.8-6.2 1.8L12 17.5l-1.8-6.2L4 9.5l6.2-1.8z'/>"
             "<path d='M19 15l.9 2.6 2.6.9-2.6.9L19 22l-.9-2.6-2.6-.9 2.6-.9z'/>"),
    "lås": ("<rect x='5' y='10.5' width='14' height='10' rx='2'/><path d='M8 10.5V8a4 4 0 0 1 8 "
            "0v2.5' fill='none' stroke='currentColor' stroke-width='2.2'/>"),
    "bock": "<path d='M4.5 12.5l4.5 4.5L19.5 6.5' fill='none' stroke='currentColor' stroke-width='3'/>",
}


def ikon(namn, klass="ic"):
    return (f"<svg class='{klass}' viewBox='0 0 24 24' fill='currentColor' aria-hidden='true'>"
            f"{_IKON.get(namn, _IKON['kda'])}</svg>")


def emblem_html(nivå, klass="", ram=False):
    """Sexkantigt nivåemblem i nivåns färg (brons, silver, guld, mythic).
    ram = champion-ramen: en roterande guldring runt emblemet."""
    emblem = (f"<div class='emblem t-{nivåklass(nivå)} {klass}' role='img' "
              f"aria-label='Level {nivå}'><div class='in'><b>{nivå}</b><span>Level</span>"
              f"</div></div>")
    return f"<div class='hexring {klass}'>{emblem}</div>" if ram else emblem


def dagar_kvar(månad):
    """'12 days left' för innevarande månad, annars 'Season ended'."""
    if månad != nu_månad():
        return "Season ended"
    kvar = max(0, månadsgränser(månad)[1] - time.time())
    if kvar < 86400:
        return f"{max(1, int(kvar // 3600))} hours left"
    dagar = int(-(-kvar // 86400))
    return f"{dagar} day{'s' if dagar != 1 else ''} left"


def banner_html(månad, s=None, namn=None, knapp="", fel=""):
    """Stora bannern överst: emblem, säsong, nivå och XP-stapel."""
    säsong = (f"<p class='eyebrow'>Season &middot; {månadsnamn(månad)} &middot; "
              f"<span class='left'>{dagar_kvar(månad)}</span></p>")
    if s is None:           # inte inloggad: visa passet utan egen nivå
        return (f"<section class='bp-hero'><div class='bp-art'>{emblem_html(0, 'big')}</div>"
                f"<div class='bp-main'>{säsong}<h1 class='bp-title'>{PASS_NAMN}</h1>"
                f"<p class='bp-lead'>Free for everyone. Every Turbo and Single Draft match "
                f"gives XP, quests give more, and every level unlocks something.</p>"
                f"<a class='cta' href='/login?next=/pass'>Sign in through Steam to start</a>"
                f"{ruta(fel, 'err')}<p class='note'>Turn on \"Expose Public Match Data\" in "
                f"Dota first.</p></div></section>")
    in_i_nivå = s["xp"] % NIVÅ_XP
    procent = round(100 * in_i_nivå / NIVÅ_XP)
    t = titel(s["nivå"], s["mästare"])
    rubrik = (f"<h1 class='bp-title'>{html.escape(namn)}</h1>" if namn
              else f"<h1 class='bp-title'>{PASS_NAMN}</h1>")
    mästare = (f"<span class='champ'>{krona_html()} Champion</span>" if s["mästare"] else
               f"<span><b>{s['klara']}/{len(s['uppdrag'])}</b> quests</span>")
    return (f"<section class='bp-hero {'is-champ' if s['mästare'] else ''}'><div class='bp-art'>"
            f"{emblem_html(s['nivå'], 'big', har(s, RAM_NIVÅ))}"
            f"</div><div class='bp-main'>{säsong}{rubrik}"
            f"{'<p class=bp-name>' + html.escape(t) + '</p>' if t else ''}"
            f"<div class='xpbar' role='progressbar' aria-valuemin='0' aria-valuemax='{NIVÅ_XP}' "
            f"aria-valuenow='{in_i_nivå}' aria-label='XP toward level {s['nivå'] + 1}'>"
            f"<i style='width:{procent}%'></i></div>"
            f"<div class='xprow'><span>Level {s['nivå']} &rarr; {s['nivå'] + 1}</span>"
            f"<span><b>{in_i_nivå}</b> / {NIVÅ_XP} XP</span></div>"
            f"<div class='chips'><span><b>{s['xp']}</b> total XP</span>{mästare}"
            f"<span><b>{s['n']}</b> matches</span></div>"
            f"{knapp}{ruta(fel, 'err')}</div></section>")


def _belöningsbild(sort, lv):
    """Bilden i en belöningsruta: ett märke eller en ikon."""
    if sort in ("bronze", "silver", "gold", "mythic"):
        return f"<div class='art a-{sort}'><span class='pill {sort}'>Lv {lv}</span></div>"
    if sort == "shine":
        return "<div class='art a-shine'><span class='pill gold shine'>Lv 20</span></div>"
    if sort == "frame":
        return f"<div class='art a-frame'>{emblem_html(lv, 'mini', ram=True)}</div>"
    if sort == "crown":
        return f"<div class='art a-crown'>{ikon('utvald', 'big')}</div>"
    return f"<div class='art a-{sort}'>{ikon(sort, 'big')}</div>"


def spår_html(s=None):
    """Belöningsbanan: en kolumn per nivå med en gratis rad och en champion-rad.
    s = spelarens pass_status, eller None när ingen är inloggad."""
    nivå = s["nivå"] if s else None
    mästare = bool(s and s["mästare"])
    gratis = {lv: (sort, n, t) for lv, sort, n, t in BELÖNINGAR}
    extra = {lv: (sort, n, t) for lv, sort, n, t in MÄSTAR_BELÖNINGAR}

    def cell(rad, lv, belöning, öppen):
        fått = öppen and nivå is not None and nivå >= lv
        status = ikon("bock", "st ok") if fått else ikon("lås", "st")
        if not belöning:
            return (f"<div class='cell {rad} empty {'got' if fått else ''}'><div class='gem'></div>"
                    f"{status}</div>")
        sort, namn, text = belöning
        return (f"<div class='cell {rad} {'got' if fått else ''}'>{_belöningsbild(sort, lv)}"
                f"<b>{html.escape(namn)}</b><small>{html.escape(text)}</small>{status}</div>")

    kolumner = ("<li class='labels' aria-hidden='true'><span class='lf'>Free</span>"
                "<span class='lc'>Champion</span></li>")
    for lv in range(1, PASS_MAXNIVÅ + 1):
        stor = lv in gratis or lv in extra
        klass = ("big " if stor else "") + ("got " if nivå is not None and nivå >= lv else "") + \
                ("now" if nivå is not None and lv == nivå + 1 else "")
        kolumner += (f"<li class='col {klass}'><span class='lvl'>{lv}</span>"
                     f"{cell('free', lv, gratis.get(lv), True)}"
                     f"{cell('champ', lv, extra.get(lv), mästare)}<span class='fill'></span></li>")

    mål = ""
    if nivå is not None:
        alla = sorted([(lv, n) for lv, _, n, _ in BELÖNINGAR] +
                      ([(lv, n) for lv, _, n, _ in MÄSTAR_BELÖNINGAR] if mästare else []))
        nästa = next(((lv, n) for lv, n in alla if lv > nivå), None)
        mål = (f"<span class='dim'>Next reward: <b>{html.escape(nästa[1])}</b> at level "
               f"{nästa[0]}</span>" if nästa else "<span class='ok'>Every reward unlocked!</span>")
    if mästare:
        lås = (f"<div class='champbar open'>{krona_html()}<span><b>Champion track unlocked.</b> "
               f"You completed every quest this month, so the gold row is yours too.</span></div>")
    else:
        klara = f" You have {s['klara']} of {len(s['uppdrag'])}." if s else ""
        lås = (f"<div class='champbar'>{ikon('lås', 'ic')}<span><b>Champion track:</b> complete "
               f"all {PASS_UPPDRAG} quests this month to unlock the gold row.{klara}</span></div>")
    return (f"<section class='card bp-track'><div class='head'><h2>Rewards</h2>{mål}</div>{lås}"
            f"<ol class='lane' tabindex='0' aria-label='Reward track'>{kolumner}</ol>"
            f"<p class='note'>Swipe or scroll sideways. Rewards live on this site and last for "
            f"the month.</p></section>"
            # Rulla banan så att nästa nivå syns, utan att flytta sidan i höjdled.
            f"<script>(function(){{var l=document.querySelector('.lane'),n=l&&l.querySelector"
            f"('.now');if(n)l.scrollLeft=n.offsetLeft-l.clientWidth/2+n.offsetWidth/2;}})();"
            f"</script>")


def nivåupp_html(c, pid, månad, s):
    """Nivå-upp-animationen: visas när spelaren öppnar sitt pass och har gått upp i nivå
    (eller låst upp champion-raden) sedan förra besöket. Första besöket visar ingenting."""
    if månad != nu_månad():
        return ""
    rad = c.execute("SELECT level,champion FROM pass_seen WHERE player_id=? AND month=?",
                    (pid, månad)).fetchone()
    c.execute("INSERT OR REPLACE INTO pass_seen VALUES(?,?,?,?)",
              (pid, månad, s["nivå"], int(s["mästare"])))
    c.commit()
    if not rad:
        return ""
    gammal, var_mästare = rad
    ny_mästare = s["mästare"] and not var_mästare
    if s["nivå"] <= gammal and not ny_mästare:
        return ""
    # Belöningar som blivit upplåsta sedan förra besöket.
    nya = [n for lv, _, n, _ in BELÖNINGAR if gammal < lv <= s["nivå"]]
    if s["mästare"]:
        nya += [n for lv, _, n, _ in MÄSTAR_BELÖNINGAR
                if lv <= s["nivå"] and (ny_mästare or lv > gammal)]
    rubrik = "Level up!" if s["nivå"] > gammal else "Champion!"
    under = (f"You reached level {s['nivå']}" if s["nivå"] > gammal
             else "You completed every quest this month")
    if ny_mästare and s["nivå"] > gammal:
        under += " and unlocked the Champion track"
    lista = "".join(f"<li>{ikon('bock', 'ic sm')} {html.escape(n)}</li>" for n in nya)
    rng = random.Random(pid)
    gnistor = "".join(
        f"<i style='--x:{rng.randint(-260, 260)}px;--y:{rng.randint(-220, 200)}px;"
        f"--d:{rng.randint(0, 400)}ms;--s:{rng.randint(5, 11)}px'></i>" for _ in range(28))
    return (f"<div class='lvlup' id='lvlup' role='dialog' aria-modal='true' "
            f"aria-labelledby='lvlup-t'><div class='rays'></div><div class='sparks'>{gnistor}"
            f"</div><div class='box'>{emblem_html(s['nivå'], 'big', har(s, RAM_NIVÅ))}"
            f"<h2 id='lvlup-t'>{rubrik}</h2><p>{under}.</p>"
            + (f"<p class='unl'>Unlocked</p><ul>{lista}</ul>" if lista else "")
            + "<button class='cta' id='lvlup-ok'>Continue</button></div></div>"
            "<script>(function(){var d=document.getElementById('lvlup'),b=document."
            "getElementById('lvlup-ok');function stang(){d.classList.add('bye');"
            "setTimeout(function(){d.remove()},350)}b.focus();b.onclick=stang;"
            "d.onclick=function(e){if(e.target===d)stang()};document.addEventListener("
            "'keydown',function(e){if(e.key==='Escape')stang()})})();</script>")


def utvalda_html(c, månad):
    """Månadens utvalda hjältar som stora porträtt."""
    hj = hjältar(c)
    kort = ""
    for h in pass_hjältar(c, månad):
        namn, img, _ = hj.get(h, (f"Hero {h}", "", ""))
        bild = (f"<img src='{BILDER}{html.escape(img)}' alt='' loading='lazy' "
                f"onerror=\"this.remove()\">" if img else "")
        kort += (f"<div class='feat'>{bild}<span class='rib'>Featured</span>"
                 f"<b>{html.escape(namn)}</b></div>")
    if not kort:
        kort = "<p class='dim'>Not picked yet: the hero list is still loading.</p>"
    xp, _, text = UPPDRAG["utvald"]
    return (f"<section class='card'><div class='head'><h2>Featured heroes</h2>"
            f"<span class='dim'>Wins with them count toward a +{xp} XP quest</span></div>"
            f"<div class='feats'>{kort}</div></section>")


def uppdragskort(lista, med_framsteg):
    """Uppdragskorten. med_framsteg = visa spelarens stapel och 'Complete'-stämpel."""
    kort = ""
    for u in lista:
        text = html.escape(UPPDRAG[u["typ"]][2].format(n=u["n"]))
        klar = bool(med_framsteg and u["klar"])
        framsteg = ""
        if med_framsteg:
            procent = round(100 * u["framsteg"] / u["n"])
            framsteg = (f"<div class='qbar' role='progressbar' aria-valuemin='0' "
                        f"aria-valuemax='{u['n']}' aria-valuenow='{u['framsteg']}' "
                        f"aria-label='{text}'><i style='width:{procent}%'></i></div>"
                        f"<div class='qnum'>{u['framsteg']} / {u['n']}</div>")
        kort += (f"<div class='qcard q-{u['typ']} {'done' if klar else ''}'>"
                 f"<div class='qic'>{ikon(u['typ'])}</div><div class='qbody'>"
                 f"<div class='qtop'><span class='qt'>{text}</span>"
                 f"<span class='xp'>+{u['xp']} XP</span></div>{framsteg}</div>"
                 f"{'<span class=stamp>Complete</span>' if klar else ''}</div>")
    return f"<div class='qgrid'>{kort}</div>"


def tid_kvar(sekunder):
    """'3 days' eller '5 hours' (minst 1 timme)."""
    if sekunder >= 86400:
        d = int(-(-sekunder // 86400))
        return f"{d} day{'s' if d != 1 else ''}"
    h = max(1, int(-(-sekunder // 3600)))
    return f"{h} hour{'s' if h != 1 else ''}"


def veckouppdrag_html(månad, status=None):
    """Veckans uppdrag (innevarande månad) och en rad per tidigare vecka i månaden."""
    veckor = status["veckor"] if status else [
        {"måndag": m, "start": a, "slut": b, "uppdrag": vecko_uppdrag(m),
         "nu": a <= time.time() < b} for m, a, b in pass_veckor(månad)]
    nu = next((v for v in veckor if v["nu"]), None)
    del_html = ""
    if nu:
        klara = sum(u.get("klar", False) for u in nu["uppdrag"]) if status else 0
        räknare = (f"<span class='wkcount {'all' if klara == len(nu['uppdrag']) else ''}'>"
                   f"{klara}/{len(nu['uppdrag'])} done</span>" if status else "")
        del_html = (f"<div class='head'><h2>Weekly quests {räknare}</h2>"
                    f"<span class='reset'>{ikon('turbo', 'ic sm')} New quests in "
                    f"{tid_kvar(nu['slut'] - time.time())}</span></div>"
                    f"{uppdragskort(nu['uppdrag'], bool(status))}")
    tidigare = [v for v in veckor if v["slut"] <= time.time()]
    if status and tidigare:
        rader = ""
        for v in tidigare:
            klara = sum(u["klar"] for u in v["uppdrag"])
            xp = sum(u["xp"] for u in v["uppdrag"] if u["klar"])
            dag = time.gmtime(v["start"])
            rader += (f"<span class='{'all' if klara == len(v['uppdrag']) else ''}'>"
                      f"{MÅNADER[dag.tm_mon - 1][:3]} {dag.tm_mday}: <b>{klara}/"
                      f"{len(v['uppdrag'])}</b> &middot; +{xp} XP</span>")
        del_html += (f"<p class='note'>Earlier weeks this month</p>"
                     f"<div class='chips weeks'>{rader}</div>")
    if not del_html:
        return ""
    return f"<section class='card weekly'>{del_html}</section>"


def uppdrag_html(månad, status=None):
    """Veckans uppdrag och månadens uppdrag som kort. Med status visas framstegen."""
    lista = status["uppdrag"] if status else pass_uppdrag(månad)
    return (veckouppdrag_html(månad, status) +
            f"<section class='card'><div class='head'><h2>Monthly quests</h2><span class='dim'>"
            f"Complete all {len(lista)} to unlock the Champion track</span></div>"
            f"{uppdragskort(lista, bool(status))}"
            f"<p class='note'>Every match also gives XP: +{XP_VINST} for a win, "
            f"+{XP_FÖRLUST} for a loss.</p></section>")


def pass_knapp(c, pid):
    """Gå med i eller lämna passet för innevarande månad (bara för spelaren själv)."""
    nu = pass_anmälan(c, pid, nu_månad())
    post = f"<form class='row join' method='post' action='/pass/list/{pid}'>"
    if nu:
        return (f"{post}<input type='hidden' name='listed' value='0'><span class='ok'>"
                f"{ikon('bock', 'ic sm')} You are in the pass</span>"
                f"<button class='quiet'>Leave</button></form>")
    return (f"{post}<input type='hidden' name='listed' value='1'>"
            f"<button class='cta'>{'Rejoin' if nu is False else 'Join'} the pass, free</button>"
            f"<span class='dim'>Join to get your badge and a place on the leaderboard."
            f"</span></form>")


def pass_topplista(c, månad):
    """(rader-html, ledarens namn eller None) för passets topplista."""
    fråga = "" if månad == nu_månad() else f"?month={månad}"
    stat = [(pid, namn, pass_status(c, pid, månad)) for pid, namn in c.execute(
        "SELECT p.id,p.name FROM players p JOIN entries e ON e.player_id=p.id "
        "WHERE e.month=? AND e.mode=? AND e.active=1", (månad, PASS)).fetchall()]
    stat.sort(key=lambda x: (x[2]["xp"], x[2]["klara"]), reverse=True)
    rader = ""
    for i, (pid, namn, s) in enumerate(stat, 1):
        klass = f"p{i}" if i <= 3 and s["xp"] else ""
        glöd = " class='glow'" if s["nivå"] >= GLÖD_NIVÅ else ""
        t = titel(s["nivå"], s["mästare"])
        krona = krona_html() if har(s, KRONA_NIVÅ) else ""
        rader += (f"<tr class='{klass}'><td class='pos'>{i}</td>"
                  f"<td class='who'><span class='whocell'>"
                  f"{emblem_html(s['nivå'], 'mini', har(s, RAM_NIVÅ))}"
                  f"<span><a{glöd} href='/pass/player/{pid}{fråga}'>{html.escape(namn)}</a>{krona}"
                  f"{'<small class=ttl>' + t + '</small>' if t else ''}</span></span></td>"
                  f"<td class='r xpcol'>{s['xp']}</td><td class='r hide-s'>{s['klara']}/"
                  f"{len(s['uppdrag'])}</td><td class='r hide-s'>{s['n']}</td></tr>")
    if not rader:
        rader = (f"<tr><td colspan=5 class='dim wide'>Nobody has joined the {PASS_NAMN} for "
                 f"{månadsnamn(månad)} yet.</td></tr>")
    ledare = stat[0][1] if stat and stat[0][2]["xp"] else None
    return rader, ledare


def sida_pass(c, månad, fel=""):
    """Passets startsida: banner, belöningsbana, utvalda hjältar, uppdrag och topplista."""
    jag = inloggad()
    finns = jag and c.execute("SELECT 1 FROM players WHERE id=?", (jag,)).fetchone()
    s = pass_status(c, jag, månad) if finns else None
    knapp = pass_knapp(c, jag) if finns and månad == nu_månad() else ""
    rader, ledare = pass_topplista(c, månad)
    leder = (f"Right now that is <b>{html.escape(ledare)}</b>." if ledare
             else "Nobody has any XP yet.")
    pris = (f"<div class='prize'><img src='{PASS_PRIS_BILD}' alt='' width='96' height='96' "
            f"onerror=\"this.style.display='none'\"><div><h2>1st prize</h2>"
            f"<p class='what'>{html.escape(PASS_PRIS_TEXT)}</p><p class='note'>"
            + (f"Most pass XP when {månadsnamn(månad)} ends wins it. {leder}"
               if månad == nu_månad() else
               f"It went to whoever had the most pass XP in {månadsnamn(månad)}.")
            + "</p></div></div>")
    egen = (f"<p class='note'><a href='/pass/player/{jag}"
            f"{'' if månad == nu_månad() else '?month=' + månad}'>Open your pass page</a></p>"
            if finns else "")
    upp = nivåupp_html(c, jag, månad, s) if finns else ""
    return sida(PASS_NAMN, f"""{upp}
{månadsväljare(månad, '/pass')}
{banner_html(månad, s, knapp=knapp, fel=fel)}
{egen}
{spår_html(s)}
{utvalda_html(c, månad)}
{uppdrag_html(månad, s)}
{pris}
<section class='card wrap'><div class='head'><h2>Pass leaderboard</h2></div>
<table class='ladder'><tr><th>#</th><th>Player</th><th class='r'>XP</th>
<th class='r hide-s'>Quests</th><th class='r hide-s'>Matches</th></tr>
{rader}</table></section>""", "pass", "/pass")


def sida_pass_spelare(c, pid, månad):
    """En spelares pass: banner med nivå, belöningsbana och uppdrag med framsteg."""
    p = c.execute("SELECT name,updated FROM players WHERE id=?", (pid,)).fetchone()
    if not p:
        return None
    fråga = "" if månad == nu_månad() else f"?month={månad}"
    s = pass_status(c, pid, månad)
    if inloggad() == pid:
        knapp = pass_knapp(c, pid) if månad == nu_månad() else ""
    elif pass_anmälan(c, pid, nu_månad()):
        knapp = f"<p class='ok'>In the {PASS_NAMN} for {månadsnamn(nu_månad())}.</p>"
    else:
        knapp = f"<p class='dim'>Not in the {PASS_NAMN} for {månadsnamn(nu_månad())}.</p>"
    tid = time.strftime("%Y-%m-%d %H:%M", time.localtime(p[1])) if p[1] else "never"
    profil = " &middot; ".join(f"<a href='/{k}/player/{pid}{fråga}'>{v[1]} profile</a>"
                               for k, v in LÄGEN.items())
    upp = nivåupp_html(c, pid, månad, s) if inloggad() == pid else ""
    return sida(f"{p[0]} in the {PASS_NAMN}", f"""{upp}
<a class='back' href='/pass{fråga}'>Back to the {PASS_NAMN}</a>
{månadsväljare(månad, f'/pass/player/{pid}')}
{banner_html(månad, s, p[0], knapp)}
<p class='note'>Last updated {tid}. New matches are fetched automatically when someone opens
the page. {profil}</p>
{spår_html(s)}
{uppdrag_html(månad, s)}
{utvalda_html(c, månad)}""", "pass", f"/pass/player/{pid}")


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
                köa_uppdatering(c, anmälda(c), färsk_lista(len(anmälda(c))))
                return self._skicka(200, med_status(sida_meny(c)))
            läge, rest = delar[0], delar[1:]
            if läge == "pass":
                if not rest:
                    köa_uppdatering(c, pass_anmälda(c), färsk_lista(len(anmälda(c))))
                    return self._skicka(200, med_status(sida_pass(c, månad)))
                if len(rest) == 2 and rest[0] == "player" and rest[1].isdigit():
                    s = sida_pass_spelare(c, int(rest[1]), månad)
                    if s:
                        köa_uppdatering(c, [int(rest[1])], FÄRSK_PROFIL)
                        return self._skicka(200, med_status(s))
            if läge in LÄGEN:
                if not rest:
                    köa_uppdatering(c, anmälda(c, läge), färsk_lista(len(anmälda(c))))
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
        if not ge_plats(c, pid):            # betan är full: väntelistan, ingen inloggning
            return self._skicka(200, sida_väntelista(c, pid))
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
            if len(delar) == 3 and delar[:2] == ["pass", "list"] and delar[2].isdigit():
                pid = int(delar[2])
                if inloggad() != pid:           # bara spelaren själv
                    return self._nekad()
                if not c.execute("SELECT 1 FROM players WHERE id=?", (pid,)).fetchone():
                    return self._404()
                pass_anmäl(c, pid, vill_listas)
                return self._redirect(f"/pass/player/{pid}")
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
