"""
Dota 2 Turbo-ranking, prototyp.

Användning:
    python3 turbo_rank.py 123456789        (Mac)
    py turbo_rank.py 123456789             (Windows)
    ... 123456789 987654321 555555555      (flera spelare -> topplista)

ID = spelarens Steam32 account_id (siffrorna i deras OpenDota/Dotabuff-länk).
Ett långt Steam64-ID (börjar på 7656...) räknas om automatiskt.
Spelaren måste ha "Expose Public Match Data" påslaget i Dota.
Bara standardbibliotek, inga installationer.
"""
import calendar
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.request

# TURBO_API finns bara för att kunna testa mot en låtsas-server.
API = os.environ.get("TURBO_API", "https://api.opendota.com/api")
TURBO = 23          # game_mode för Turbo (kontrollerat mot /constants/game_mode)
SINGLE_DRAFT = 4     # game_mode för Single Draft
START = 1500        # startpoäng
K = 24              # hur snabbt poängen rör sig per match
# Hur mycket den förväntade vinstchansen får påverka. 1 = vanlig Elo, 0 = inte alls.
# Med 0.1 räknas 31 % som 48,1 % (en tiondel av avståndet från 50 %).
FÖRVÄNTAN_VIKT = 0.1
ASSIST_VIKT = 1 / 3  # en assist är värd en tredjedels kill
KDA_NORMAL = 2.0    # (kills + assists/3) / deaths som räknas som en "vanlig" match
KDA_MAX = 0.50      # K/D/A kan som mest ändra en matchs poäng med 50 %
MOTSTÅND_MAX = 400   # motståndarnivån flyttas högst så här långt från START
GULD_BONUS = 10      # extra MMR för vinst med guldhjälten (bara Turbo)
GULD_STREAK = 0.1    # varje guldvinst i rad höjer bonusen med 10 % av GULD_BONUS
GULD_MAX = 20        # bonusen blir aldrig högre än så (nås vid 11 guldvinster i rad)
MEDALJER = ["Herald", "Guardian", "Crusader", "Archon", "Legend", "Ancient", "Divine",
            "Immortal"]
STEAM64_BAS = 76561197960265728
FÖRSÖK = 3          # antal försök vid 429 / tillfälliga fel
MAX_VÄNTAN = 20     # väntar aldrig längre än så (sekunder) på ett nytt försök


class ApiFel(Exception):
    """Fel med ett meddelande som går att visa direkt för användaren."""


def tolka_id(text):
    """Gör om text till ett Steam32-ID, eller kastar ApiFel med förklaring."""
    text = str(text).strip()
    if not text.isdigit():
        raise ApiFel("The ID must contain digits only.")
    n = int(text)
    if n >= STEAM64_BAS:        # långt Steam64-ID -> räkna om
        n -= STEAM64_BAS
    if not 0 < n < 2 ** 32:
        raise ApiFel("That does not look like a valid Steam ID.")
    return n


def hämta_json(sökväg):
    """GET mot OpenDota. Försöker igen vid 429 och tillfälliga fel."""
    req = urllib.request.Request(API + sökväg,
                                 headers={"User-Agent": "turbo-rank-prototyp"})
    for försök in range(1, FÖRSÖK + 1):
        sista = försök == FÖRSÖK
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    vänta = int(e.headers.get("Retry-After") or 0)
                except ValueError:
                    vänta = 0
                vänta = vänta or 5 * försök
                if sista or vänta > MAX_VÄNTAN:
                    raise ApiFel("OpenDota is rate limiting us (HTTP 429). Wait a minute "
                                 "and try again. The free limit is roughly 60 calls "
                                 "per minute and 2000 per day.")
                time.sleep(vänta)
            elif e.code >= 500:
                if sista:
                    raise ApiFel(f"OpenDota is having problems right now (HTTP {e.code}). "
                                 "Try again in a moment.")
                time.sleep(2 * försök)
            elif e.code == 404:
                raise ApiFel("OpenDota could not find what was requested (HTTP 404).")
            else:
                raise ApiFel(f"OpenDota answered with an error (HTTP {e.code}).")
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            if sista:
                orsak = getattr(e, "reason", e)
                raise ApiFel("Network error: could not reach OpenDota. Check the "
                             f"internet connection. ({orsak})")
            time.sleep(2 * försök)
        except ValueError:
            raise ApiFel("OpenDota sent a response that could not be read.")


