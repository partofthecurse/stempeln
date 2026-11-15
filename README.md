<p align="center">
   <img src="stempel/static/icons/app-icon.svg" alt="Stempel logo" width="120">
</p>

# Stempel

Schlanke, selbst gehostete Zeiterfassung für den Homeserver. Ein Container, eine SQLite-Datei, keine Cloud. Benutzerkonten mit optionaler Zwei-Faktor-Authentifizierung, Hell-/Dunkelmodus und mehrere Wege, die Arbeitszeit **automatisch** zu erfassen.

## Funktionen

- **Stempeluhr** mit Live-Timer, Tagessoll und voraussichtlichem Feierabend
- **Automatische Erfassung**
  - *Gerät im Heimnetz*: Stempel pingt jede Minute deinen Arbeitslaptop (oder prüft einen TCP-Port). Ist er online, läuft die Zeit. Ist er länger als die Leerlaufzeit weg, endet der Eintrag zum letzten Kontakt.
  - *Lebenszeichen-API*: Ein kleiner Agent auf dem Arbeits-PC meldet sich nur, solange du Maus oder Tastatur benutzt. Damit zählen Mittagspausen nicht als Arbeitszeit. Skripte für Windows (PowerShell) und Linux/macOS liegen in der App unter **Automatik**.
  - *Ein/Aus/Toggle-API* für Home Assistant, NFC-Tags und iOS-/Android-Kurzbefehle
- **Gesetzliche Pausen** (§4 ArbZG): 30 Min. ab 6 h, 45 Min. ab 9 h, automatisch abgezogen. Tatsächliche Pausen ab 15 Min. werden angerechnet.
- **Soll/Ist und Überstundensaldo** mit Startdatum und Übertrag, frei wählbaren Arbeitstagen und Wochenstunden
- **Feiertage** je Bundesland (Paket `holidays`)
- **Urlaub, Krankheit und freie Tage** inklusive halber Tage und Resturlaub
- **Monatsübersicht**: Einträge nachtragen, korrigieren und löschen; CSV-Export, der sich direkt in Excel öffnen lässt
- **Konten**: Admin und Benutzer, TOTP-2FA mit Wiederherstellungscodes und Login-Ratenbegrenzung. Jedes Konto sieht nur die eigenen Zeiten.

## Installation

1. `compose.yaml` und `.env.example` auf den Docker-Host kopieren, `.env.example` als `.env` speichern und `STEMPEL_BIND_ADDRESS` setzen. Der Standard `127.0.0.1` ist nur auf dem Host erreichbar. Für das Heimnetz die LAN-IP des Hosts eintragen.
2. Ersten Admin anlegen:

   ```sh
   docker compose run --rm --no-deps --entrypoint python stempel -m stempel.manage create-admin
   ```

3. Starten:

   ```sh
   docker compose up -d
   ```

4. `http://<STEMPEL_BIND_ADDRESS>:32769` öffnen, anmelden und unter **Konto** das Arbeitszeitmodell einstellen (Wochenstunden, Arbeitstage, Bundesland, Urlaubstage).

Lokal bauen statt das veröffentlichte Image zu nutzen: `docker compose -f compose.yaml -f compose.local.yaml up -d --build`.

Update: `docker compose pull && docker compose up -d`.

Notfallzugang für Admins: `python -m stempel.manage reset-password USER` bzw. `reset-2fa USER` (Aufruf wie in Schritt 2).

## Automatik einrichten

### Gerät im Heimnetz

Unter **Automatik** die IP oder den Hostnamen des Arbeitsgeräts eintragen. Am besten vergibst du im Router eine feste IP. Windows beantwortet Ping standardmäßig nicht. Dann entweder die Firewallregel „Datei- und Druckerfreigabe (Echoanforderung – ICMPv4 eingehend)“ aktivieren oder einen offenen Port angeben, z. B. `192.168.1.42:445`.

Stempelst du manuell aus, während das Gerät noch online ist, startet Stempel erst nach der nächsten Leerlaufpause automatisch neu.

### API

Unter **Automatik** ein Token erzeugen. Danach:

```sh
curl -X POST http://stempel.lan:32769/api/v1/clock \
  -H "Authorization: Bearer stp_…" -H "Content-Type: application/json" \
  -d '{"action": "toggle"}'      # in | out | toggle | heartbeat
```

Mit `?format=text` kommt statt JSON nur ein lesbarer Satz zurück, z. B. `👋 Ausgestempelt um 17:03 · heute 8:21 h von 8:00 h · Saldo +1:15`. Im JSON steht er im Feld `message`. `action` darf auch als Query-Parameter kommen.

### NFC-Tag

- **iPhone:** Kurzbefehle → Automation → NFC → Tag scannen → „Inhalte von URL abrufen“ (`POST …/api/v1/clock?format=text&action=toggle`, Header `Authorization: Bearer stp_…`) → „Mitteilung anzeigen“.
- **Android:** über die Home-Assistant-App (Tag scannen → Automation → `rest_command`) oder mit „HTTP Shortcuts“ + „NFC Tools“.

`GET /api/v1/status` liefert Status, heutige Nettozeit, Tagessoll und Saldo in Minuten, etwa für ein Home-Assistant-Dashboard. Fertige Beispiele für Home Assistant (`rest_command`) und die Agenten stehen in der App.

## Konfiguration

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `STEMPEL_BIND_ADDRESS` | `127.0.0.1` | Host-Adresse für den Port |
| `STEMPEL_PORT` | `32769` | Host-Port |
| `TZ` | `Europe/Berlin` | Zeitzone für Tagesgrenzen |
| `MONITOR_INTERVAL_SECONDS` | `60` | Prüfintervall für Geräte und Leerlauf |
| `COOKIE_SECURE` / `TRUST_PROXY` | `false` | nur hinter einem HTTPS-Reverse-Proxy auf `true` setzen |

## Entwicklung

```sh
docker build -t stempel:test .
docker run --rm --network none -v "$PWD/tests:/app/tests:ro" --tmpfs /tmp -e STEMPEL_DATA_DIR=/tmp/data \
  --entrypoint python stempel:test -m unittest discover -s /app/tests -v
```

Ein Tag `v*` baut das Multi-Arch-Image, veröffentlicht es auf `ghcr.io/partofthecurse/stempel` und legt ein Release an.

## Hinweise

- Schriften: DM Sans und Outfit, SIL Open Font License 1.1, siehe [NOTICE](NOTICE)
- Mit KI-Unterstützung (Claude) entwickelt
