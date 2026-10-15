<p align="center">
  <img src="docs/banner.svg" alt="folder.mover" width="880">
</p>

<p align="center">
  <a href="https://github.com/simuuh/folder-mover/actions/workflows/ci.yml"><img src="https://github.com/simuuh/folder-mover/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/flask-3.x-000000?logo=flask&logoColor=white" alt="Flask">
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/simuuh/folder-mover?color=3ecf76" alt="License: MIT"></a>
  <br>
  <img src="https://img.shields.io/badge/self--hosted-✓-4f8ef7" alt="Self-hosted">
  <img src="https://img.shields.io/badge/no%20build%20step-vanilla%20JS-f59e0b" alt="Vanilla JS">
  <img src="https://img.shields.io/badge/theme-dark%20%7C%20light-252a35" alt="Dark and light theme">
  <a href="https://github.com/simuuh/folder-mover/commits/main"><img src="https://img.shields.io/github/last-commit/simuuh/folder-mover" alt="Last commit"></a>
</p>

<p align="center">
  <b>A tiny self-hosted web UI that turns a messy download folder into a clean movie & series library on your NAS —<br>without the complexity of the *arr stack.</b>
</p>

---

You download with JDownloader (or anything else) into a local folder. Folder Mover
scans it, peels away UUID and hoster wrapper folders, recognises movies, episodes and
season packs, matches them against the series that already exist on your NAS — and
moves everything into place after **you** confirmed it. With a live progress bar.

## ✨ Highlights

| | |
|---|---|
| 🧠 **Smart classification** | `S01E01`, `S01` season packs, `1x01`, `Folge 01`, movies by year + quality tags |
| 🪆 **Wrapper unwrapping** | UUID folders, hoster suffixes (`- rapidgator`), double-nested release folders — **also wrappers inside wrappers** and wrappers holding several releases |
| 📚 **Library matching** | Fuzzy-matches against existing NAS series folders, so `The.Boys.S05` lands in your existing `The Boys/` |
| 🔁 **Duplicate detection** | Finds an older release of the same movie and offers to replace it |
| ⏳ **Unfinished downloads** | Releases with `.part` / `.crdownload` files are flagged and deselected |
| 📈 **Live progress** | Chunked copy with speed, ETA and per-file progress via Server-Sent Events; reconnects after a page reload |
| 🛡️ **Safe by default** | Nothing moves without confirmation; free-space check; no half-copied files left behind; all paths validated server-side |
| 🗂️ **History** | Searchable log of every move, filterable by type and errors |
| 🌗 **Dark & light** | Follows your OS theme, switchable in the header |

## ⚙️ How it works

```mermaid
flowchart LR
    A[📥 Download folder] -->|scan| B[Unwrap wrappers]
    B --> C[Classify<br/>movie / series]
    C --> D[Match against<br/>NAS library]
    D --> E{👀 Review<br/>in the browser}
    E -->|confirm| F[Chunked copy<br/>+ fsync]
    F --> G[(🎬 Filme / 📺 Serien)]
    F --> H[🧹 Clean up source]
    F --> I[📝 history.jsonl]
```

### Folder structures it understands

```text
Downloads/
├── 6d5b82f9-…/                                       UUID wrapper → looked through
│   └── Movie.2022.German.1080p.WEB-Group/
│       └── movie.mkv
├── 0a1b2c3d-…/                                       wrapper inside a wrapper
│   └── Series.S05.German.1080p.WEB-Group - rapidgator/
│       └── Series.S05.German.1080p.WEB-Group/
│           ├── series.s05e01.mkv
│           └── series.s05e02.mkv
├── 9f8e7d6c-…/                                       one wrapper, two releases → two entries
│   ├── Movie.A.2020.German.1080p.WEB-Group/
│   └── Movie.B.2021.German.1080p.WEB-Group/
└── The.Sheriff.2026.German.1080p.WEB-SiXTYNiNE/
    └── The.Sheriff.2026.German.1080p.WEB-SiXTYNiNE.mkv
```

Result on the NAS:

