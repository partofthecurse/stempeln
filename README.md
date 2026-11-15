<p align="center">
   <img src="stempeln/static/icons/app-icon.svg" alt="Stempeln logo" width="120">
</p>

# Stempeln

Schlanke, selbst gehostete Zeiterfassung für den Homeserver. Ein Container, eine SQLite-Datei, keine Cloud. Benutzerkonten mit optionaler Zwei-Faktor-Authentifizierung, Hell-/Dunkelmodus und mehrere Wege, die Arbeitszeit **automatisch** zu erfassen.

> [!WARNING]
> **Nutzung auf eigenes Risiko.** Stempeln ist ein privates Hobbyprojekt ohne jede Gewährleistung. Für Datenverlust, fehlerhafte Zeiten oder Rechnungen und daraus entstehende Schäden wird keine Haftung übernommen. **Rechnungen musst du selbst prüfen** bzw. von deiner Steuerberatung prüfen lassen. Details unter [Haftungsausschluss](#haftungsausschluss).

## Funktionen

- **Stempeluhr** mit Live-Timer, Tagessoll und voraussichtlichem Feierabend
- **Projekte & Kunden** mit Farbe und optionalem Stunden- **oder Tagessatz**: beim Einstempeln wählen, mit einem Klick wechseln, nachträglich zuordnen. Monatsauswertung mit Stunden und abrechenbarem Betrag, CSV pro Eintrag.
- **Rechnungen & E-Rechnung** (optional, unter Konto einschalten)
  - Entwurf direkt aus den noch nicht abgerechneten Zeiten eines Kunden: eine Position je Projekt, je Tag oder je Eintrag; Stunden optional gerundet, Tagessätze anteilig, auf halbe oder ganze Tage
  - Positionen frei bearbeiten oder ergänzen (Std., Tag, Stück, pauschal), Kopf-/Fußtext mit Platzhaltern, Leistungsnachweis als Anlage
  - **ZUGFeRD/Factur-X** (PDF/A-3 mit eingebetteter CII-XML nach EN 16931) und **XRechnung 3.0** als XML für Behörden mit Leitweg-ID
  - Regelbesteuerung mit mehreren Steuersätzen, **Kleinunternehmer (§ 19 UStG)**, **Reverse Charge** und Drittland je Kunde
  - Fortlaufender Nummernkreis (z. B. `RE-{JJJJ}-{NNN}`, optional jährlich neu), Nummer erst beim Abschließen
  - Abgeschlossene Rechnungen sind unveränderlich: Absender- und Kundendaten werden eingefroren, das PDF wird mit SHA-256 im Datenvolume archiviert (`/data/invoices`), abgerechnete Zeiten werden gesperrt
  - Status offen/bezahlt/überfällig, Storno per Stornorechnung (gibt die Zeiten wieder frei)
- **Für Angestellte oder freiberuflich**: Soll, Überstundensaldo, Resturlaub und Abwesenheiten lassen sich pro Konto abschalten. Dann zählt Stempeln nur Stunden je Projekt.
- **Automatische Erfassung**
  - *Gerät im Heimnetz*: Stempeln pingt jede Minute deinen Arbeitslaptop (oder prüft einen TCP-Port). Ist er online, läuft die Zeit. Ist er länger als die Leerlaufzeit weg, endet der Eintrag zum letzten Kontakt.
  - *Lebenszeichen-API*: Ein kleiner Agent auf dem Arbeits-PC meldet sich nur, solange du Maus oder Tastatur benutzt. Damit zählen Mittagspausen nicht als Arbeitszeit. Skripte für Windows (PowerShell) und Linux/macOS liegen in der App unter **Automatik**.
  - *Ein/Aus/Toggle-API* für Home Assistant, NFC-Tags und iOS-/Android-Kurzbefehle
- **Gesetzliche Pausen** (§4 ArbZG): 30 Min. ab 6 h, 45 Min. ab 9 h, automatisch abgezogen. Tatsächliche Pausen ab 15 Min. werden angerechnet.
- **Soll/Ist und Überstundensaldo** mit Startdatum und Übertrag, frei wählbaren Arbeitstagen und Wochenstunden
- **Feiertage** je Bundesland (Paket `holidays`)
- **Urlaub, Krankheit und freie Tage** inklusive halber Tage und Resturlaub
- **Monatsübersicht**: Einträge nachtragen, korrigieren und löschen; CSV-Export, der sich direkt in Excel öffnen lässt
- **Deutsch und Englisch**: Umschalter oben rechts (auch auf der Anmeldeseite), die Wahl wird pro Konto gespeichert; ohne Wahl gilt die Browsersprache. Zahlen-, Datums- und Betragsformate, Feiertagsnamen und die API-Textantworten folgen der Sprache. Rechnungen haben eine eigene Sprache je Kunde.
- **Konten**: Admin und Benutzer, TOTP-2FA mit Wiederherstellungscodes und Login-Ratenbegrenzung. Jedes Konto sieht nur die eigenen Zeiten.

## Installation

1. `compose.yaml` und `.env.example` auf den Docker-Host kopieren, `.env.example` als `.env` speichern und `STEMPELN_BIND_ADDRESS` setzen. Der Standard `127.0.0.1` ist nur auf dem Host erreichbar. Für das Heimnetz die LAN-IP des Hosts eintragen.
2. Ersten Admin anlegen:

   ```sh
   docker compose run --rm --no-deps --entrypoint python stempeln -m stempeln.manage create-admin
   ```

3. Starten:

   ```sh
   docker compose up -d
   ```

4. `http://<STEMPELN_BIND_ADDRESS>:32769` öffnen, anmelden und unter **Konto** das Arbeitszeitmodell einstellen (Wochenstunden, Arbeitstage, Bundesland, Urlaubstage).

Lokal bauen statt das veröffentlichte Image zu nutzen: `docker compose -f compose.yaml -f compose.local.yaml up -d --build`.

Update: `docker compose pull && docker compose up -d`.

Notfallzugang für Admins: `python -m stempeln.manage reset-password USER` bzw. `reset-2fa USER` (Aufruf wie in Schritt 2).

## Automatik einrichten

### Gerät im Heimnetz

Unter **Automatik** die IP oder den Hostnamen des Arbeitsgeräts eintragen. Am besten vergibst du im Router eine feste IP. Windows beantwortet Ping standardmäßig nicht. Dann entweder die Firewallregel „Datei- und Druckerfreigabe (Echoanforderung – ICMPv4 eingehend)“ aktivieren oder einen offenen Port angeben, z. B. `192.168.1.42:445`.

Stempelst du manuell aus, während das Gerät noch online ist, startet Stempeln erst nach der nächsten Leerlaufpause automatisch neu.

### API

Unter **Automatik** ein Token erzeugen. Danach:

```sh
curl -X POST http://stempeln.lan:32769/api/v1/clock \
  -H "Authorization: Bearer stp_…" -H "Content-Type: application/json" \
  -d '{"action": "toggle"}'      # in | out | toggle | heartbeat
```

Mit `?format=text` kommt statt JSON nur ein lesbarer Satz zurück, z. B. `👋 Ausgestempelt um 17:03 · heute 8:21 h von 8:00 h · Saldo +1:15`. Im JSON steht er im Feld `message`. `action` darf auch als Query-Parameter kommen.

### NFC-Tag

- **iPhone:** Kurzbefehle → Automation → NFC → Tag scannen → „Inhalte von URL abrufen“ (`POST …/api/v1/clock?format=text&action=toggle`, Header `Authorization: Bearer stp_…`) → „Mitteilung anzeigen“.
- **Android:** über die Home-Assistant-App (Tag scannen → Automation → `rest_command`) oder mit „HTTP Shortcuts“ + „NFC Tools“.

Optional `"project": "Name"` oder die Projekt-ID: `in` bzw. `toggle` mit Projekt startet dieses Projekt. Läuft gerade ein anderes, wird gewechselt (`"result": "switched"`). So kann jeder NFC-Tag ein eigenes Projekt haben. Für automatische Einträge (Gerät, Lebenszeichen) lässt sich unter **Automatik** ein Standardprojekt wählen.

`GET /api/v1/status` liefert Status, heutige Nettozeit, Tagessoll und Saldo in Minuten, etwa für ein Home-Assistant-Dashboard. Fertige Beispiele für Home Assistant (`rest_command`) und die Agenten stehen in der App.

## Konfiguration

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `STEMPELN_BIND_ADDRESS` | `127.0.0.1` | Host-Adresse für den Port |
| `STEMPELN_PORT` | `32769` | Host-Port |
| `TZ` | `Europe/Berlin` | Zeitzone für Tagesgrenzen |
| `MONITOR_INTERVAL_SECONDS` | `60` | Prüfintervall für Geräte und Leerlauf |
| `COOKIE_SECURE` / `TRUST_PROXY` | `false` | nur hinter einem HTTPS-Reverse-Proxy auf `true` setzen |
| `DEFAULT_LANGUAGE` | `de` | Sprache, wenn weder Konto noch Browser eine unterstützte Sprache liefern (`de`, `en`) |

## Entwicklung

```sh
docker build -t stempeln:test .
docker run --rm --network none -v "$PWD/tests:/app/tests:ro" -v "$PWD/tools:/app/tools:ro" --tmpfs /tmp \
  -e STEMPELN_DATA_DIR=/tmp/data --entrypoint python stempeln:test -m unittest discover -s /app/tests -v
```

**Übersetzungen:** Deutsch ist die Quellsprache. Texte werden im Code mit `_()` (Templates/Meldungen) bzw. `N_()` (Konstanten) markiert; die englischen Texte stehen in `stempeln/translations/en.json`. `python tools/i18n_extract.py` zeigt fehlende Übersetzungen, `--update en` ergänzt neue Schlüssel und entfernt unbenutzte. Ein Test schlägt fehl, solange etwas fehlt. Für eine weitere Sprache eine neue JSON-Datei anlegen und sie in `stempeln/i18n.py` (`LANGUAGES`, Monats- und Wochentagsnamen) eintragen.

Ein Tag `v*` baut das Multi-Arch-Image, veröffentlicht es auf `ghcr.io/partofthecurse/stempeln` und legt ein Release an.

## Hinweise

- Schriften: DM Sans und Outfit, SIL Open Font License 1.1, siehe [NOTICE](NOTICE). Für PDFs liegen DM-Sans-TTF-Dateien unter `stempeln/pdf_fonts`.
- Rechnungen: Die erzeugten Dateien wurden während der Entwicklung mit dem Mustang-Validator (veraPDF für PDF/A-3, Schematron-Regeln für EN 16931 und XRechnung 3.0) geprüft. Das ist keine Garantie für deine konkreten Rechnungen, siehe Haftungsausschluss. Das Datenvolume ist das Rechnungsarchiv und gehört in die Datensicherung (Aufbewahrungspflicht in der Regel 8 Jahre).
- Mit KI-Unterstützung (Claude) entwickelt

## Haftungsausschluss

Stempeln wird so bereitgestellt, wie es ist („as is“), ohne ausdrückliche oder stillschweigende Gewährleistung, insbesondere nicht für Richtigkeit, Vollständigkeit, Verfügbarkeit oder Eignung für einen bestimmten Zweck. Wer Stempeln installiert oder betreibt, tut das **auf eigenes Risiko und in eigener Verantwortung**.

- Der Autor haftet nicht für Schäden, die aus der Nutzung entstehen, etwa durch Datenverlust, fehlerhaft erfasste oder berechnete Arbeitszeiten, Sicherheitslücken oder falsche Rechnungen. Unberührt bleibt die Haftung für Vorsatz und grobe Fahrlässigkeit, soweit sie gesetzlich nicht ausgeschlossen werden kann.
- **Rechnungen, Steuerberechnung und E-Rechnungsdateien müssen vor dem Versand selbst geprüft werden**, am besten durch eine Steuerberatung. Stempeln ist keine Buchhaltungssoftware, kein zertifiziertes Kassen- oder Archivsystem und keine Steuer- oder Rechtsberatung. Für die Einhaltung von Pflichtangaben, Steuerrecht, GoBD und Aufbewahrungsfristen bist du selbst verantwortlich.
- Arbeitszeitauswertungen (z. B. Pausen nach ArbZG, Saldo) sind Hilfsmittel und ersetzen keine rechtliche Prüfung.
- Für Datensicherung, Updates und den sicheren Betrieb (Netzwerk, Zugriffe, Reverse Proxy) bist du selbst verantwortlich.

*English summary: Use at your own risk. This software comes without any warranty and the author accepts no liability. Always have invoices and tax figures checked yourself or by a tax advisor before sending them.*
