# Ranked Ladders

Månadsvisa rankingstegar för Dota 2 **Turbo** och **Single Draft**. Matcherna hämtas från
[OpenDota](https://www.opendota.com). Bara Pythons standardbibliotek, data sparas i SQLite.

## Starta lokalt

- **Windows:** dubbelklicka på `starta.bat`
- **Mac:** `python3 turbo_site.py`

Öppna sedan http://localhost:8000. Kräver Python 3.9 eller nyare.

## Så fungerar det

- **Två stegar** med egen topplista, profil och MMR. En spelare får vara med i en stege per månad.
- **Ny säsong varje månad:** alla börjar om på 1500 MMR den 1:a (UTC).
- **MMR** utgår från vinst eller förlust (omkring 12 per match) och justeras lite för
  förväntad vinstchans (motståndets nivå och egen MMR, dämpad till en tiondel) och för K/D/A: `(kills + assists / 3) / deaths`, högst ±30 %.
- **Guldhjälte (Turbo):** varje spelare har en slumpad hjälteordning per månad. Vinst med
  guldhjälten ger +10 MMR, och varje guldvinst i rad höjer bonusen med 1.
- **Inloggning med Steam:** spelaren loggar in på Steams egen sida. Sajten ser aldrig
  lösenordet. Bara inloggad spelare kan anmäla sig själv till en stege.
- **Automatisk uppdatering:** nya matcher hämtas i bakgrunden när någon öppnar en sida.

## Filer

| Fil | Innehåll |
|---|---|
| `turbo_site.py` | Hemsidan: sidor, databas, inloggning, bakgrundsuppdatering |
| `turbo_rank.py` | Anrop mot OpenDota och MMR-räkningen. Går att köra själv: `python3 turbo_rank.py <account_id>` |
| `starta.bat` | Startar sajten på Windows |

## Inställningar (miljövariabler)

| Variabel | Standard | Används till |
|---|---|---|
| `TURBO_URL` | `http://localhost:8000` | Sajtens adress utåt. Sätts till t.ex. `https://min-sajt.se` på nätet |
| `TURBO_HOST` | `127.0.0.1` | Sätts till `0.0.0.0` på en server |
| `TURBO_PORT` | `8000` | Porten sajten lyssnar på |

När `TURBO_URL` inte är localhost stängs testformuläret "track a player by ID" av.

## Status

Prototyp. Testad mot en låtsas-server, ännu inte med riktiga spelare.
