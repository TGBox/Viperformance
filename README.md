# Viperformance – Bottleneck-Tracker

Zeichnet auf, was den Laptop ausbremst, und erzeugt daraus einen HTML-Bericht.
Reines Python: kein HWiNFO64, kein Internet nötig. Admin-Rechte nur für die CPU-Temperatur.

## Einrichtung (einmalig)

    uv sync
    winget install PawnIO      (Treiber für die CPU-Temperatur)

Die Temperaturen liest [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
über das Paket `HardwareMonitor`. Dafür muss das Skript als Administrator laufen; `start.bat`
fragt per UAC danach. Ohne Admin-Rechte oder ohne PawnIO läuft die Messung trotzdem, nur ohne
Temperaturen.

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

| Größe                         | Quelle                                              |
|-------------------------------|---------------------------------------------------- |
| CPU-Last gesamt / je Kern     | psutil                                              |
| CPU-Takt relativ zum Nenntakt | Leistungsindikator `% Processor Performance`        |
| RAM, zugesicherter Speicher   | psutil, `% Committed Bytes In Use`                  |
| Auslagerung                   | `Pages Output/sec`, `Pages Input/sec`               |
| Datenträger-Aktivität/-Latenz | `% Idle Time`, `Avg. Disk sec/Transfer` je Laufwerk |
| Thermische Drosselung         | `Thermal Zone Information` (falls vorhanden)        |
| Akku-/Netzbetrieb             | psutil                                              |
| Grafikeinheit: Auslastung     | `GPU Engine\Utilization Percentage` (wie Task-Manager) |
| Grafikspeicher                | `GPU Adapter Memory` (eigener + geteilter)          |
| CPU-/GPU-Temperatur           | LibreHardwareMonitor (Admin + PawnIO)               |
| Top-Prozesse (CPU, I/O, RAM) | psutil, mind. alle 10 s (eigener Thread)           |

Nicht erfasst: Netzwerk, GPU-Last je Programm. Eine Onboard-Grafik hat meist keinen eigenen
Temperatursensor; maßgeblich ist dann die CPU-Temperatur.

## Diagnose-Regeln

- **Drosselung:** CPU-Last ≥ 50 % und Takt < 75 % des Nenntakts in ≥ 10 % der Lastzeit
- **Angezogene Bremse:** Last ≥ 30 % oder ein Kern ≥ 90 %, und Takt < 75 % in ≥ 25 % dieser Zeit
- **Thermisches Limit:** ACPI-Passivlimit < 100 % in ≥ 1 % der Zeit
- **Prozessor sehr heiß:** CPU-Temperatur ≥ 95 °C in ≥ 5 % der Zeit
- **Grafikeinheit ausgelastet:** ≥ 90 % in ≥ 5 % der Zeit
- **CPU ausgelastet:** Gesamtlast ≥ 90 % in ≥ 5 % der Zeit
- **Ein Kern am Anschlag:** ein Kern ≥ 95 % bei Gesamtlast < 50 % in ≥ 5 % der Zeit
- **RAM knapp:** P90 > 88 %; als Auslagern gilt nur, wenn in ≥ 5 % der Zeit ≥ 100 Seiten/s in die Auslagerungsdatei geschrieben werden
- **Datenträger (je Laufwerk):** ≥ 90 % aktiv in ≥ 5 % der Zeit, Antwortzeit P95 > 25 ms, oder Zugriffe ≥ 100 ms in ≥ 1 % der Zeit

Die Schwellen stehen in `diagnose()` und lassen sich dort anpassen.
