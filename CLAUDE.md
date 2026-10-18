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
Browser (templates/index.html + static/theme.css, Vanilla JS, kein Build-Step)
   │  POST /api/scan   (startet nur einen Hintergrund-Thread, antwortet sofort)
   ▼
app.py: api_scan() → threading.Thread → scanner.py: scan_downloads(config, on_progress=...)
   │                        ├─ emit("init"/"library"/"scanning"/"done"/"error", current, found)
   │                        │   → app.py schreibt das in _scan_progress (mit _scan_lock)
   │                        ├─ SeriesLibrary: liest series_dir EINMAL pro Scan,
   │                        │   indexiert alle vorhandenen Serienordner
   │                        ├─ find_releases(): löst Wrapper rekursiv auf (auch Wrapper im
   │                        │   Wrapper), liefert 0..n Releases (Ordner ODER lose Videodatei)
   │                        ├─ classify(): Regex-Erkennung S01E01 / S01 / 1x01 / Folge NN / Jahr+Qualität;
   │                        │   "unknown"-Ordner werden über ihre Videonamen nachklassifiziert
   │                        ├─ library.match(): normalisierter Vergleich gegen NAS-Bibliothek
   │                        │   (automatisch nur bei identischem Schlüssel, sonst nur Hinweis)
   │                        └─ _dir_stats() / MovieIndex.find_duplicate(): Grösse + .part-Erkennung bzw.
   │                            NAS-Duplikate (Titel + Jahr, auch "Title (Year)"-Ordner)
   │
   ▼
GET /api/scan-progress (Server-Sent Events, Poll alle 0.15s)
   │   liefert {phase, current, found} laufend, am Ende {done: true, items, error, known_series}
   ▼
Browser zeigt Karten zur manuellen Bestätigung/Bearbeitung an
   │  POST /api/move  { moves: [...] }
   ▼
app.py validiert JEDEN Move serverseitig (_validate_move, inkl. replaced_path) und prüft
   │   mount_marker → Thread → mover.py: execute_moves()
   │   _copy_with_progress(): Chunked Copy (4 MB) nach "<ziel>.fmpart", fsync() periodisch
   │   (alle 64 MB + am Dateiende), erst dann os.replace auf den finalen Namen. Film-Ordner
   │   entstehen komplett in "<ziel>.fmpart/" und werden am Ende umbenannt.
   │   Vorher Snapshot (Größe+mtime) der Quelle: ändert sie sich während des Kopierens →
   │   Rollback. Danach werden NUR die kopierten Dateien gelöscht, leere Ordner per rmdir.
   │   Filme: Dateien/Ordner nach `junk_patterns` (config.py: is_junk) werden nicht kopiert,
   │   nur mit der Quelle gelöscht.
   │   Serien: nur Videos (ohne Samples) + passende Untertitel; Rest wird aufgeräumt.
   │   Erst nach Erfolg: replaced_path (altes NAS-Release bei "Ersetzen") löschen.
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
| `config.py` | Lädt `config.yaml`, merged mit `DEFAULT_CONFIG` (flaches Deep-Merge auf Top-Level-Keys); `video_extensions()`-Helper |
| `gunicorn.conf.py` | Produktions-Server-Settings (liest host/port aus der Config) |
| `templates/index.html` | Frontend (HTML+CSS+JS in einer Datei), kein Framework, kein Build |
| `templates/login.html` | Login-Formular |
| `i18n.py` + `translations/*.json` | Übersetzungen (DE = Quelle, EN), Sprachwahl |
| `static/theme.css` | Gemeinsame Design-Tokens Dark/Light für beide Templates |
| `tests/` | pytest-Suite (Scanner, Mover, API inkl. Pfad-Validierung, i18n, Deployment-Dateien) |

### Auth-Modell

