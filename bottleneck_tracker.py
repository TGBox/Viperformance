#!/usr/bin/env python3
"""
Bottleneck-Tracker: zeichnet auf, was den Laptop ausbremst, und erzeugt einen HTML-Bericht.

Kommt ohne HWiNFO64 und ohne Internet aus.
Abhängigkeiten (einmalig: uv sync):  psutil, dazu HardwareMonitor (LibreHardwareMonitor)
für CPU-/GPU-Temperaturen. Die Temperaturen brauchen Admin-Rechte und den PawnIO-Treiber
(winget install PawnIO); ohne beides läuft alles andere trotzdem.

Start:       uv run bottleneck_tracker.py  (oder Doppelklick auf start.bat)
Während der Aufzeichnung:
    [ENTER]            Marker setzen ("jetzt hat es gehakt") - optional vorher Text tippen
    q + [ENTER]        Aufzeichnung beenden und Bericht öffnen   (Strg+C geht auch)

Weitere Optionen:
    --interval 1.0     Messintervall in Sekunden
    --out ORDNER       Zielordner für CSV und Bericht (Standard: ./logs neben dem Skript)
    --analyze DATEI    vorhandene CSV erneut auswerten, ohne neu zu messen
    --no-open          Bericht nicht automatisch im Browser öffnen

Datenquellen: psutil (CPU-Last je Kern, RAM, Prozesse, Akku) und die Windows-
Leistungsindikatoren (PDH) für CPU-Takt, Datenträger-Latenz, Auslagerung,
ACPI-Thermalzonen sowie Auslastung und Speicher der Grafikeinheit. Die Indikatoren werden über ihre englischen Namen angesprochen
und funktionieren deshalb auch auf einem deutschen Windows.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import html
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

try:
    import psutil
except ImportError:
    sys.exit("psutil fehlt. Bitte einmalig einrichten:  uv sync  (oder: pip install psutil)")

IS_WINDOWS = sys.platform == "win32"
PROCESS_SWEEP_SECONDS = 10.0  # so oft werden die Top-Prozesse mindestens ermittelt
SWEEP_DUTY = 0.2              # die Prozess-Abfrage darf höchstens diesen Anteil der Zeit laufen
MARKER_BEFORE_S = 15          # Fenster vor einem Marker, das ausgewertet wird
MARKER_AFTER_S = 3

FIELDS = [
    "ts", "cpu", "core_max", "perf_pct", "mhz", "ram", "commit", "hard_faults",
    "disk_busy", "disk_lat_ms", "disk_queue", "temp_c", "passive_limit",
    "on_battery", "marker", "top", "pages_out", "pagefile_pct", "disk_name", "disk_lat_name",
    "self_cpu", "sweep_s", "gpu", "gpu_mem_mb", "cpu_temp", "gpu_temp",
]


# =============================================================================
# 1. Windows-Leistungsindikatoren (PDH) per ctypes
# =============================================================================
class _PdhValue(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [
            ("longValue", ctypes.c_long),
            ("doubleValue", ctypes.c_double),
            ("largeValue", ctypes.c_longlong),
            ("ptr", ctypes.c_void_p),
        ]

    _anonymous_ = ("u",)
    _fields_ = [("CStatus", ctypes.c_ulong), ("u", _U)]


class _PdhItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _PdhValue)]


class PerfCounters:
    """Kleine Hülle um PDH. Jeder Zähler, der nicht existiert, liefert einfach None."""

    FMT = 0x00000200 | 0x00008000   # PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
    MORE_DATA = 0x800007D2

    SINGLE = {
        "perf_pct": r"\Processor Information(_Total)\% Processor Performance",
        "disk_idle": r"\PhysicalDisk(_Total)\% Idle Time",
        "disk_lat": r"\PhysicalDisk(_Total)\Avg. Disk sec/Transfer",
        "disk_queue": r"\PhysicalDisk(_Total)\Avg. Disk Queue Length",
        "hard_faults": r"\Memory\Pages Input/sec",
        "commit": r"\Memory\% Committed Bytes In Use",
        "pages_out": r"\Memory\Pages Output/sec",
        "pagefile_pct": r"\Paging File(_Total)\% Usage",
    }
    MULTI = {
        "temp_k": r"\Thermal Zone Information(*)\Temperature",
        "passive_limit": r"\Thermal Zone Information(*)\% Passive Limit",
        "disk_idle_each": r"\PhysicalDisk(*)\% Idle Time",
        "disk_lat_each": r"\PhysicalDisk(*)\Avg. Disk sec/Transfer",
        "gpu_engine": r"\GPU Engine(*)\Utilization Percentage",
        "gpu_mem_dedicated": r"\GPU Adapter Memory(*)\Dedicated Usage",
        "gpu_mem_shared": r"\GPU Adapter Memory(*)\Shared Usage",
    }

    def __init__(self):
        self.query = None
        self.single = {}
        self.multi = {}
        self.missing = []
        if not IS_WINDOWS:
            return
        try:
            self.pdh = pdh = ctypes.WinDLL("pdh.dll")
            H, DW = ctypes.c_void_p, ctypes.c_ulong
            pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(H)]
            pdh.PdhAddEnglishCounterW.argtypes = [H, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(H)]
            pdh.PdhCollectQueryData.argtypes = [H]
            pdh.PdhGetFormattedCounterValue.argtypes = [H, DW, ctypes.POINTER(DW), ctypes.POINTER(_PdhValue)]
            pdh.PdhGetFormattedCounterArrayW.argtypes = [H, DW, ctypes.POINTER(DW), ctypes.POINTER(DW), ctypes.c_void_p]
            pdh.PdhCloseQuery.argtypes = [H]
            for fn in ("PdhOpenQueryW", "PdhAddEnglishCounterW", "PdhCollectQueryData",
                       "PdhGetFormattedCounterValue", "PdhGetFormattedCounterArrayW", "PdhCloseQuery"):
                getattr(pdh, fn).restype = DW

            query = H()
            if pdh.PdhOpenQueryW(None, None, ctypes.byref(query)) != 0:
                return
            self.query = query
            for target, paths in ((self.single, self.SINGLE), (self.multi, self.MULTI)):
                for key, path in paths.items():
                    handle = H()
                    if pdh.PdhAddEnglishCounterW(query, path, None, ctypes.byref(handle)) == 0:
                        target[key] = handle
                    else:
                        self.missing.append(key)
            pdh.PdhCollectQueryData(query)   # erste Probe: Raten-Zähler brauchen zwei Messungen
        except Exception as exc:             # PDH ist Zusatz - niemals die Messung daran scheitern lassen
            print(f"[Hinweis] Windows-Leistungsindikatoren nicht verfügbar: {exc}")
            self.query = None

    @property
    def available(self) -> bool:
        return self.query is not None

    def read(self) -> dict:
        """Liefert {name: float|None} für Einzelzähler und {name: [(instanz, float)]} für Mehrfachzähler."""
        out = {k: None for k in self.SINGLE}
        out.update({k: [] for k in self.MULTI})
        if not self.available:
            return out
        try:
            if self.pdh.PdhCollectQueryData(self.query) != 0:
                return out
            for key, handle in self.single.items():
                val = _PdhValue()
                rc = self.pdh.PdhGetFormattedCounterValue(handle, self.FMT, None, ctypes.byref(val))
                if rc == 0 and val.CStatus in (0, 1):
                    out[key] = val.doubleValue
            for key, handle in self.multi.items():
                out[key] = self._read_array(handle)
        except Exception:
            pass
        return out

    def _read_array(self, handle) -> list:
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        rc = self.pdh.PdhGetFormattedCounterArrayW(handle, self.FMT, ctypes.byref(size), ctypes.byref(count), None)
        if rc != self.MORE_DATA or size.value == 0:
            return []
        buf = ctypes.create_string_buffer(size.value)
        rc = self.pdh.PdhGetFormattedCounterArrayW(handle, self.FMT, ctypes.byref(size), ctypes.byref(count), buf)
        if rc != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_PdhItem))
        return [(items[i].szName or "", items[i].FmtValue.doubleValue) for i in range(count.value)
                if items[i].FmtValue.CStatus in (0, 1)]

    def close(self):
        if self.available:
            try:
                self.pdh.PdhCloseQuery(self.query)
            except Exception:
                pass
            self.query = None


def gpu_load(engines) -> float | None:
    """Wie im Task-Manager: je GPU-Engine die Anteile aller Prozesse addieren, die vollste Engine zählt.

    Instanzname: pid_1234_luid_0x..._phys_0_eng_0_engtype_3D
    """
    total = {}
    for name, value in engines:
        key = name.partition("_luid_")[2] or name
        total[key] = total.get(key, 0.0) + value
    return min(100.0, max(total.values())) if total else None


def gpu_memory_mb(dedicated, shared) -> float | None:
    """Belegter Grafikspeicher des Adapters mit dem höchsten Verbrauch (eigener + mit dem RAM geteilter)."""
    total = {}
    for name, value in list(dedicated) + list(shared):
        total[name] = total.get(name, 0.0) + value
    return max(total.values()) / 1e6 if total else None


class Sensors:
    """CPU-/GPU-Temperaturen über LibreHardwareMonitor (PyPI-Paket HardwareMonitor).

    Zusatz: Fehlt das Paket, fehlen Admin-Rechte oder der PawnIO-Treiber, bleiben die Werte leer
    und `reason` nennt den Grund. Die Messung selbst läuft immer weiter.
    """

    def __init__(self):
        self.computer = None
        self.reason = None
        self.gpu_name = None
        if not IS_WINDOWS:
            self.reason = "nur unter Windows verfügbar"
            return
        try:
            from HardwareMonitor.Hardware import Computer
        except Exception as exc:
            self.reason = f"Paket HardwareMonitor nicht ladbar ({exc}) - einmalig  uv sync  ausführen"
            return
        try:
            computer = Computer()
            computer.IsCpuEnabled = True
            computer.IsGpuEnabled = True
            computer.Open()
            self.computer = computer
            for hw in computer.Hardware:
                if self._kind(hw.HardwareType).startswith("Gpu"):
                    self.gpu_name = str(hw.Name)
                    break
            if self.read()["cpu_temp"] is None:
                self.reason = ("CPU-Temperatur nicht lesbar - PawnIO-Treiber installieren (winget install PawnIO)"
                               if is_admin() else "CPU-Temperatur nicht lesbar - als Administrator starten")
        except Exception as exc:
            self.reason = f"LibreHardwareMonitor nicht startbar ({exc})"
            self.computer = None

    @staticmethod
    def _kind(enum_value) -> str:
        return str(enum_value).rsplit(".", 1)[-1]      # .NET-Enum -> "Cpu", "GpuIntel", "Temperature", ...

    def read(self) -> dict:
        out = {"cpu_temp": None, "gpu_temp": None}
        if self.computer is None:
            return out
        try:
            for hw in self.computer.Hardware:
                kind = self._kind(hw.HardwareType)
                key = "cpu_temp" if kind == "Cpu" else "gpu_temp" if kind.startswith("Gpu") else None
                if key is None:
                    continue
                hw.Update()
                temps = [float(s.Value) for s in hw.Sensors
                         if self._kind(s.SensorType) == "Temperature" and s.Value is not None
                         and "Distance" not in str(s.Name)]          # "Distance to TjMax" ist ein Abstand
                temps = [t for t in temps if 0 < t < 150]
                if temps:
                    out[key] = max(temps + ([out[key]] if out[key] is not None else []))
        except Exception:
            pass
        return out

    def close(self):
        if self.computer is not None:
            try:
                self.computer.Close()
            except Exception:
                pass
            self.computer = None


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def cpu_identity() -> tuple:
    """(Name, Nenntakt in MHz) - aus der Registry, lesbar ohne Admin-Rechte."""
    name, mhz = None, None
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
                mhz = float(winreg.QueryValueEx(key, "~MHz")[0])
        except OSError:
            pass
    if mhz is None:
        try:
            freq = psutil.cpu_freq()
            mhz = float(freq.max or freq.current) if freq else None
        except Exception:
            mhz = None
    return name, (mhz if mhz and mhz > 0 else None)


# =============================================================================
# 2. Aufzeichnung
# =============================================================================
class Recorder:
    def __init__(self, csv_path: Path, interval: float):
        self.csv_path = csv_path
        self.interval = interval
        self.stop_event = threading.Event()
        self.perf = PerfCounters()
        self.sensors = Sensors()
        self.cpu_name, self.nominal_mhz = cpu_identity()
        self._marker = None
        self._marker_lock = threading.Lock()
        self._io_prev = {}
        self.samples = 0
        self.thread = threading.Thread(target=self._run, name="sampler", daemon=True)
        self.sweeper = threading.Thread(target=self._run_sweeper, name="sweeper", daemon=True)
        self._sweep_lock = threading.Lock()
        self._sweep_result = None
        self._self_prev = None

    def start(self):
        self.thread.start()
        self.sweeper.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=self.interval + 10)
        self.sweeper.join(timeout=15)
        self.perf.close()
        self.sensors.close()

    def _run_sweeper(self):
        """Ermittelt die Top-Prozesse getrennt von der Messung, damit diese nie darauf warten muss.

        Auf manchen Rechnern dauert ein Durchlauf mehrere Sekunden (viele Prozesse, Virenscanner).
        Deshalb passt sich der Abstand an: Die Abfrage läuft höchstens SWEEP_DUTY der Zeit.
        """
        self._sweep_processes()              # Zähler anstoßen, erster Durchlauf liefert noch keine Werte
        pause = PROCESS_SWEEP_SECONDS
        while not self.stop_event.wait(pause):
            started = time.monotonic()
            try:
                result = self._sweep_processes()
            except Exception:
                continue
            took = time.monotonic() - started
            with self._sweep_lock:
                self._sweep_result = (result, took)
            pause = max(PROCESS_SWEEP_SECONDS - took, took * (1 - SWEEP_DUTY) / SWEEP_DUTY)

    def set_marker(self, label: str):
        with self._marker_lock:
            self._marker = label or "Marker"

    def _run(self):
        psutil.cpu_percent(percpu=True)      # Zähler anstoßen (psutil merkt sich den Stand je Thread)
        next_tick = time.monotonic() + self.interval
        with open(self.csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            fh.write(f"# cpu_name={self.cpu_name or ''};nominal_mhz={self.nominal_mhz or ''};"
                     f"interval={self.interval};cores={psutil.cpu_count() or ''};"
                     f"gpu_name={(self.sensors.gpu_name or '').replace(';', ',')}\n")
            writer.writeheader()
            while not self.stop_event.wait(max(0.0, next_tick - time.monotonic())):
                next_tick += self.interval
                if next_tick < time.monotonic():           # nach Standby nicht "nachholen"
                    next_tick = time.monotonic() + self.interval
                row = self._sample()
                with self._sweep_lock:
                    sweep, self._sweep_result = self._sweep_result, None
                if sweep:
                    row["top"] = json.dumps(sweep[0], ensure_ascii=False)
                    row["sweep_s"] = round(sweep[1], 2)
                with self._marker_lock:
                    if self._marker:
                        row["marker"], self._marker = self._marker, None
                writer.writerow(row)
                fh.flush()                                   # bei Absturz bleibt alles bis hierher erhalten
                self.samples += 1

    def _sample(self) -> dict:
        cores = psutil.cpu_percent(percpu=True)
        used, now = sum(os.times()[:2]), time.monotonic()      # CPU-Zeit dieses Skripts (alle Threads)
        self_cpu = None
        if self._self_prev and now > self._self_prev[1]:
            self_cpu = 100.0 * (used - self._self_prev[0]) / (now - self._self_prev[1]) / (psutil.cpu_count() or 1)
        self._self_prev = (used, now)
        pc = self.perf.read()
        perf_pct = pc["perf_pct"]
        temps = [k - 273.15 for _, k in pc["temp_k"] if k and k > 200]
        # Je Laufwerk messen: "_Total" mittelt über alle Laufwerke und versteckt ein einzelnes volles.
        busy_each = [(min(100.0, max(0.0, 100.0 - v)), n) for n, v in pc["disk_idle_each"] if n != "_Total"]
        lat_each = [(v * 1000, n) for n, v in pc["disk_lat_each"] if n != "_Total"]
        disk_busy, disk_name = max(busy_each) if busy_each else (
            min(100.0, max(0.0, 100.0 - pc["disk_idle"])) if pc["disk_idle"] is not None else None, "")
        disk_lat, disk_lat_name = max(lat_each) if lat_each else (
            pc["disk_lat"] * 1000 if pc["disk_lat"] is not None else None, "")
        sens = self.sensors.read()
        try:
            battery = psutil.sensors_battery()
        except Exception:
            battery = None
        row = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "cpu": _r(sum(cores) / len(cores)) if cores else "",
            "core_max": _r(max(cores)) if cores else "",
            "perf_pct": _r(perf_pct),
            "mhz": _r(self.nominal_mhz * perf_pct / 100, 0) if perf_pct and self.nominal_mhz else "",
            "ram": _r(psutil.virtual_memory().percent),
            "commit": _r(pc["commit"]),
            "hard_faults": _r(pc["hard_faults"], 0),
            "disk_busy": _r(disk_busy),
            "disk_lat_ms": _r(disk_lat, 2),
            "disk_queue": _r(pc["disk_queue"], 2),
            "temp_c": _r(max(temps)) if temps else "",
            "passive_limit": _r(min(v for _, v in pc["passive_limit"])) if pc["passive_limit"] else "",
            "on_battery": "" if battery is None or battery.power_plugged is None
                          else (0 if battery.power_plugged else 1),
            "marker": "",
            "top": "",
            "pages_out": _r(pc["pages_out"], 0),
            "pagefile_pct": _r(pc["pagefile_pct"]),
            "disk_name": disk_name,
            "disk_lat_name": disk_lat_name,
            "self_cpu": _r(self_cpu, 2),
            "sweep_s": "",
            "gpu": _r(gpu_load(pc["gpu_engine"])),
            "gpu_mem_mb": _r(gpu_memory_mb(pc["gpu_mem_dedicated"], pc["gpu_mem_shared"]), 0),
            "cpu_temp": _r(sens["cpu_temp"]),
            "gpu_temp": _r(sens["gpu_temp"]),
        }
        return row

    def _sweep_processes(self) -> list:
        """Top-Prozesse seit dem letzten Durchlauf: [[name, cpu_anteil_%, io_bytes, ram_mb], ...]"""
        ncpu = psutil.cpu_count() or 1
        agg, io_now = {}, {}
        for proc in psutil.process_iter(["name"]):
            if proc.pid == 0:                  # "System Idle Process" ist keine Last
                continue
            try:
                with proc.oneshot():
                    cpu = proc.cpu_percent(None) / ncpu
                    rss = proc.memory_info().rss
                    try:
                        io = proc.io_counters()
                        io_total = io.read_bytes + io.write_bytes
                    except (psutil.Error, AttributeError, NotImplementedError):
                        io_total = None
            except psutil.Error:
                continue
            delta = 0
            if io_total is not None:
                prev = self._io_prev.get(proc.pid)
                if prev is not None and io_total >= prev:
                    delta = io_total - prev
                io_now[proc.pid] = io_total
            name = proc.info.get("name") or f"pid {proc.pid}"
            entry = agg.setdefault(name, [0.0, 0, 0])
            entry[0] += cpu
            entry[1] += delta
            entry[2] += rss
        self._io_prev = io_now
        by_cpu = sorted(agg.items(), key=lambda kv: kv[1][0], reverse=True)[:5]
        by_io = sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)[:3]
        picked = {n: v for n, v in by_cpu if v[0] >= 0.5}
        picked.update({n: v for n, v in by_io if v[1] >= 1_000_000})
        by_ram = sorted(agg.items(), key=lambda kv: kv[1][2], reverse=True)[:5]
        picked.update(dict(by_ram))
        return [[n, round(v[0], 1), int(v[1]), round(v[2] / 1e6)] for n, v in picked.items()]


def _r(value, digits=1):
    return "" if value is None else round(value, digits)


def record(csv_path: Path, interval: float) -> bool:
    rec = Recorder(csv_path, interval)
    rec.start()
    print("=" * 64)
    print("AUFZEICHNUNG LÄUFT - arbeite ganz normal weiter.")
    print(f"  Log-Datei : {csv_path}")
    print(f"  Intervall : {interval:g} s" + ("" if rec.perf.available or not IS_WINDOWS
          else "   (ohne Windows-Leistungsindikatoren)"))
    if rec.perf.missing:
        print(f"  Nicht verfügbar auf diesem Gerät: {', '.join(rec.perf.missing)}")
    if rec.sensors.gpu_name:
        print(f"  Grafik    : {rec.sensors.gpu_name}")
    if rec.sensors.reason:
        print(f"  [Hinweis] Temperatursensoren: {rec.sensors.reason}")
    print()
    print("  [ENTER]       Marker setzen, wenn es gerade gehakt hat (Text davor ist optional)")
    print("  q + [ENTER]   beenden und Bericht erzeugen (oder Strg+C)")
    print("=" * 64)
    try:
        while True:
            try:
                line = input().strip()
            except EOFError:                   # keine Konsole (z. B. Start ohne Fenster): auf Strg+C warten
                while True:
                    time.sleep(3600)
            if line.lower() in ("q", "quit", "exit", "ende"):
                break
            rec.set_marker(line)
            print(f"  Marker gesetzt um {datetime.now():%H:%M:%S}" + (f": {line}" if line else ""))
    except KeyboardInterrupt:
        pass
    print("\nBeende Aufzeichnung ...")
    rec.stop()
    return rec.samples > 0


# =============================================================================
# 3. Auswertung
# =============================================================================
def _num(text):
    try:
        return float(text) if text not in ("", None) else None
    except ValueError:
        return None


def load_csv(csv_path: Path) -> tuple:
    meta = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        first = fh.readline()
        if first.startswith("#"):
            for part in first[1:].strip().split(";"):
                key, _, val = part.partition("=")
                meta[key.strip()] = val.strip()
        else:
            fh.seek(0)
        rows = []
        for raw in csv.DictReader(fh):
            try:
                ts = datetime.fromisoformat(raw["ts"])
            except (KeyError, ValueError, TypeError):
                continue                       # abgeschnittene letzte Zeile o. ä.
            row = {k: _num(raw.get(k)) for k in FIELDS if k not in ("ts", "marker", "top", "disk_name", "disk_lat_name")}
            row["disk_name"] = raw.get("disk_name") or ""
            row["disk_lat_name"] = raw.get("disk_lat_name") or ""
            row["ts"] = ts
            row["marker"] = raw.get("marker") or ""
            try:
                row["top"] = json.loads(raw["top"]) if raw.get("top") else None
            except ValueError:
                row["top"] = None
            rows.append(row)
    return meta, rows


def quantile(values, q):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    pos = (len(vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def share(rows, predicate, base=None):
    """Anteil (0..100) der Zeilen, für die predicate gilt; None-Werte zählen nicht mit."""
    hits = total = 0
    for row in rows:
        if base is not None and not _safe(base, row):
            continue
        result = _safe(predicate, row, default=None)
        if result is None:
            continue
        total += 1
        hits += bool(result)
    return (100.0 * hits / total if total else None), total


def _safe(fn, row, default=False):
    try:
        return fn(row)
    except TypeError:                          # Vergleich mit None -> Messwert fehlt
        return default


def fmt(value, unit="", digits=0):
    if value is None:
        return "n/v"
    text = f"{value:.{digits}f}".replace(".", ",")
    return f"{text}{unit}"


def col(rows, key):
    return [r[key] for r in rows]


def top_label(sweeps):
    """Auffälligster Prozess (CPU bzw. I/O) aus einer Liste von Prozess-Durchläufen."""
    best_cpu, best_io = None, None
    for sweep in sweeps:
        for name, cpu, io, *_ in sweep:
            if best_cpu is None or cpu > best_cpu[1]:
                best_cpu = (name, cpu)
            if best_io is None or io > best_io[1]:
                best_io = (name, io)
    return best_cpu, best_io


def episodes(rows, predicate, gap=5.0) -> list:
    """Fasst aufeinanderfolgende Treffer zu Zeitabschnitten [(von, bis)] zusammen."""
    out, start, prev = [], None, None
    for row in rows:
        if not _safe(predicate, row):
            continue
        ts = row["ts"]
        if start is None:
            start = prev = ts
        elif (ts - prev).total_seconds() > gap:
            out.append((start, prev))
            start = prev = ts
        else:
            prev = ts
    if start is not None:
        out.append((start, prev))
    return out


def procs_during(rows, predicate, by="cpu") -> list:
    """Aktivste Programme in den Phasen, in denen predicate zutraf: [(name, cpu_%, io_mb)].

    Ein Prozess-Durchlauf beschreibt die Zeit seit dem vorherigen Durchlauf. Er zählt mit,
    wenn in diesem Abschnitt mindestens ein Treffer lag.
    """
    agg, sweeps, hit = {}, 0, False
    for row in rows:
        hit = hit or _safe(predicate, row)
        if row["top"] is None:
            continue
        if hit:
            sweeps += 1
            for name, cpu, io, *_ in row["top"]:
                entry = agg.setdefault(name, [0.0, 0])
                entry[0] += cpu
                entry[1] += io
        hit = False
    table = [(name, v[0] / sweeps, v[1] / 1e6) for name, v in agg.items()]
    table.sort(key=lambda e: e[2] if by == "io" else e[1], reverse=True)
    return [e for e in table[:3] if (e[2] >= 1 if by == "io" else e[1] >= 0.5)]


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60} s" if seconds % 60 else f"{seconds // 60} min"


def diagnose(rows, meta) -> list:
    """Liefert eine Liste von Befunden (dicts) für den Bericht.

    level: 'bad' | 'warn' | 'ok'      title/summary: immer sichtbar
    meaning/actions/extra: erscheinen beim Aufklappen
    when: Bedingung je Messpunkt - daraus werden Zeiträume und beteiligte Programme ermittelt
    """
    out = []

    def add(level, title, summary, meaning, actions, when=None, by="cpu", extra=None):
        out.append({"level": level, "title": title, "summary": summary, "meaning": meaning,
                    "actions": actions, "when": when, "by": by, "extra": extra})

    cores = _num(meta.get("cores"))
    loaded = lambda r: r["cpu"] >= 50

    # --- CPU-Drosselung: Takt bricht ein, obwohl Last anliegt ---------------------
    throttled = lambda r: r["cpu"] >= 50 and r["perf_pct"] < 75
    thr, n_loaded = share(rows, lambda r: r["perf_pct"] < 75, base=loaded)
    if thr is not None and n_loaded >= 10 and thr >= 10:
        on_batt, _ = share(rows, lambda r: r["on_battery"] == 1, base=throttled)
        battery = (f" {fmt(on_batt, ' %')} dieser Phasen liefen im Akkubetrieb."
                   if on_batt is not None and on_batt >= 60 else "")
        add("bad", "Der Prozessor wird unter Last ausgebremst",
            f"In {fmt(thr, ' %')} der Zeit mit hoher Last lief er mit weniger als drei Vierteln "
            "seiner normalen Geschwindigkeit.",
            "Wenn viel zu tun ist, schaltet ein Prozessor normalerweise hoch (Turbo). Hier ist er trotz Arbeit "
            "langsamer geworden. Das passiert, wenn der Rechner zu heiß wird, zu wenig Strom bekommt oder auf "
            "Stromsparen eingestellt ist." + battery,
            ["Netzteil anschließen und den Test wiederholen.",
             "In Windows unter Einstellungen > System > Netzbetrieb den Energiemodus auf „Beste Leistung“ stellen.",
             "Lüftungsschlitze freihalten, den Laptop auf eine harte Unterlage stellen, Lüfter entstauben.",
             "Im Diagramm „CPU-Takt“ nachsehen: Fällt die Linie erst nach einiger Zeit ab, ist es Hitze; "
             "ist sie von Anfang an niedrig, ist es die Stromversorgung oder der Energiemodus."],
            when=throttled)
    else:
        # Mittlere Last: Der Rechner arbeitet erkennbar, der Prozessor schaltet aber nicht hoch.
        working = lambda r: r["cpu"] >= 30 or r["core_max"] >= 90
        slowed = lambda r: (r["cpu"] >= 30 or r["core_max"] >= 90) and r["perf_pct"] < 75
        slow, n_working = share(rows, lambda r: r["perf_pct"] < 75, base=working)
        if slow is not None and n_working >= 20 and slow >= 25:
            on_batt, _ = share(rows, lambda r: r["on_battery"] == 1, base=slowed)
            battery = (f" {fmt(on_batt, ' %')} dieser Phasen liefen im Akkubetrieb - das ist die "
                       "wahrscheinlichste Ursache." if on_batt is not None and on_batt >= 60 else "")
            add("warn", "Der Prozessor läuft mit angezogener Bremse",
                f"In {fmt(slow, ' %')} der Zeit, in der der Rechner zu tun hatte, lief er mit weniger als "
                "drei Vierteln seiner normalen Geschwindigkeit.",
                "Der Rechner hatte Arbeit, hat den Prozessor aber nicht hochgeschaltet. Im Akkubetrieb und in "
                "Stromspar-Einstellungen ist das Absicht: Es spart Strom, macht aber alles langsamer." + battery,
                ["Mit angeschlossenem Netzteil noch einmal messen und vergleichen.",
                 "In Windows unter Einstellungen > System > Netzbetrieb den Energiemodus auf "
                 "„Beste Leistung“ stellen.",
                 "Bleibt der Takt auch am Netzteil niedrig: Leistungsprofil im Hersteller-Tool des Laptops prüfen."],
                when=slowed)

    limited = lambda r: r["passive_limit"] < 100
    passive, _ = share(rows, limited)
    if passive is not None and passive >= 1:
        add("bad", "Windows bremst den Prozessor wegen Hitze",
            f"In {fmt(passive, ' %', 1)} der Zeit war die Leistung begrenzt "
            f"(stärkste Begrenzung: auf {fmt(quantile(col(rows, 'passive_limit'), 0), ' %')}).",
            "Der Rechner meldet selbst, dass er zu warm ist, und nimmt deshalb Leistung zurück, "
            "um sich zu schützen.",
            ["Lüftungsschlitze freihalten und Lüfter entstauben.",
             "Den Laptop nicht auf Decke, Sofa oder Schoß betreiben.",
             "Hält das Problem an: Wärmeleitpaste und Kühlung prüfen lassen."],
            when=limited)

    hot = lambda r: r["cpu_temp"] >= 95
    hot_share, _ = share(rows, hot)
    if hot_share is not None and hot_share >= 5:
        add("warn", "Der Prozessor wird sehr heiß",
            f"In {fmt(hot_share, ' %')} der Zeit lag er bei 95 °C oder mehr "
            f"(höchster Wert: {fmt(quantile(col(rows, 'cpu_temp'), 1.0), ' °C')}).",
            "Ab etwa 95 bis 100 °C nimmt ein Prozessor Leistung zurück, um sich zu schützen. Kurze Spitzen "
            "unter Volllast sind bei Laptops üblich; hält die Temperatur an, kostet das Geschwindigkeit.",
            ["Im Diagramm „CPU-Takt“ prüfen, ob der Takt in diesen Phasen einbricht.",
             "Lüftungsschlitze freihalten, den Laptop auf eine harte Unterlage stellen, Lüfter entstauben.",
             "Hält das Problem an: Wärmeleitpaste und Kühlung prüfen lassen."],
            when=hot)

    # --- Grafikeinheit ------------------------------------------------------------
    gpu_full = lambda r: r["gpu"] >= 90
    gpu_share, _ = share(rows, gpu_full)
    if gpu_share is not None and gpu_share >= 5:
        add("warn", "Die Grafikeinheit ist voll ausgelastet",
            f"In {fmt(gpu_share, ' %')} der Zeit war sie zu 90 % oder mehr beschäftigt.",
            "Die Grafikeinheit zeichnet alles, was auf dem Bildschirm erscheint, und dekodiert Videos. Ist sie "
            "voll, ruckeln Fenster, Videos und Animationen, auch wenn der Prozessor noch Luft hat. Eine "
            "Onboard-Grafik ist dafür anfälliger als eine eigene Grafikkarte.",
            ["Im Task-Manager unter Prozesse die Spalte „GPU“ einblenden: Sie zeigt, welches Programm sie belegt.",
             "Häufige Auslöser: Videokonferenzen mit Hintergrundeffekten, viele Browser-Tabs mit Videos, "
             "hochauflösende externe Bildschirme.",
             "Grafiktreiber aktualisieren."],
            when=gpu_full)

    # --- CPU schlicht ausgelastet -------------------------------------------------
    saturated = lambda r: r["cpu"] >= 90
    sat, _ = share(rows, saturated)
    if sat is not None and sat >= 5:
        add("warn", "Der Prozessor ist voll ausgelastet",
            f"In {fmt(sat, ' %')} der Zeit waren alle Kerne zu 90 % oder mehr beschäftigt.",
            "Alle Rechenkerne haben gleichzeitig gearbeitet. Jedes weitere Programm muss dann warten - "
            "der ganze Rechner fühlt sich zäh an.",
            ["Die Liste darüber zeigt, welche Programme in diesen Phasen am meisten gerechnet haben.",
             "Ist es ein Programm, das du gerade bewusst benutzt (Export, Build, Videokonferenz), ist das normal.",
             "Läuft es im Hintergrund, ohne dass du es brauchst: schließen oder seinen Autostart abschalten."],
            when=saturated)

    one_core = lambda r: r["core_max"] >= 95 and r["cpu"] < 50
    single, _ = share(rows, one_core)
    if single is not None and single >= 5:
        per_core = (f" Bei deinen {fmt(cores)} Kernen taucht so ein Programm in der Liste mit nur etwa "
                    f"{fmt(100 / cores)} % Gesamtanteil auf." if cores else "")
        always, _ = share(rows, lambda r: r["core_max"] >= 95)
        own = [v for v in col(rows, "self_cpu") if v is not None]
        self_cores = sum(own) / len(own) if own else None
        if always is not None and always >= 98:
            per_core += (" Der Kern war praktisch die ganze Messung über voll. Das spricht für ein Programm, das "
                         "dauerhaft rechnet oder hängt - im Task-Manager unter Leistung > CPU > „Logische "
                         "Prozessoren“ lässt sich das gegenprüfen.")
        if self_cores is not None and cores and self_cores * cores >= 30:
            per_core += (f" Achtung: Das Messskript selbst hat etwa {fmt(self_cores * cores)} % eines Kerns "
                         "gebraucht und kann einen Teil dieses Befunds verursacht haben.")
        add("warn", "Ein Programm lastet einen einzelnen Kern voll aus",
            f"In {fmt(single, ' %')} der Zeit war ein Kern komplett belegt, obwohl insgesamt noch viel "
            "Leistung frei war.",
            "Viele Programme können nur einen Kern nutzen. Ist dieser eine Kern voll, reagiert genau dieses "
            "Programm träge - egal, wie viele andere Kerne frei sind. Der Rest des Rechners bleibt dabei "
            "meist flüssig." + per_core,
            ["Die Liste darüber zeigt, welche Programme in diesen Phasen am meisten gerechnet haben.",
             "Tut das Programm das dauerhaft, obwohl du nichts damit machst, hat es sich vermutlich "
             "aufgehängt: beenden und neu starten.",
             "Arbeitest du gerade damit, ist das normal. Schneller wird es dann nur mit einem Prozessor, "
             "der pro Kern schneller ist - mehr Kerne helfen nicht."],
            when=one_core)

    # --- RAM ----------------------------------------------------------------------
    # "Pages Input/sec" zählt auch ganz normales Einlesen von Programm- und Datendateien.
    # Echtes Auslagern zeigt sich an "Pages Output/sec" (Schreiben in die Auslagerungsdatei).
    ram_p90 = quantile(col(rows, "ram"), 0.90)
    faults_p95 = quantile(col(rows, "hard_faults"), 0.95)
    swapping = lambda r: r["pages_out"] >= 100
    paging, _ = share(rows, swapping)
    hogs = sorted((p for p in top_processes(rows) if p["ram_mb"]), key=lambda p: p["ram_mb"], reverse=True)[:5]
    hog_text = ("Größte Speicherverbraucher: " + ", ".join(
        f"{p['name']} ({fmt(p['ram_mb'] / 1000, ' GB', 1)})" for p in hogs) + ".") if hogs else None
    ram_actions = ["Die größten Verbraucher stehen unten. Nicht benötigte Programme und Browser-Tabs schließen.",
                   "Kommt das im Alltag regelmäßig vor, hilft auf Dauer nur mehr Arbeitsspeicher."]
    if ram_p90 is not None and ram_p90 > 88:
        if paging is not None and paging >= 5:
            add("bad", "Der Arbeitsspeicher reicht nicht aus",
                f"Er war zu {fmt(ram_p90, ' %')} gefüllt, und in {fmt(paging, ' %')} der Zeit musste Windows "
                "Daten auf das Laufwerk auslagern.",
                "Ist der Arbeitsspeicher voll, schiebt Windows Daten auf das viel langsamere Laufwerk und holt "
                "sie später zurück. Das spürt man als Hänger beim Wechseln zwischen Programmen.",
                ram_actions, when=swapping, extra=hog_text)
        elif paging is not None:
            add("warn", "Der Arbeitsspeicher ist fast voll",
                f"Er war zu {fmt(ram_p90, ' %')} gefüllt. Gebremst hat das in dieser Messung aber noch nicht.",
                "Windows musste kaum Daten auf das Laufwerk auslagern - es war also knapp, aber noch nicht zu "
                "wenig. Kommt ein weiteres großes Programm dazu, kann das kippen.",
                ram_actions, extra=hog_text)
        else:
            more = (f" Dabei wurden bis zu {fmt(faults_p95)} Speicherseiten pro Sekunde vom Laufwerk geladen."
                    if faults_p95 is not None and faults_p95 > 500 else "")
            add("warn", "Der Arbeitsspeicher ist fast voll",
                f"Er war zu {fmt(ram_p90, ' %')} gefüllt.{more}",
                "Diese ältere Messung kann nicht unterscheiden, ob Windows wirklich ausgelagert hat. "
                "Eine neue Messung zeigt das.",
                ram_actions, extra=hog_text)
    elif paging is not None and paging >= 5:
        add("warn", "Windows lagert Daten auf das Laufwerk aus",
            f"In {fmt(paging, ' %')} der Zeit, obwohl der Arbeitsspeicher nicht dauerhaft voll war.",
            "Windows hat Daten aus dem Arbeitsspeicher auf das Laufwerk geschoben. Das bremst kurz, "
            "ist bei einzelnen Schüben aber unkritisch.",
            ram_actions, when=swapping, extra=hog_text)

    # --- Datenträger (jeweils das am stärksten betroffene Laufwerk) ----------------
    def worst_drive(key, predicate):
        names = [r.get(key) for r in rows if r.get(key) and _safe(predicate, r)]
        return f" Betroffen ist vor allem Laufwerk {max(set(names), key=names.count)}." if names else ""

    disk_actions = ["Die Liste darüber zeigt, welche Programme in diesen Momenten am meisten gelesen und geschrieben haben.",
                    "Häufige Auslöser sind Virenscanner, Windows-Update, die Suchindexierung und Cloud-Abgleich.",
                    "Freien Speicherplatz prüfen - ein fast volles Laufwerk kann langsamer werden.",
                    "Tritt es oft auf, den Zustand des Laufwerks auslesen (z. B. mit CrystalDiskInfo)."]
    busy_pred = lambda r: r["disk_busy"] >= 90
    slow_pred = lambda r: r["disk_lat_ms"] > 25
    stall_pred = lambda r: r["disk_lat_ms"] >= 100
    busy, _ = share(rows, busy_pred)
    lat_p95 = quantile(col(rows, "disk_lat_ms"), 0.95)
    lat_max = quantile(col(rows, "disk_lat_ms"), 1.0)
    stalls, _ = share(rows, stall_pred)
    if busy is not None and busy >= 5:
        add("warn", "Ein Laufwerk ist dauernd beschäftigt",
            f"In {fmt(busy, ' %')} der Zeit hat es ohne Pause gearbeitet." + worst_drive("disk_name", busy_pred),
            "Ein Laufwerk kann immer nur eine begrenzte Menge gleichzeitig lesen und schreiben. Ist es voll "
            "beschäftigt, warten alle Programme, die etwas davon brauchen.",
            disk_actions, when=busy_pred, by="io")
    if lat_p95 is not None and lat_p95 > 25:
        add("bad" if lat_p95 > 100 else "warn", "Ein Laufwerk antwortet langsam",
            f"Zugriffe dauerten oft bis zu {fmt(lat_p95, ' ms')}. Üblich sind bei einer SSD weniger als 5 ms."
            + worst_drive("disk_lat_name", slow_pred),
            "Jedes Öffnen, Speichern und Starten wartet auf das Laufwerk. Sind die Antwortzeiten dauerhaft "
            "hoch, wirkt der ganze Rechner träge.",
            disk_actions, when=slow_pred, by="io")
    elif stalls is not None and stalls >= 1:
        add("warn", "Ein Laufwerk hängt gelegentlich kurz",
            f"In {fmt(stalls, ' %', 1)} der Zeit dauerte ein Zugriff eine Zehntelsekunde oder länger "
            f"(längster: {fmt(lat_max, ' ms')})." + worst_drive("disk_lat_name", stall_pred),
            "Die meiste Zeit antwortet das Laufwerk schnell, zwischendurch aber gar nicht. In so einem Moment "
            "friert das Programm, das gerade darauf zugreift, kurz ein. Einzelne Aussetzer sind normal - "
            "störend wird es erst, wenn sie sich häufen.",
            disk_actions, when=stall_pred, by="io")

    if not out:
        add("ok", "Keine Engstelle gefunden",
            "Die gemessenen Größen haben in dieser Messung nicht gebremst.",
            ("Prozessor, Arbeitsspeicher, Laufwerke und Grafikeinheit waren unauffällig. Wenn es trotzdem "
             "hakt, kommen Netzwerk oder das Programm selbst in Frage. Diese misst das Skript nicht."
             if any(r["gpu"] is not None for r in rows) else
             "Wenn es trotzdem hakt, kommen Grafikkarte, Netzwerk oder das Programm selbst in Frage. "
             "Diese drei wurden in dieser Messung nicht erfasst."),
            ["Beim nächsten Hänger im Aufzeichnungsfenster ENTER drücken. Der Bericht zeigt dann, "
             "was genau in diesem Moment los war."])
    return out


LEVEL_LABEL = {"bad": "Problem", "warn": "Auffällig", "ok": "In Ordnung"}


def render_findings(rows, findings) -> str:
    esc = html.escape
    parts = []
    for f in findings:
        body = f"<h3>Was das bedeutet</h3><p>{esc(f['meaning'])}</p>"
        if f["extra"]:
            body += f"<p>{esc(f['extra'])}</p>"
        if f["when"]:
            spans = episodes(rows, f["when"])
            if spans:
                total = sum((b - a).total_seconds() + 1 for a, b in spans)
                longest = sorted(sorted(spans, key=lambda s: s[1] - s[0], reverse=True)[:5])
                items = "".join(
                    f"<li>{a:%H:%M:%S}" + (f" bis {b:%H:%M:%S} ({fmt_duration((b - a).total_seconds() + 1)})"
                                           if b > a else " (kurz)") + "</li>" for a, b in longest)
                more = (f"<li>… und {len(spans) - 5} weitere{'r' if len(spans) == 6 else ''} kürzere{'r' if len(spans) == 6 else ''} Abschnitt{'e' if len(spans) != 6 else ''}</li>") if len(spans) > 5 else ""
                body += (f"<h3>Wann es auftrat</h3><p>{len(spans)} Abschnitt{'e' if len(spans) != 1 else ''}, "
                         f"zusammen {fmt_duration(total)}.{' Die längsten:' if len(spans) > 5 else ''}</p>"
                         f"<ul>{items}{more}</ul>")
            procs = procs_during(rows, f["when"], f["by"])
            if procs:
                if f["by"] == "io":
                    items = "".join(f"<li>{esc(n)} - {fmt(io, ' MB')} gelesen und geschrieben</li>"
                                    for n, _, io in procs)
                else:
                    items = "".join(f"<li>{esc(n)} - {fmt(cpu, ' %', 1)} der gesamten Rechenleistung</li>"
                                    for n, cpu, _ in procs)
                body += f"<h3>Aktivste Programme in diesen Phasen</h3><ul>{items}</ul>"
        body += "<h3>Was du tun kannst</h3><ul>" + "".join(f"<li>{esc(a)}</li>" for a in f["actions"]) + "</ul>"
        parts.append(
            f'<details class="finding {f["level"]}"><summary>'
            f'<span class="badge">{LEVEL_LABEL[f["level"]]}</span>'
            f'<span class="f-title">{esc(f["title"])}</span>'
            f'<span class="f-sum">{esc(f["summary"])}</span></summary>'
            f'<div class="f-body">{body}</div></details>')
    return "".join(parts)


def analyze_markers(rows) -> list:
    result = []
    for idx, row in enumerate(rows):
        if not row["marker"]:
            continue
        t = row["ts"].timestamp()
        window = [r for r in rows[max(0, idx - 2000): idx + 2000]
                  if -MARKER_BEFORE_S <= r["ts"].timestamp() - t <= MARKER_AFTER_S]
        peak = lambda key: quantile(col(window, key), 1.0)
        low = lambda key: quantile(col(window, key), 0.0)
        flags = []
        cpu, core, perf = peak("cpu"), peak("core_max"), low("perf_pct")
        ram, busy, lat, faults = peak("ram"), peak("disk_busy"), peak("disk_lat_ms"), peak("pages_out")
        limit = low("passive_limit")
        gpu, cpu_temp = peak("gpu"), peak("cpu_temp")
        if cpu is not None and cpu >= 90:
            flags.append(f"CPU-Gesamtlast bis {fmt(cpu, ' %')}")
        elif core is not None and core >= 95:
            flags.append(f"ein Kern bei {fmt(core, ' %')} (Gesamtlast nur {fmt(cpu, ' %')})")
        if perf is not None and perf < 75 and cpu is not None and cpu >= 50:
            flags.append(f"CPU-Takt auf {fmt(perf, ' %')} des Nenntakts gefallen")
        if limit is not None and limit < 100:
            flags.append(f"thermisches Limit {fmt(limit, ' %')}")
        if cpu_temp is not None and cpu_temp >= 95:
            flags.append(f"CPU-Temperatur bis {fmt(cpu_temp, ' °C')}")
        if gpu is not None and gpu >= 90:
            flags.append(f"Grafikeinheit bis {fmt(gpu, ' %')} ausgelastet")
        if ram is not None and ram > 90:
            flags.append(f"RAM bei {fmt(ram, ' %')}")
        if faults is not None and faults >= 100:
            flags.append(f"{fmt(faults)} Seiten/s in die Auslagerungsdatei geschrieben")
        if lat is not None and lat > 25:
            slow = max((r for r in window if r["disk_lat_ms"] is not None), key=lambda r: r["disk_lat_ms"])
            flags.append(f"Datenträger-Antwortzeit bis {fmt(lat, ' ms', 1)}"
                         + (f" (Laufwerk {slow['disk_lat_name']})" if slow.get("disk_lat_name") else ""))
        elif busy is not None and busy >= 90:
            flags.append(f"Datenträger zu {fmt(busy, ' %')} aktiv")
        sweeps = [r["top"] for r in window if r["top"]]
        if not sweeps:                         # der nächste Durchlauf danach deckt den Marker-Zeitraum ab
            sweeps = [r["top"] for r in rows[idx: idx + 120] if r["top"]][:1]
        best_cpu, best_io = top_label(sweeps)
        procs = []
        if best_cpu and best_cpu[1] >= 5:
            procs.append(f"{best_cpu[0]} ({fmt(best_cpu[1], ' %')} CPU)")
        if best_io and best_io[1] >= 20_000_000 and (not best_cpu or best_io[0] != best_cpu[0]):
            procs.append(f"{best_io[0]} ({fmt(best_io[1] / 1e6, ' MB')} Datenträger-I/O)")
        result.append({"ts": row["ts"], "label": row["marker"], "flags": flags, "procs": procs})
    return result


def top_processes(rows) -> list:
    sweeps = [r["top"] for r in rows if r["top"] is not None]
    agg = {}
    for sweep in sweeps:
        for name, cpu, io, *rest in sweep:
            entry = agg.setdefault(name, {"sum": 0.0, "peak": 0.0, "io": 0, "ram": None})
            entry["sum"] += cpu
            entry["peak"] = max(entry["peak"], cpu)
            entry["io"] += io
            if rest:
                entry["ram"] = max(entry["ram"] or 0, rest[0])
    table = [{"name": n, "avg": e["sum"] / len(sweeps), "peak": e["peak"], "io_mb": e["io"] / 1e6,
              "ram_mb": e["ram"]} for n, e in agg.items()]
    by_cpu = sorted(table, key=lambda e: (e["avg"], e["io_mb"]), reverse=True)[:10]
    by_ram = sorted(table, key=lambda e: e["ram_mb"] or 0, reverse=True)[:5]
    return by_cpu + [e for e in by_ram if e not in by_cpu and e["ram_mb"]]


def downsample(rows, keys_max, keys_min, limit=1200):
    """Verdichtet lange Sitzungen, ohne Spitzen (bzw. Takt-Einbrüche) wegzumitteln."""
    step = max(1, -(-len(rows) // limit))
    times, series = [], {k: [] for k in keys_max + keys_min}
    for i in range(0, len(rows), step):
        chunk = rows[i:i + step]
        times.append(int(chunk[0]["ts"].timestamp() * 1000))
        for key in keys_max:
            series[key].append(quantile(col(chunk, key), 1.0))
        for key in keys_min:
            series[key].append(quantile(col(chunk, key), 0.0))
    return times, series


# =============================================================================
# 4. HTML-Bericht (eine Datei, ohne Internet lauffähig)
# =============================================================================
CHARTS = [
    {"title": "CPU-Last", "unit": "%", "max": 100, "series": [
        ["cpu", "Gesamt", "a"], ["core_max", "am stärksten belasteter Kern", "b"]]},
    {"title": "CPU-Takt relativ zum Nenntakt", "unit": "%", "ref": 100,
     "note": "Über 100 % = Turbo. Fällt der Wert bei hoher Last deutlich unter 100 %, wird gedrosselt.",
     "series": [["perf_pct", "Takt", "a"]]},
    {"title": "Arbeitsspeicher", "unit": "%", "max": 100, "series": [
        ["ram", "physisch belegt", "a"], ["commit", "zugesichert (inkl. Auslagerungsdatei)", "b"]]},
    {"title": "Datenträger-Aktivität", "unit": "%", "max": 100, "note": "Je Messpunkt das am stärksten beschäftigte Laufwerk.",
     "series": [["disk_busy", "aktive Zeit", "a"]]},
    {"title": "Datenträger-Antwortzeit", "unit": "ms", "note": "Je Messpunkt das langsamste Laufwerk.",
     "series": [["disk_lat_ms", "Antwortzeit je Zugriff", "a"]]},
    {"title": "Speicherseiten von/zur Platte", "unit": "/s",
     "note": "Gelesen: auch normales Laden von Programmen und Dateien. Geschrieben: Windows lagert aus - "
             "dauerhaft über 0 heißt, der RAM reicht nicht.",
     "series": [["hard_faults", "gelesen", "a"], ["pages_out", "in Auslagerungsdatei geschrieben", "b"]]},
    {"title": "Grafikeinheit", "unit": "%", "max": 100,
     "note": "Auslastung der am stärksten beschäftigten GPU-Engine (wie im Task-Manager).",
     "series": [["gpu", "Auslastung", "a"]]},
    {"title": "Grafikspeicher", "unit": "MB",
     "note": "Eigener und mit dem Arbeitsspeicher geteilter Speicher. Eine Onboard-Grafik nutzt fast nur geteilten.",
     "series": [["gpu_mem_mb", "belegt", "a"]]},
    {"title": "Temperatur", "unit": "°C", "ref": 95,
     "note": "Gelesen über LibreHardwareMonitor. Ab der gestrichelten Linie (95 °C) drosseln Prozessoren meist.",
     "series": [["cpu_temp", "Prozessor (wärmster Sensor)", "a"], ["gpu_temp", "Grafik", "b"]]},
    {"title": "Temperatur (ACPI-Thermalzone)", "unit": "°C",
     "note": "Grober Wert des Mainboards, nicht die Kerntemperatur der CPU.",
     "series": [["temp_c", "wärmste Zone", "b"]]},
]

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bottleneck-Bericht __TITLE__</title>
<style>
  :root { --bg:#f5f6f8; --card:#fff; --ink:#1c2330; --muted:#5d6878; --line:#dfe3ea; --grid:#e8ebf0;
          --a:#2563c9; --b:#c2570c; --bad:#b42318; --warn:#a15c00; --ok:#1f7a3d; --info:#2563c9; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#12151b; --card:#1b2029; --ink:#e7eaf0; --muted:#9aa5b5; --line:#2c3340; --grid:#262d39;
            --a:#6ea8ff; --b:#f0975a; --bad:#ff8a7a; --warn:#f2b85c; --ok:#6fd08c; --info:#6ea8ff; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--ink);
         font:15px/1.5 "Segoe UI", system-ui, sans-serif; }
  main { max-width:1100px; margin:0 auto; }
  h1 { font-size:22px; margin:0 0 4px; }
  h2 { font-size:16px; margin:0 0 12px; }
  .sub { color:var(--muted); margin:0 0 20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px 20px; margin-bottom:16px; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:16px; }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
  .tile b { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
  .tile span { color:var(--muted); font-size:12.5px; }
  .finding { border:1px solid var(--line); border-left:6px solid var(--info); border-radius:8px;
             background:var(--bg); margin:0 0 12px; }
  .finding:last-child { margin-bottom:0; }
  .finding.bad { border-left-color:var(--bad); } .finding.bad .badge { color:var(--bad); }
  .finding.warn { border-left-color:var(--warn); } .finding.warn .badge { color:var(--warn); }
  .finding.ok { border-left-color:var(--ok); } .finding.ok .badge { color:var(--ok); }
  .finding summary { cursor:pointer; list-style:none; padding:14px 16px; display:grid;
                     grid-template-columns:auto 1fr auto; gap:6px 12px; align-items:center; border-radius:8px; }
  .finding summary::-webkit-details-marker { display:none; }
  .finding summary::after { content:"Details anzeigen \25BE"; color:var(--muted); font-size:13px; white-space:nowrap;
                            grid-column:1 / -1; }
  .finding[open] summary::after { content:"Details ausblenden \25B4"; }
  .finding summary:hover::after { color:var(--ink); text-decoration:underline; }
  .finding summary:focus-visible { outline:2px solid var(--info); outline-offset:2px; }
  .badge { font-size:12px; font-weight:700; letter-spacing:.03em; text-transform:uppercase;
           border:1.5px solid currentColor; border-radius:999px; padding:1px 9px; white-space:nowrap; }
  .f-title { font-size:17px; font-weight:700; line-height:1.3; }
  .f-sum { grid-column:1 / -1; font-size:15.5px; line-height:1.55; }
  .f-body { padding:2px 16px 14px; border-top:1px solid var(--line); font-size:15px; line-height:1.6; }
  .f-body h3 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:16px 0 4px; }
  .f-body p { margin:0 0 6px; } .f-body ul { margin:0; padding-left:20px; } .f-body li { margin-bottom:3px; }
  @media (max-width:600px) { .finding summary { grid-template-columns:1fr; } }
  table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
  th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:600; font-size:12.5px; }
  td.n, th.n { text-align:right; }
  .chart-head { display:flex; flex-wrap:wrap; gap:4px 16px; align-items:baseline; margin-bottom:4px; }
  .chart-head h2 { margin:0; }
  .key { font-size:12.5px; color:var(--muted); white-space:nowrap; }
  .key i { display:inline-block; width:14px; height:3px; border-radius:2px; vertical-align:middle; margin-right:5px; }
  .key b { color:var(--ink); font-variant-numeric:tabular-nums; }
  .note { color:var(--muted); font-size:12.5px; margin:4px 0 0; }
  svg { display:block; width:100%; height:auto; }
  svg text { fill:var(--muted); font-size:11px; font-family:inherit; }
  .cursor-time { color:var(--muted); font-size:12.5px; min-height:1.5em; }
</style>
</head>
<body>
<main>
  <h1>Bottleneck-Bericht</h1>
  <p class="sub">__SUBTITLE__</p>
  <div class="tiles">__TILES__</div>
  <div class="card"><h2>Diagnose</h2>__FINDINGS__
    <p class="note">Auf eine Meldung klicken, um Zeitpunkte, beteiligte Programme und Abhilfe zu sehen.</p></div>
  __MARKERS__
  <div class="card"><h2>Verlauf</h2>
    <div class="cursor-time" id="cursorTime">Mit der Maus über ein Diagramm fahren, um Werte abzulesen.__MARKERHINT__</div>
  </div>
  <div id="charts"></div>
  __PROCESSES__
  <div class="card"><h2>Hinweise zur Messung</h2><p class="note" style="font-size:14px">__NOTES__</p></div>
</main>
<script>
const D = __DATA__;
const W = 1000, H = 190, L = 46, R = 10, T = 10, B = 22;
const t0 = D.t[0], t1 = D.t[D.t.length - 1], span = Math.max(1, t1 - t0);
const X = t => L + (t - t0) / span * (W - L - R);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const timeFmt = ms => new Date(ms).toLocaleTimeString('de-DE');
const numFmt = v => v == null ? 'n/v' : v.toLocaleString('de-DE', {maximumFractionDigits: v < 10 ? 1 : 0});
const cursors = [];

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
  return 10 * p;
}

D.charts.forEach(c => {
  const all = c.series.flatMap(s => D.series[s[0]]).filter(v => v != null);
  const top = c.max || niceMax(Math.max(...all, c.ref || 0) * 1.05);
  const Y = v => T + (1 - Math.min(v, top) / top) * (H - T - B);
  let g = '';
  for (let i = 0; i <= 4; i++) {
    const v = top * i / 4, y = Y(v);
    g += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="var(--grid)"/>` +
         `<text x="${L - 6}" y="${y + 4}" text-anchor="end">${numFmt(v)}</text>`;
  }
  for (let i = 0; i <= 5; i++) {
    const t = t0 + span * i / 5;
    g += `<text x="${X(t)}" y="${H - 5}" text-anchor="${i == 0 ? 'start' : i == 5 ? 'end' : 'middle'}">${timeFmt(t)}</text>`;
  }
  if (c.ref) g += `<line x1="${L}" x2="${W - R}" y1="${Y(c.ref)}" y2="${Y(c.ref)}" stroke="var(--muted)" stroke-dasharray="2 4"/>`;
  D.markers.forEach((m, i) => {
    g += `<line x1="${X(m.t)}" x2="${X(m.t)}" y1="${T}" y2="${H - B}" stroke="var(--bad)" stroke-dasharray="4 3"/>` +
         `<text x="${X(m.t) + 3}" y="${T + 9}" style="fill:var(--bad)">${i + 1}</text>`;
  });
  c.series.forEach(s => {
    let d = '', pen = false;
    D.series[s[0]].forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += (pen ? 'L' : 'M') + X(D.t[i]).toFixed(1) + ' ' + Y(v).toFixed(1);
      pen = true;
    });
    g += `<path d="${d}" fill="none" stroke="var(--${s[2]})" stroke-width="1.6" stroke-linejoin="round"/>`;
  });
  g += `<line class="cur" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="var(--ink)" stroke-width="1" visibility="hidden"/>`;

  const card = document.createElement('div');
  card.className = 'card';
  const keys = c.series.map(s =>
    `<span class="key"><i style="background:var(--${s[2]})"></i>${s[1]} <b data-k="${s[0]}"></b></span>`).join('');
  card.innerHTML = `<div class="chart-head"><h2>${c.title} <span class="key">(${c.unit})</span></h2>${keys}</div>` +
    `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${c.title}">${g}</svg>` +
    (c.note ? `<p class="note">${c.note}</p>` : '');
  document.getElementById('charts').appendChild(card);
  const svg = card.querySelector('svg');
  cursors.push({line: svg.querySelector('.cur'), card, unit: c.unit});
  svg.addEventListener('mousemove', e => {
    const box = svg.getBoundingClientRect();
    const t = t0 + ((e.clientX - box.left) / box.width * W - L) / (W - L - R) * span;
    let lo = 0, hi = D.t.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (D.t[mid] < t) lo = mid + 1; else hi = mid; }
    if (lo > 0 && Math.abs(D.t[lo - 1] - t) < Math.abs(D.t[lo] - t)) lo--;
    showCursor(lo);
  });
  svg.addEventListener('mouseleave', () => showCursor(-1));
});

function showCursor(i) {
  const hint = document.getElementById('cursorTime');
  hint.dataset.idle = hint.dataset.idle || hint.textContent;
  hint.textContent = i < 0 ? hint.dataset.idle : 'Zeitpunkt: ' + timeFmt(D.t[i]);
  cursors.forEach(c => {
    c.line.setAttribute('visibility', i < 0 ? 'hidden' : 'visible');
    if (i >= 0) { c.line.setAttribute('x1', X(D.t[i])); c.line.setAttribute('x2', X(D.t[i])); }
    c.card.querySelectorAll('b[data-k]').forEach(b => {
      b.textContent = i < 0 ? '' : numFmt(D.series[b.dataset.k][i]) + ' ' + c.unit;
    });
  });
}
</script>
</body>
</html>
"""