def hämta_profil(account_id):
    """(namn, rank_tier). Namnet är None om OpenDota inte känner till kontot,
    rank_tier är None om spelaren saknar (synlig) rank."""
    data = hämta_json(f"/players/{account_id}")
    if not isinstance(data, dict):
        return None, None
    return (data.get("profile") or {}).get("personaname"), data.get("rank_tier")


def hämta_profilnamn(account_id):
    """Spelarens namn, eller None om OpenDota inte känner till kontot."""
    return hämta_profil(account_id)[0]


def rank_till_mmr(tier):
    """Dotas rank (t.ex. 53 = Legend 3) till ungefärlig riktig MMR. None om okänd."""
    try:
        medalj, stjärnor = divmod(int(tier), 10)
    except (TypeError, ValueError):
        return None
    if not 1 <= medalj <= 8:
        return None
    if medalj == 8:                      # Immortal har inga stjärnor
        return 5620
    return (medalj - 1) * 770 + (min(max(stjärnor, 1), 5) - 1) * 154


def rank_namn(tier):
    """'Legend 3' för 53. '?' om okänd."""
    if rank_till_mmr(tier) is None:
        return "?"
    medalj, stjärnor = divmod(int(tier), 10)
    return MEDALJER[medalj - 1] + (f" {min(max(stjärnor, 1), 5)}" if medalj < 8 else "")


def motståndarnivå(lobby_rank, egen_rank):
    """Motståndets nivå på stegens skala, eller None om rankerna saknas.

    Lobbyns snittrank jämförs med spelarens egen rank: en lobby som ligger
    300 MMR över spelaren räknas som en motståndare på START + 300.
    """
    lobby, egen = rank_till_mmr(lobby_rank), rank_till_mmr(egen_rank)
    if lobby is None or egen is None:
        return None
    return START + max(-MOTSTÅND_MAX, min(MOTSTÅND_MAX, lobby - egen))


def hämta_matcher(account_id, game_mode):
    """Alla matcher i ett spelläge för en spelare. Tom lista om det inte finns några."""
    # significant=0 behövs för Turbo: annars räknar OpenDota bara "riktiga" lägen
    # (All Pick, Single Draft m.fl.) och Turbo faller bort.
    matcher = hämta_json(f"/players/{account_id}/matches"
                         f"?game_mode={game_mode}&significant=0")
    if not isinstance(matcher, list):
        raise ApiFel("OpenDota sent an unexpected response.")
    return matcher


def inga_matcher_fel(account_id, vad="Turbo"):
    """Rätt felmeddelande när en spelare saknar matcher."""
    # Tom lista ser likadan ut för fel ID och privat profil,
    # så vi tittar på profilen för att kunna ge rätt besked.
    if hämta_profilnamn(account_id) is None:
        return ApiFel(f"OpenDota does not know any account with ID {account_id}. "
                      "Check the digits (Steam32 ID from the OpenDota/Dotabuff link).")
    return ApiFel(f"No {vad} matches found. Either the profile is private "
                  '(turn on "Expose Public Match Data" in Dota under Settings > '
                  f"Options > Advanced and play a match) or the player has not "
                  f"played {vad}.")


def hämta_turbo_matcher(account_id):
    """Alla Turbo-matcher för en spelare. Kastar ApiFel om det inte finns några."""
    matcher = hämta_matcher(account_id, TURBO)
    if not matcher:
        raise inga_matcher_fel(account_id)
    return matcher


def giltig(match):
    """Har matchen det vi behöver för att räkna poäng?"""
    return (match.get("radiant_win") is not None
            and match.get("player_slot") is not None
            and match.get("start_time") is not None
            and match.get("match_id") is not None)


def vann(match):
    # player_slot < 128 = Radiant, annars Dire
    radiant = match["player_slot"] < 128
    return radiant == bool(match["radiant_win"])


def kda_faktor(kills, deaths, assists):
    """Tal mellan -KDA_MAX och +KDA_MAX: plus för bra K/D/A, minus för dålig.

    Dubbelt så bra som KDA_NORMAL ger fullt plus, hälften så bra fullt minus.
    Saknas siffrorna blir det 0 (matchen räknas som vanlig Elo).
    """
    if kills is None or deaths is None or assists is None:
        return 0.0
    kda = (kills + assists * ASSIST_VIKT) / max(deaths, 1)
    if kda <= 0:
        return -KDA_MAX
    return max(-KDA_MAX, min(KDA_MAX, KDA_MAX * math.log2(kda / KDA_NORMAL)))


