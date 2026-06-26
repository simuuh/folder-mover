# CLAUDE.md

Diese Datei gibt Claude Code (claude.ai/code) Kontext für die Arbeit in diesem Repository.

## Projektüberblick

**Folder Mover** ist eine self-hosted Flask-Webapp, die Downloads (von JDownloader o.ä.)
automatisch klassifiziert (Film/Serie) und strukturiert auf ein NAS verschiebt
(gemountet via SMB/NFS). Single-User-Tool, läuft als systemd-Service auf einem
Linux-Host mit Zugriff auf lokales Download-Verzeichnis + NAS-Mountpoints.

Kein Datenbank-Backend — Zustand lebt in: `config.yaml` (Konfiguration),
`history.jsonl` (Move-Log, append-only) und In-Memory-Globals in `app.py`/`mover.py`
(laufender Move-Fortschritt, da nur ein Vorgang gleichzeitig unterstützt wird).

## Architektur & Datenfluss

```
Browser (templates/index.html, ~1600+ Zeilen Vanilla JS, kein Build-Step)
   │  POST /api/scan   (startet nur einen Hintergrund-Thread, antwortet sofort)
   ▼
app.py: api_scan() → threading.Thread → scanner.py: scan_downloads(config, on_progress=...)
   │                        ├─ emit("init"/"library"/"scanning"/"done"/"error", current, found)
   │                        │   → app.py schreibt das in _scan_progress (mit _scan_lock)
   │                        ├─ SeriesLibrary: liest series_dir EINMAL pro Scan,
   │                        │   indexiert alle vorhandenen Serienordner
   │                        ├─ find_release_folder(): entfernt UUID-/Hoster-Wrapper
   │                        ├─ classify(): Regex-Erkennung S01E01 / S01 / 1x01 / Folge NN / Jahr+Qualität
   │                        ├─ library.match(): LCS-basiertes Fuzzy-Matching gegen NAS-Bibliothek
   │                        └─ _dir_size() / _find_movie_duplicate(): Walks über Quell- bzw. NAS-Verzeichnisse
   │
   ▼
GET /api/scan-progress (Server-Sent Events, Poll alle 0.15s)
   │   liefert {phase, current, found} laufend, am Ende {done: true, items: [...]}
   ▼
Browser zeigt Karten zur manuellen Bestätigung/Bearbeitung an
   │  POST /api/move  { moves: [...] }
   ▼
app.py startet Thread → mover.py: execute_moves()
   │   _copy_with_progress(): Chunked Copy (4 MB), fsync() periodisch (alle 64 MB + am Dateiende),
   │   danach Quelle löschen (os.unlink / shutil.rmtree)
   ▼
GET /api/progress (Server-Sent Events, Poll alle 0.25s) → Live-Fortschrittsbalken
   │
   ▼
history.py: append() schreibt Ergebnis nach history.jsonl (max. 500 Zeilen, älteste werden getrimmt)
```

**Wichtig:** `/api/scan` ist **asynchron** — der POST-Request startet nur einen Thread und
antwortet sofort mit `{"ok": true}`. Die eigentlichen Scan-Ergebnisse kommen ausschließlich
über den SSE-Stream `/api/scan-progress` (Event mit `done: true` enthält `items`). Wer an
`scan_downloads()` etwas ändert, **muss** den `on_progress`-Parameter erhalten — `app.py`
ruft ihn fest mit `scan_downloads(config, on_progress=on_progress)` auf. Ohne diesen
Parameter wirft der Call einen `TypeError`, der nur im Hintergrund-Thread landet: der
HTTP-Request selbst bleibt `200 OK`, aber `/api/scan-progress` hängt für immer (kein
`done`-Event kommt je an) — das Symptom ist ein endlos drehender Browser-Tab-Spinner
ohne sichtbaren Fehler im Vordergrund. Diese Kopplung schon einmal versehentlich gebrochen
worden (siehe Git-Historie) — beim Ändern von `scanner.py` immer gegen den tatsächlichen
`app.py`-Aufruf prüfen, nicht nur gegen die Funktionsdefinition isoliert.

