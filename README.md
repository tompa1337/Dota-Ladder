# Ranked Ladders

Månadsvisa rankingstegar för Dota 2 **Turbo** och **Single Draft**. Matcherna hämtas från
[OpenDota](https://www.opendota.com). Bara Pythons standardbibliotek, data sparas i SQLite.

## Starta lokalt

- **Windows:** dubbelklicka på `starta.bat`
- **Mac:** `python3 turbo_site.py`

Öppna sedan http://localhost:8000. Kräver Python 3.9 eller nyare.

## Så fungerar det

- **Två stegar** med egen topplista, profil och MMR. En spelare kan vara med i båda.
- **Ny säsong varje månad:** alla börjar om på 1500 MMR den 1:a (UTC).
- **MMR** utgår från vinst eller förlust (omkring 12 per match) och justeras lite för
  förväntad vinstchans (motståndets nivå och egen MMR, dämpad till en tiondel) och för K/D/A: `(kills + assists / 3) / deaths`, högst ±50 %.
- **Guldhjälte (Turbo):** varje spelare har en slumpad hjälteordning per månad. Vinst med
  guldhjälten ger +10 MMR, och varje guldvinst i rad höjer bonusen med 1, upp till högst +20.
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
| `TURBO_URL` | `http://localhost:8000`, eller Railways domän | Sajtens adress utåt. Behöver bara sättas för en egen domän |
| `TURBO_HOST` | `127.0.0.1` lokalt, `0.0.0.0` på nätet | Vilket nätverkskort sajten lyssnar på |
| `TURBO_PORT` | `8000` | Porten sajten lyssnar på. Saknas den används `PORT`, som värdtjänster brukar sätta |
| `TURBO_DB` | `turbo.db` bredvid koden, eller på Railways volym | Sökväg till databasfilen, t.ex. på en disk som sparas mellan omstarter |

När `TURBO_URL` inte är localhost stängs testformuläret "track a player by ID" av.

## Lägga sajten på nätet med Railway

1. Skapa ett konto på [railway.com](https://railway.com) och logga in med GitHub.
2. **New Project → Deploy from GitHub repo** och välj `Dota-Ladder`. Railway hittar
   `Dockerfile` och bygger sajten.
3. **Lägg till en volym** på tjänsten med mount path `/data`. Utan den försvinner databasen
   vid varje omstart.
4. Under tjänstens **Settings → Networking**, välj **Generate Domain**. Frågar Railway efter
   port, ange `8080`.
5. Öppna adressen du fick. Sajten läser själv av adressen och volymen, så inga variabler
   behöver sättas.

Varje ändring som laddas upp till `main` på GitHub publiceras automatiskt.

Egen domän: lägg till den under Networking och sätt variabeln `TURBO_URL` till
`https://din-domän.se`, så att Steam-inloggningen skickar tillbaka spelarna dit.

## Status

Prototyp. Testad mot en låtsas-server, ännu inte med riktiga spelare.