def förklara(rating, vinst, kills=None, deaths=None, assists=None, motståndare=None):
    """Alla delsteg bakom en matchs MMR-ändring (används av förklaringssidan).
    motståndare = motståndets nivå på stegens skala; None räknas som START."""
    mot = START if motståndare is None else motståndare
    rå = 1 / (1 + 10 ** ((mot - rating) / 400))         # vanlig Elo-chans
    förväntat = 0.5 + (rå - 0.5) * FÖRVÄNTAN_VIKT       # dämpad, så att den styr mindre
    f = kda_faktor(kills, deaths, assists)
    grund = K * (1 - förväntat) if vinst else -K * förväntat
    ändring = grund * (1 + f) if vinst else grund * (1 - f)
    har_kda = None not in (kills, deaths, assists)
    kda = (kills + assists * ASSIST_VIKT) / max(deaths, 1) if har_kda else None
    return {"före": rating, "efter": rating + ändring, "förväntat": förväntat, "rå": rå,
            "grund": grund, "kda": kda, "faktor": f, "ändring": ändring,
            "motståndare": motståndare}


def guldbonus(streak):
    """Extra MMR för guldvinst nummer `streak` i rad: 10, 11, 12 ... högst GULD_MAX."""
    return min(GULD_MAX, GULD_BONUS * (1 + GULD_STREAK * (streak - 1)))


def poängändring(rating, vinst, kills=None, deaths=None, assists=None, motståndare=None):
    """Hur många MMR en match ger eller kostar.

    Vinst: bra K/D/A ger mer, många dödsfall ger mindre.
    Förlust: bra K/D/A kostar mindre, många dödsfall kostar mer.
    """
    return förklara(rating, vinst, kills, deaths, assists, motståndare)["ändring"]


def månad_för(tid):
    """Säsongen (månaden) en tidpunkt hör till, t.ex. '2026-10'. Räknas i UTC."""
    return time.strftime("%Y-%m", time.gmtime(tid))


def månadsgränser(månad):
    """(start, slut) i sekunder för en månad 'ÅÅÅÅ-MM'. Slutet räknas inte med."""
    år, m = int(månad[:4]), int(månad[5:7])
    nästa = (år + 1, 1) if m == 12 else (år, m + 1)
    return (calendar.timegm((år, m, 1, 0, 0, 0)),
            calendar.timegm((nästa[0], nästa[1], 1, 0, 0, 0)))


def räkna(matcher, månad=None, egen_rank=None):
    """MMR för en månad (alla börjar om på START den 1:a), äldsta match först.
    månad=None räknar alla matcher i en följd. Guldhjältar räknas bara på sajten."""
    rating, w, l = START, 0, 0
    for m in sorted(filter(giltig, matcher), key=lambda m: m["start_time"]):
        if månad and månad_för(m["start_time"]) != månad:
            continue
        resultat = 1 if vann(m) else 0
        rating += poängändring(rating, resultat, m.get("kills"), m.get("deaths"),
                               m.get("assists"),
                               motståndarnivå(m.get("average_rank"), egen_rank))
        w += resultat
        l += 1 - resultat
    n = w + l
    return {"rating": round(rating), "matcher": n, "vinster": w,
            "förluster": l, "vinstprocent": round(100 * w / n, 1) if n else 0}


def main(ids):
    månad = månad_för(time.time())
    rader = []
    for nr, text in enumerate(ids):
        if nr:
            time.sleep(1.1)  # håll oss under gratis-gränsen för anrop
        try:
            i = tolka_id(text)
            matcher = hämta_turbo_matcher(i)
            rader.append((i, räkna(matcher, månad, hämta_profil(i)[1])))
        except ApiFel as e:
            print(f"{text}: {e}")

    if not rader:
        return 1
    rader.sort(key=lambda x: x[1]["rating"], reverse=True)
    print(f"\nSeason {månad} (resets on the 1st, UTC). Gold hero bonuses are not included.")
    print(f"{'#':<3}{'Player':<14}{'MMR':<8}{'Matches':<9}{'W-L':<12}{'Win%'}")
    for plats, (i, s) in enumerate(rader, 1):
        vf = f"{s['vinster']}-{s['förluster']}"
        print(f"{plats:<3}{i:<14}{s['rating']:<8}{s['matcher']:<9}{vf:<12}"
              f"{s['vinstprocent']}")
    return 0


if __name__ == "__main__":
    # Windows-terminaler kan ha en teckentabell som inte klarar alla tecken.
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