### Module

| Datei | Verantwortung |
|---|---|
| `app.py` | Flask-Routen, Session-Auth, Move-Thread-Orchestrierung, SSE-Endpoint |
| `scanner.py` | Klassifikation, Wrapper-Erkennung, Fuzzy-Matching gegen NAS-Bibliothek, Scan-Resultate |
| `mover.py` | Tatsächliches Kopieren mit Fortschritt/Speed/ETA, Cleanup nach dem Move |
| `history.py` | Append-only JSONL-Log für abgeschlossene Moves |
| `config.py` | Lädt `config.yaml`, merged mit `DEFAULT_CONFIG` (flaches Deep-Merge auf Top-Level-Keys) |
| `templates/index.html` | Komplettes Frontend (HTML+CSS+JS in einer Datei), kein Framework, kein Build |
| `templates/login.html` | Login-Formular für Basic-Session-Auth |

### Auth-Modell

Einfache Session-basierte Anmeldung (`app.py: require_auth`), Credentials aus
`config.yaml: auth.username/password` im Klartext. Kein Passwort-Hashing, kein
Rate-Limiting auf `/login`. Bewusst minimal gehalten für LAN/VPN-Nutzung — **nicht
für Exposition ins offene Internet gedacht.**

## Bekannte Performance-Falle: "Webapp lädt sehr lange, kein Fehler im Log"

Das war (Stand jetzt teilweise behoben, siehe unten) kein Bug im Sinne eines Fehlers,
sondern **erwartetes Verhalten bei synchronen Filesystem-Operationen über
Netzwerk-Mounts (SMB/NFS)**, die im Code zunächst nicht als potenziell langsam
behandelt wurden:

1. **`/api/scan` konnte lange dauern, weil:**
   - `SeriesLibrary._load()` (`scanner.py`) alle Einträge in `series_dir` einmal
     iteriert — bei sehr vielen Serienordnern auf einem langsamen NAS-Mount messbar.
     *(noch unverändert — siehe "Offene Punkte" unten)*
   - `library.match()` für **jeden** Scan-Eintrag **alle** indexierten Serien
     mit LCS vergleicht (`O(n_items × n_series × 60²)` Zeichenvergleiche).
     *(noch unverändert)*
   - ~~`_dir_size()` für jeden gefundenen Release-Ordner per `os.walk` + `stat()`
     **jede einzelne Datei** durchläuft, nur um die Anzeigegröße zu ermitteln.~~
     **Behoben:** `_dir_size()` stat't jetzt nur noch Dateien mit einer
     konfigurierten Video-Extension (Extras wie `.nfo`/`.srt`/Samples werden
     ignoriert). Wenn kein Video gefunden wird, fällt sie auf einen reinen
     Top-Level-`iterdir()` zurück statt eines vollen rekursiven Walks. Das
     reduziert die Anzahl der `stat()`-Roundtrips pro Release spürbar.
   - `_find_movie_duplicate()` iteriert bei Filmen **alle Buchstaben-Unterordner**
     in `movies_dir` und darin **jeden Eintrag** mit Fuzzy-Match — ein voller
     NAS-Directory-Walk pro Scan-Treffer. *(noch unverändert — siehe unten)*

2. **`/api/move` war durch Design langsam, nicht durch einen Bug:**
   ~~`_copy_with_progress()` (`mover.py`) ruft nach **jedem einzelnen 4-MB-Chunk**
   `flush()` + `os.fsync()` auf.~~
   **Behoben:** `fsync()` wird jetzt nur noch periodisch ausgelöst
   (`FSYNC_EVERY_BYTES`, aktuell alle 64 MB) statt nach jedem 4-MB-Chunk, plus
   einmal am Dateiende, bevor Metadaten kopiert/die Quelle gelöscht wird. Das
   vermeidet hunderte unnötige Sync-Roundtrips pro Datei, bei nur geringfügig
   ungenauerer Live-Geschwindigkeitsanzeige (die jetzt alle ~64 MB statt alle
   4 MB neu verankert wird).