Session-basierte Anmeldung (`app.py: require_auth`). Credentials aus
`config.yaml: auth.username` + `auth.password_hash` (Werkzeug-Hash, erzeugt mit
`python app.py --hash-password`) oder als Fallback `auth.password` im Klartext
(Vergleich via `hmac.compare_digest`). Ohne Hash und mit leerem oder Default-Passwort
(`changeme`) ist der Login gesperrt (`password_configured()`). Die Session
(`start_session()`) enthält Login-Zeit, zufällige `sid` und einen Fingerprint von
Benutzer+Passwort: ungültig nach `session_days`, nach Logout (`sid` gesperrt, nur
In-Memory) und nach Passwortwechsel. In Tests Sessions immer über
`app_module.start_session(sess)` anlegen, nicht nur `logged_in=True`.
In-Memory-Lockout nach 5 Fehlversuchen pro IP (hinter Reverse-Proxy `proxy_count`
setzen → `ProxyFix`). Cookie `SameSite=Lax`/`HttpOnly`, Security-Header inkl. CSP
in `after_request`. Gedacht für LAN/VPN — **nicht für Exposition ins offene Internet.**

## NAS-Zugriffe und Performance

Alle Filesystem-Operationen sind synchron und laufen über Netzwerk-Mounts
(SMB/NFS) — jeder `stat()`/`iterdir()` ist ein Roundtrip. Stand heute:

- `SeriesLibrary` und `MovieIndex` lesen `series_dir` bzw. `movies_dir` **einmal
  pro Scan** (MovieIndex lazy beim ersten Film, Buchstaben-Ordner plus flache
  Film-Ordner). `library.match()` cacht pro normalisiertem Schlüssel.
- `_dir_stats()` stat't nur Videodateien (Fallback: Top-Level-`iterdir()`).
- `_copy_with_progress()` macht `fsync()` nur alle `FSYNC_EVERY_BYTES` (64 MB) und
  am Dateiende.
- Leere oder fehlende Medienordner (typisch: NAS nicht gemountet) meldet der Scan
  als Warnung (`config.library_warnings()`); mit `mount_marker` werden Moves dann
  verweigert.

**Offen:** Kein Timeout um NAS-Zugriffe — ein hängender Mount zeigt sich als
endlos laufender Scan/Move ohne Log-Zeile.

## i18n (DE/EN)

- `i18n.py` lädt `translations/*.json` (flach: `"bereich.name": "Text"`).
  Platzhalter `{name}`; bei `count == 1` wird `<key>_one` genommen, falls vorhanden.
  Fehlende/leere Einträge fallen auf Deutsch zurück.
- Sprache pro Request: Cookie `fm-lang` (Umschalter im Header) → Config
  `language` → `Accept-Language` (Haupt-Tag) → `de`.
- **Jinja:** `{{ t('key', name=...) }}`. **JS:** `t('key', {name})`, für HTML mit
  hervorgehobenen Werten `tStrong()`. Der Katalog wird als `I18N` in die Seite
  eingebettet (kein Extra-Request). Keine dynamisch zusammengesetzten Schlüssel
  (`'phase_' + x`) — der Test findet sie sonst nicht; stattdessen Maps.
- **Server-Fehler fürs UI:** API-Antworten über `api_error("api.xyz", status)`
  (übersetzt + stabiler `error_key`). Mover-Fehler über `_err(key, english_text,
  **args)` → Ergebnis enthält `error` (Englisch, für Logs) und `error_i18n`
  (`[{key, args}]`), wird so auch in `history.jsonl` gespeichert und im Browser
  per `errorText()` in der aktuellen Sprache angezeigt. Scanner-Statusmeldungen
  laufen als `current_key`/`current_args` im Progress-Event.
- `tests/test_i18n.py` prüft: alle im Code verwendeten Schlüssel existieren in
  `de.json`, alle Kataloge haben dieselben Schlüssel und Platzhalter.

## Git-Workflow (verbindlich)

**Nie direkt auf `main` committen.** `main` ist immer lauffähig/deploybar. Jede
Änderung entsteht auf einem eigenen Branch, der vom aktuellen `main` abzweigt:

| Präfix | Wofür | Beispiel |
|---|---|---|
| `feature/` | Neue Funktion, sichtbares neues Verhalten | `feature/jellyfin-refresh` |
| `fix/` | Fehlerbehebung | `fix/history-counter` |
| `chore/` | Alles ohne Verhaltensänderung: Doku, CI, Dependencies, Refactoring, Release | `chore/ci-minimal` |

- Branch-Namen kurz, kleingeschrieben, mit Bindestrichen.
- Commit-Messages mit passendem Präfix: `feat: …`, `fix: …`, `chore: …`
  (Beschreibung auf Deutsch ist ok).
