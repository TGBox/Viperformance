# Viperformance – Bottleneck-Tracker

Zeichnet auf, was den Laptop ausbremst, und erzeugt daraus einen HTML-Bericht.
Reines Python: kein HWiNFO64, keine Admin-Rechte, kein Internet nötig.

## Einrichtung (einmalig)

    uv sync

## Benutzung

    uv run bottleneck_tracker.py        (oder Doppelklick auf start.bat)

Während der Aufzeichnung:

| Eingabe         | Wirkung                                                        |
|-----------------|----------------------------------------------------------------|
| `ENTER`         | Marker setzen, wenn es gerade gehakt hat (Text davor optional) |
| `q` + `ENTER`   | beenden, auswerten, Bericht im Browser öffnen                  |
| `Strg+C`        | wie `q`                                                        |

CSV und Bericht landen in `logs/`.

Optionen: `--interval 0.5` (Messintervall in s), `--out ORDNER`,
`--analyze logs\datei.csv` (vorhandene Messung neu auswerten), `--no-open`.

## Was gemessen wird

| Größe                         | Quelle                                             |
|-------------------------------|----------------------------------------------------|
| CPU-Last gesamt / je Kern     | psutil                                             |
| CPU-Takt relativ zum Nenntakt | Leistungsindikator `% Processor Performance`       |
| RAM, zugesicherter Speicher   | psutil, `% Committed Bytes In Use`                 |
| Auslagerung                   | `Pages Input/sec`                                  |
| Datenträger-Aktivität/-Latenz | `% Idle Time`, `Avg. Disk sec/Transfer`            |
| Thermische Drosselung         | `Thermal Zone Information` (falls vorhanden)       |
| Akku-/Netzbetrieb             | psutil                                             |
| Top-Prozesse (CPU, I/O)       | psutil, alle 5 s                                   |

Nicht erfasst: Grafikkarte, Netzwerk, echte CPU-Kerntemperatur.

## Diagnose-Regeln

- **Drosselung:** CPU-Last ≥ 50 % und Takt < 75 % des Nenntakts in ≥ 10 % der Lastzeit
- **Thermisches Limit:** ACPI-Passivlimit < 100 % in ≥ 1 % der Zeit
- **CPU ausgelastet:** Gesamtlast ≥ 90 % in ≥ 5 % der Zeit
- **Ein Kern am Anschlag:** ein Kern ≥ 95 % bei Gesamtlast < 50 % in ≥ 5 % der Zeit
- **RAM knapp:** P90 > 88 %; mit > 500 nachgeladenen Seiten/s (P95) gilt Auslagern als belegt
- **Datenträger:** ≥ 90 % aktiv in ≥ 5 % der Zeit, oder Antwortzeit P95 > 25 ms

Die Schwellen stehen in `diagnose()` und lassen sich dort anpassen.