3. **Kein Server-Timeout, kein Heartbeat bei hängenden NAS-Calls:**
   Flask läuft mit `threaded=True`, aber ein einzelner Request (z. B. `/api/scan`)
   hat keine Zeitbegrenzung. Ein stockender Mount äußert sich also nicht als
   Fehler, sondern als unbegrenzt langer Hang. *(noch unverändert — siehe unten)*

### Offene Punkte (nicht in diesem Durchgang behoben)

- `library.match()` cachen/vorberechnen statt pro Scan-Item neu über alle Serien
  zu laufen (CPU-seitig, kein NAS-I/O, aber bei großen Bibliotheken relevant).
- `_find_movie_duplicate()`: Analog zu `SeriesLibrary` einen Movies-Index einmal
  pro Scan aufbauen statt pro Film-Treffer die komplette `movies_dir`-Struktur
  erneut zu walken.
- Für `/api/scan` ein explizites Timeout/Logging um die NAS-Zugriffe legen, damit
  ein hängender Mount sich wenigstens als Log-Zeile zeigt statt als stiller Hang.

## Git-Setup

Aktiver Branch ist **`main`**. Es existiert zusätzlich ein lokaler Branch `mistral`
mit einem alternativen, unfertigen Scanner-Refactoring (vereinfachte Klassifikation,
keine `SeriesLibrary`-Klasse) — der ist **nicht** in `main` gemerged und sollte nicht
versehentlich als Referenz für Änderungen an `main` verwendet werden, da die beiden
Branches inkompatible `scanner.py`-Versionen haben.

## Sicherheitsrelevante Stellen (beim Ändern besonders vorsichtig sein)

- `app.py: api_delete` und `api_delete_nas` — Pfad-Traversal-Schutz über
  `os.path.realpath(...).startswith(allowed_dir + os.sep)`. Jede Änderung an dieser
  Logik kann dazu führen, dass beliebige Pfade außerhalb der konfigurierten
  Verzeichnisse gelöscht werden können. Sorgfältig testen, insbesondere Edge-Cases
  wie Symlinks und Pfade ohne abschließenden Separator.
- Passwort liegt im Klartext in `config.yaml` (nicht versioniert, siehe `.gitignore`).
  Keine Hashing-Logik vorhanden — falls das geändert wird, `app.py: login()` und
  `config.py` gemeinsam anpassen.

## Konventionen in diesem Code

- Python 3.11+, Type Hints im neuen Stil (`str | None`, `dict[str, str]`).
- Kein Test-Framework vorhanden — Änderungen manuell gegen eine Beispielordnerstruktur
  prüfen (siehe README "Folder structure it handles" für typische Eingaben).
- Frontend ist **eine** Datei (`templates/index.html`) ohne Build-Step — Änderungen
  direkt im HTML/JS, kein separates Bundling.
- Konfiguration ist immer optional zu lesen über `config.get(key, default)` —
  `DEFAULT_CONFIG` in `config.py` ist die Quelle der Wahrheit für Defaults.
- Deutsche UI-Strings (z. B. "Scanne…", "Verschiebe Dateien…") — bei neuen
  UI-Texten konsistent auf Deutsch bleiben.

## Lokales Entwickeln/Testen

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.yaml.example config.yaml   # Pfade + Passwort anpassen
python app.py                        # läuft auf :8080 (oder konfiguriertem Port)
```

Logs im Dev-Betrieb laufen direkt auf stdout (siehe `logging.basicConfig` in `app.py`).
Im Produktivbetrieb via systemd: `journalctl -u folder-mover -f`.

## Deployment

`install.sh` kopiert die App nach `/opt/folder-mover`, legt ein venv an, installiert
`folder-mover.service` (systemd) und startet den Service. Bei Code-Änderungen am
Produktivsystem: Dateien erneut nach `/opt/folder-mover` kopieren und
`sudo systemctl restart folder-mover` ausführen — das Skript überschreibt
keine bestehende `config.yaml`.