- Ein Branch = ein Thema. Mehrere unabhängige Themen → mehrere Branches.
- **Mehrere Themen nacheinander, nicht parallel:** den nächsten Branch erst
  anlegen, wenn der vorige in `main` gemergt ist (immer vom aktuellen `main`).
  `mover.py`, `scanner.py` und `translations/*.json` werden von fast jedem Thema
  angefasst — parallele Branches vom selben alten `main` erzeugen dort Konflikte.
  Läuft doch einmal ein Branch parallel: vor dem Merge `git rebase main`.
- Neue i18n-Schlüssel thematisch einsortieren (neben verwandte Schlüssel), nicht
  pauschal ans Dateiende — das hält Diffs klein und konfliktfrei.
- Vor dem Merge: `ruff check .` und `pytest` müssen grün sein.
- Merge nach `main` per Pull Request oder lokal mit `git merge --no-ff <branch>`,
  danach den Branch löschen.
- Für Claude: zu Beginn jeder Aufgabe `git switch main && git pull` und dann
  `git switch -c <präfix>/<name>`. Pushen und PRs öffnen nur, wenn der User es will.

### Releases

1. Branch `chore/release-x.y.z`: Version in `app.py` (`__version__`) **und**
   `pyproject.toml` hochzählen (SemVer: `feature` → Minor, nur `fix` → Patch).
2. Nach `main` mergen, dann `git tag vx.y.z && git push origin vx.y.z`.
3. Der Tag startet `.github/workflows/release.yml` → Docker-Image auf
   `ghcr.io/simuuh/folder-mover` (`x.y.z`, `x.y`, `latest`) + GitHub-Release.

CI (`.github/workflows/ci.yml`) ist bewusst minimal (ein Job, Python 3.11, nur
push auf `main` + PRs, keine Doku-only-Läufe) — beim Erweitern auf das
GitHub-Free-Kontingent achten.

### Sonstiges

Es existiert zusätzlich ein lokaler Branch `mistral`
mit einem alternativen, unfertigen Scanner-Refactoring (vereinfachte Klassifikation,
keine `SeriesLibrary`-Klasse) — der ist **nicht** in `main` gemerged und sollte nicht
versehentlich als Referenz für Änderungen an `main` verwendet werden, da die beiden
Branches inkompatible `scanner.py`-Versionen haben.

## Sicherheitsrelevante Stellen (beim Ändern besonders vorsichtig sein)

- `app.py: _is_within()` ist der zentrale Pfad-Traversal-Schutz (realpath +
  `os.path.commonpath`). Benutzt von `api_delete`, `api_delete_nas` und
  `_validate_move`. Das Root-Verzeichnis selbst ist **nie** erlaubt (sonst könnte
  z. B. das ganze `download_dir` gelöscht werden). Änderungen immer mit
  `tests/test_app.py` absichern.
- `/api/move` bekommt Quell-/Zielpfade vom Client — `_validate_move()` darf nicht
  umgangen werden (Quelle in `download_dir`, Ziel in `movies_dir`/`series_dir`,
  `video_files` ohne `..`, `replaced_path` in den Medienordnern und weder Ziel
  noch im Ziel).
- `require_sane_dirs`: Überschneiden sich `download_dir`/`movies_dir`/`series_dir`,
  sind Scan, Move und Löschen gesperrt (409). `require_no_move`: kein Löschen
  während eines Moves.
- Der Mover löscht nach einem Move nur, was er selbst kopiert hat (Snapshot),
  nie per `rmtree` den ganzen Quellordner. Daran nichts ändern, ohne
  `tests/test_mover.py` (`test_files_appearing_*`) zu erweitern.
- Frontend: Dateinamen stammen aus Downloads und sind **nicht vertrauenswürdig**.
  Niemals in `onclick`/Attribute interpolieren — Karten werden über den numerischen
  Key `item._k` + Event-Delegation (`data-action`) angesprochen, Text immer über
  `escHtml()` bzw. `textContent`.
- Passwort/Hash in `config.yaml` (nicht versioniert, siehe `.gitignore`).

## Konventionen in diesem Code