def build_report(csv_path: Path, html_path: Path) -> bool:
    meta, rows = load_csv(csv_path)
    if len(rows) < 5:
        print("Zu wenige Messpunkte für eine Auswertung - bitte länger laufen lassen.")
        return False
    esc = html.escape
    start, end = rows[0]["ts"], rows[-1]["ts"]
    minutes = (end - start).total_seconds() / 60
    nominal = _num(meta.get("nominal_mhz"))

    # --- Kacheln ---
    perf_loaded = [r["perf_pct"] for r in rows if r["cpu"] is not None and r["cpu"] >= 50]
    takt_label = "Takt unter Last (Median)"
    if not perf_loaded:
        perf_loaded, takt_label = col(rows, "perf_pct"), "Takt (Median, keine Lastphase)"
    perf_med = quantile(perf_loaded, 0.5)
    takt = fmt(perf_med, " %")
    if perf_med and nominal:
        takt_label += f" - {fmt(nominal * perf_med / 100)} MHz"
    tiles = [
        (fmt(quantile(col(rows, "cpu"), 0.95), " %"), "CPU-Last (P95)"),
        (fmt(quantile(col(rows, "core_max"), 0.95), " %"), "stärkster Kern (P95)"),
        (takt if perf_med is not None else "n/v", takt_label),
        (fmt(quantile(col(rows, "ram"), 1.0), " %"), "RAM (Maximum)"),
        (fmt(quantile(col(rows, "disk_lat_ms"), 0.95), " ms", 1), "Datenträger-Antwortzeit (P95)"),
    ]
    gpu_p95 = quantile(col(rows, "gpu"), 0.95)
    if gpu_p95 is not None:
        tiles.append((fmt(gpu_p95, " %"), "Grafikeinheit (P95)"))
    cpu_temp_max = quantile(col(rows, "cpu_temp"), 1.0)
    temp_max = quantile(col(rows, "temp_c"), 1.0)
    if cpu_temp_max is not None:
        tiles.append((fmt(cpu_temp_max, " °C"), "CPU-Temperatur (Maximum)"))
    elif temp_max is not None:
        tiles.append((fmt(temp_max, " °C"), "Thermalzone (Maximum)"))
    tiles_html = "".join(f'<div class="tile"><b>{esc(v)}</b><span>{esc(l)}</span></div>' for v, l in tiles)

    # --- Diagnose ---
    findings_html = render_findings(rows, diagnose(rows, meta))

    # --- Marker ---
    markers = analyze_markers(rows)
    markers_html = ""
    if markers:
        body = ""
        for i, m in enumerate(markers, 1):
            what = "; ".join(m["flags"]) if m["flags"] else \
                "keine Auffälligkeit in den gemessenen Größen (evtl. Netzwerk oder die Anwendung selbst)"
            procs = ", ".join(m["procs"]) or "-"
            body += (f"<tr><td class='n'>{i}</td><td>{m['ts']:%H:%M:%S}</td><td>{esc(m['label'])}</td>"
                     f"<td>{esc(what)}</td><td>{esc(procs)}</td></tr>")
        markers_html = (f'<div class="card"><h2>Deine Marker - was in den {MARKER_BEFORE_S} Sekunden davor los war</h2>'
                        "<table><tr><th class='n'>Nr.</th><th>Zeit</th><th>Notiz</th><th>Auffällig</th>"
                        f"<th>Aktivster Prozess</th></tr>{body}</table></div>")

    # --- Prozesse ---
    procs = top_processes(rows)
    sweep_times = [r["ts"] for r in rows if r["top"] is not None]
    gaps = [(b - a).total_seconds() for a, b in zip(sweep_times, sweep_times[1:])]
    sweep_every = f"etwa alle {fmt(quantile(gaps, 0.5))} s" if gaps else "in Abständen"
    processes_html = ""
    if procs:
        body = "".join(
            f"<tr><td>{esc(p['name'])}</td><td class='n'>{fmt(p['avg'], ' %', 1)}</td>"
            f"<td class='n'>{fmt(p['peak'], ' %')}</td><td class='n'>{fmt(p['io_mb'], ' MB')}</td>"
            f"<td class='n'>{fmt(p['ram_mb'] / 1000, ' GB', 1) if p['ram_mb'] else 'n/v'}</td></tr>" for p in procs)
        processes_html = ('<div class="card"><h2>Aktivste Prozesse</h2><table><tr><th>Prozess</th>'
                          "<th class='n'>CPU im Schnitt</th><th class='n'>CPU-Spitze</th>"
                          "<th class='n'>Datenträger-I/O gesamt</th><th class='n'>RAM (Spitze)</th></tr>"
                          f"{body}</table><p class='note'>CPU-Werte als Anteil an der gesamten Rechenleistung "
                          f"(alle Kerne = 100 %), erfasst {sweep_every}. Gleichnamige Prozesse "
                          "sind zusammengefasst.</p></div>")

    # --- Diagrammdaten ---
    keys_max = ["cpu", "core_max", "ram", "commit", "disk_busy", "disk_lat_ms", "hard_faults", "pages_out",
                "temp_c", "gpu", "gpu_mem_mb", "cpu_temp", "gpu_temp"]
    keys_min = ["perf_pct"]
    times, series = downsample(rows, keys_max, keys_min)
    charts = []
    for chart in CHARTS:
        kept = [s for s in chart["series"] if any(v is not None for v in series[s[0]])]
        if kept:
            charts.append({**chart, "series": kept})
    missing = sorted({s[0] for c in CHARTS for s in c["series"]} - {s[0] for c in charts for s in c["series"]})
    data = {
        "t": times,
        "series": {k: v for k, v in series.items() if any(s[0] == k for c in charts for s in c["series"])},
        "charts": charts,
        "markers": [{"t": int(m["ts"].timestamp() * 1000)} for m in markers],
    }

    notes = [f"Messintervall {esc(meta.get('interval', '?'))} s, {len(rows)} Messpunkte."]
    interval = _num(meta.get("interval"))
    expected = (end - start).total_seconds() / interval + 1 if interval else None
    if expected and len(rows) < 0.85 * expected:
        notes.insert(0, f"Achtung: Es fehlen {fmt(100 - 100 * len(rows) / expected, ' %')} der Messpunkte "
                        f"({len(rows)} statt etwa {fmt(expected)}). Das Skript kam zeitweise nicht hinterher; "
                        "Zeitanteile in der Diagnose sind deshalb ungenau.")
    own = [v for v in col(rows, "self_cpu") if v is not None]
    self_med = sum(own) / len(own) if own else None
    if self_med is not None:
        notes.append(f"Eigenverbrauch des Messskripts: {fmt(self_med, ' %', 1)} der gesamten Rechenleistung "
                     "(Durchschnitt).")
    sweep_med = quantile(col(rows, "sweep_s"), 0.5)
    if sweep_med is not None and sweep_med >= 1:
        notes.append(f"Die Prozess-Abfrage dauerte auf diesem Gerät etwa {fmt(sweep_med, ' s', 1)} je Durchlauf "
                     "und läuft deshalb seltener.")
    if minutes < 2:
        notes.insert(0, "Sehr kurze Messung - die Diagnose ist nur eine Momentaufnahme. Aussagekräftig wird es "
                        "ab einigen Minuten normaler Arbeit, am besten mit Markern.")
    if len(rows) > len(times):
        notes.append("Für die Diagramme wurden die Daten verdichtet; dabei bleibt je Abschnitt die Spitze "
                     "(beim Takt der tiefste Wert) erhalten.")
    if missing:
        labels = {s[0]: f"{c['title']} ({s[1]})" for c in CHARTS for s in c["series"]}
        notes.append("Auf diesem Gerät nicht messbar: " + "; ".join(labels[k] for k in missing) + ".")
    if meta.get("gpu_name"):
        notes.append(f"Grafikeinheit: {esc(meta['gpu_name'])}.")
    has = lambda key: any(r[key] is not None for r in rows)
    if has("cpu_temp") and not has("gpu_temp"):
        notes.append("Die Grafikeinheit meldet keine eigene Temperatur. Eine Onboard-Grafik sitzt im Prozessor, "
                     "maßgeblich ist dann die CPU-Temperatur.")
    if not has("cpu_temp"):
        notes.append("Die CPU-Temperatur fehlt: Dafür muss das Skript als Administrator laufen (start.bat fragt "
                     "danach) und der PawnIO-Treiber installiert sein (winget install PawnIO).")
    notes.append("Nicht erfasst wird das Netzwerk.")
    batt, _ = share(rows, lambda r: r["on_battery"] == 1)
    if batt is not None:
        notes.append(f"Akkubetrieb: {fmt(batt, ' %')} der Zeit.")

    subtitle = (f"{start:%d.%m.%Y %H:%M:%S} bis {end:%H:%M:%S} ({fmt(minutes, ' min', 1)})"
                + (f" - {esc(meta['cpu_name'])}" if meta.get("cpu_name") else ""))
    page = (HTML_TEMPLATE
            .replace("__TITLE__", f"{start:%d.%m.%Y %H:%M}")
            .replace("__SUBTITLE__", subtitle)
            .replace("__TILES__", tiles_html)
            .replace("__FINDINGS__", findings_html)
            .replace("__MARKERS__", markers_html)
            .replace("__MARKERHINT__", " Rote gestrichelte Linien sind deine Marker." if markers else "")
            .replace("__PROCESSES__", processes_html)
            .replace("__NOTES__", " ".join(notes))
            .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    html_path.write_text(page, encoding="utf-8")
    return True


# =============================================================================
# 5. Programmstart
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Zeichnet Laptop-Engstellen auf und erzeugt einen HTML-Bericht.")
    parser.add_argument("--interval", type=float, default=1.0, help="Messintervall in Sekunden (Standard: 1.0)")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "logs",
                        help="Zielordner für CSV und Bericht")
    parser.add_argument("--analyze", type=Path, metavar="CSV", help="vorhandene CSV auswerten statt neu zu messen")
    parser.add_argument("--no-open", action="store_true", help="Bericht nicht im Browser öffnen")
    args = parser.parse_args()
    if args.interval < 0.2:
        parser.error("--interval muss mindestens 0.2 Sekunden betragen")

    if args.analyze:
        csv_path = args.analyze
        if not csv_path.is_file():
            sys.exit(f"Datei nicht gefunden: {csv_path}")
        html_path = csv_path.with_suffix(".html")
    else:
        args.out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        csv_path = args.out / f"bottleneck_{stamp}.csv"
        html_path = args.out / f"bottleneck_{stamp}.html"
        if not record(csv_path, args.interval):
            sys.exit("Es wurden keine Messpunkte aufgezeichnet.")

    print("Werte Messdaten aus ...")
    if build_report(csv_path, html_path):
        print(f"Bericht: {html_path}")
        if not args.no_open:
            webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