```text
/mnt/Filme/M/Movie.2022.German.1080p.WEB-Group/
/mnt/Filme/T/The.Sheriff.2026.German.1080p.WEB-SiXTYNiNE/
/mnt/Serien/Series Name/S05/series.s05e01.mkv
```

<details>
<summary><b>Wrapper rules in detail</b></summary>

A top-level entry is resolved recursively (up to 6 levels) into **zero or more releases**:

1. A folder that directly contains video files is a release — unless it is a UUID /
   configured wrapper, in which case each video file becomes its own entry.
2. A folder with exactly one sub-folder is looked through if it is a wrapper
   (UUID, short generic name, `wrapper_patterns`), if the child is a UUID, or if the
   names match (`Release - hoster` › `Release`).
3. A wrapper with several sub-folders is split into one entry per sub-folder that
   contains videos. A real release with sub-folders (season pack with per-episode
   folders, `Sample/`, `Subs/`) is kept whole.

When a wrapper holds several releases, deleting or moving one of them never touches
its siblings.
</details>

## 🚀 Quick start

**Requirements:** Linux with systemd, Python 3.11+, NAS mounted as a local path (SMB/NFS).

```bash
git clone https://github.com/simuuh/folder-mover.git
cd folder-mover

cp config.yaml.example config.yaml
nano config.yaml                    # paths, user, password

sudo bash install.sh                # or: sudo SERVICE_USER=media bash install.sh
```

The service runs on `http://<host>:8080` (gunicorn, one worker) and starts on boot.

```bash
journalctl -u folder-mover -f       # logs
sudo systemctl restart folder-mover # after config changes
```

### Set a hashed password

```bash
/opt/folder-mover/venv/bin/python /opt/folder-mover/app.py --hash-password
```

Paste the output into `auth.password_hash` and remove the plaintext `password`.

## 🔧 Configuration

All keys are optional; defaults live in [`config.py`](config.py).

| Key | Default | Description |
|---|---|---|
| `download_dir` | `/home/user/Downloads` | Folder that gets scanned |
| `movies_dir` | `/mnt/Filme` | Movies end up in `movies_dir/<Letter>/<Release>` |
| `series_dir` | `/mnt/Serien` | Episodes end up in `series_dir/<Series>/Sxx/` |
| `host` / `port` | `0.0.0.0` / `8080` | Listen address |
| `secret_key` | random per start | Stable session secret — set it to stay logged in across restarts |
| `auth.username` | `admin` | Login name |
| `auth.password_hash` | – | Hash from `app.py --hash-password` (preferred) |
| `auth.password` | `changeme` | Plaintext fallback if no hash is set |
| `video_extensions` | `.mkv .mp4 .avi .m4v .ts .m2ts` | What counts as a video |
| `wrapper_patterns` | `[]` | Extra regexes for container folders that are never a release |

Environment overrides: `FOLDER_MOVER_CONFIG` (path to config file), `FOLDER_MOVER_HISTORY` (path to history file).

## 🔒 Security

Folder Mover is built for a **single user on a LAN or VPN** — do not expose it to the
internet. It validates every path server-side, rate-limits logins and sends strict
security headers. See [SECURITY.md](SECURITY.md) for details and how to report issues.

## 🧑‍💻 Development

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp config.yaml.example config.yaml   # point the paths at test folders
python app.py                        # dev server on :8080

pytest                               # tests
ruff check .                         # lint
```

The frontend is plain HTML/CSS/JS in [`templates/`](templates) and [`static/`](static) — no build step.
The UI language is German.

## 🗺️ Roadmap

- [ ] Docker image & compose example
- [ ] Jellyfin / Plex library refresh after a move
- [ ] Notifications (ntfy, Gotify, Telegram) when downloads are waiting or a move finished
- [ ] Auto-scan / watch mode for the download folder
- [ ] Cancel a running move
- [ ] TMDB lookup for clean `Title (Year)` folder names
- [ ] Optional episode renaming & folder-per-movie for loose files

## 📄 License

[MIT](LICENSE) © Simon Muster