- Python 3.11+, Type Hints im neuen Stil (`str | None`, `dict[str, str]`).
- Tests: `pytest` (Ordnerstrukturen werden in `tmp_path` erzeugt, Helper
  `make_files()` in `tests/conftest.py`). Lint: `ruff check .`. Beides läuft in CI
  (`.github/workflows/ci.yml`). Neue Wrapper-Fälle immer als Test in
  `tests/test_scanner.py` ergänzen.
- Frontend ist `templates/index.html` (+ `static/theme.css` für Farben) ohne
  Build-Step. Farben nur über CSS-Variablen, damit Dark/Light funktionieren.
- Konfiguration ist immer optional zu lesen über `config.get(key, default)` —
  `DEFAULT_CONFIG` in `config.py` ist die Quelle der Wahrheit für Defaults.
- **Keine UI-Texte im Code.** Jeder sichtbare Text ist ein Schlüssel in
  `translations/de.json` (Quelle, Deutsch) und `translations/en.json` (gleiche
  Schlüssel; der User übersetzt selbst — neue Schlüssel dort zunächst mit dem
  deutschen Text anlegen). Siehe Abschnitt „i18n“.

## Lokales Entwickeln/Testen

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
cp config.yaml.example config.yaml   # Pfade + Passwort anpassen
python app.py                        # läuft auf :8080 (oder konfiguriertem Port)
```

Logs im Dev-Betrieb laufen direkt auf stdout (siehe `logging.basicConfig` in `app.py`).
Im Produktivbetrieb via systemd: `journalctl -u folder-mover -f`.

## Deployment

`install.sh` kopiert die App nach `/opt/folder-mover`, legt ein venv an, installiert
`folder-mover.service` (systemd, gunicorn via `gunicorn.conf.py`) und startet den
Service. **Es muss genau EIN gunicorn-Worker laufen** — Scan-/Move-Zustand liegt im
Prozessspeicher; mehrere Worker würden sich gegenseitig nicht sehen. Neue
Python-Dateien/Ordner müssen in der `cp`-Zeile von `install.sh` **und** in den
`COPY`-Zeilen des `Dockerfile` ergänzt werden.

### Docker

- `Dockerfile` (python:3.12-slim, gunicorn) + Beispiel `docker-compose.yml`.
  `docker-entrypoint.sh` startet als root, setzt **nur** `/config` (+ Dateien
  darin) auf `PUID:PGID` und wechselt per `setpriv` auf diesen User. Grund:
  Docker legt einen fehlenden Bind-Mount-Ordner als root an → vorher
  „Permission denied“ auf `history.jsonl`/`.secret_key`. Niemals rekursiv über
  `/downloads`/`/movies`/`/series` chownen. Pfade im Container fix: `/downloads`, `/movies`,
  `/series`, `/config` (gesetzt über `FOLDER_MOVER_*`-Env-Variablen, die laut
  `config.py: ENV_OVERRIDES` Vorrang vor `config.yaml` haben).
- Nutzer-Einstellungen gehören in `.env` (Vorlage `.env.example`, Git- und
  Docker-ignoriert); `docker-compose.yml` enthält nur `${…}`-Platzhalter und
  `env_file: .env`. Neue Docker-Optionen immer in `.env.example` dokumentieren.
  Passwort-Hash in `.env` in **einfachen** Anführungszeichen (enthält `$`).
- Session-Secret: ohne `secret_key` erzeugt `app.py: _load_secret_key()` einmalig
  `.secret_key` neben `history.jsonl` (im Container also in `/config`).
- `mover.py: _try_rename()` verschiebt per `rename()`, wenn Quelle und Ziel auf
  demselben Dateisystem liegen; bei `EXDEV` (z. B. zwei getrennte Bind-Mounts)
  wird normal kopiert. In Tests ist der Rename-Pfad per autouse-Fixture
  abgeschaltet, außer bei `@pytest.mark.rename`.
- Lokal testen: `docker build -t folder-mover:test .` und mit gemounteten
  Testordnern starten. Bei Code-Änderungen am
Produktivsystem: Dateien erneut nach `/opt/folder-mover` kopieren und
`sudo systemctl restart folder-mover` ausführen — das Skript überschreibt
keine bestehende `config.yaml`.