# Folder Mover

A self-hosted web UI for organizing media downloads into a structured NAS library — without the complexity of \*arr software.

Built for a setup where JDownloader saves files to a local directory, and the NAS is mounted via SMB/NFS.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Flask](https://img.shields.io/badge/flask-3.x-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Smart classification** — detects movies and series (including season packs) from release names
- **NAS library matching** — fuzzy-matches against existing series folders so `The.Boys.S05` lands in your existing `The.Boys/` directory
- **Wrapper detection** — strips UUID folders, hoster-suffix wrappers (`-rapidgator`, `-filecrypt`), and double-nested release folders
- **Live progress** — real copy with per-file progress bar, speed (MB/s), and ETA via Server-Sent Events
- **Move history** — searchable log of all completed moves with filter by type/status
- **Manual review** — nothing moves without your confirmation; every suggestion is editable
- **Basic Auth** — simple password protection for LAN/VPN access

## Folder structure it handles

```
Downloads/
├── 6d5b82f9-.../                          # UUID wrapper → ignored
│   └── Movie.2022.German.1080p.WEB-Group/
│       └── movie.mkv
├── Series.S05.German.1080p.WEB-Group - rapidgator/   # hoster suffix → ignored
│   ├── series.s05e01.mkv
│   └── series.s05e02.mkv
└── The.Sheriff.2026.German.1080p.WEB-SiXTYNiNE/
    └── The.Sheriff.2026.German.1080p.WEB-SiXTYNiNE.mkv
```

Result on NAS:

```
/mnt/Filme/S/The.Sheriff.2026.German.1080p.WEB-SiXTYNiNE/
/mnt/Serien/Series Name/S05/series.s05e01.mkv
```

## Requirements

- Python 3.11+
- Ubuntu/Debian (or any Linux with systemd)
- NAS mounted as local path (SMB/NFS)

## Installation

```bash
# 1. Clone
git clone https://github.com/youruser/folder-mover.git
cd folder-mover

# 2. Configure
cp config.yaml.example config.yaml
nano config.yaml   # set your paths and password

# 3. Install (as root)
sudo bash install.sh
```

The service starts on `http://<ip>:8080` and runs automatically on boot.

```bash
# Logs
journalctl -u folder-mover -f

# Restart after config change
sudo systemctl restart folder-mover
```

## Configuration

```yaml
download_dir: /home/user/Downloads
movies_dir:   /mnt/Filme
series_dir:   /mnt/Serien

port: 8080

auth:
  username: admin
  password: yourpassword   # change this

video_extensions:
  - .mkv
  - .mp4
  - .avi
  - .m4v
  - .ts
```

## How it works

1. **Scan** — walks the download directory, detects release folders, strips wrappers
2. **Classify** — regex-based detection of `S01E01`, `S01` season packs, `1x01`, `Folge 01`, movie year+quality patterns
3. **Match** — fuzzy LCS-based matching against existing NAS series folders (≥80% score = auto-match)
4. **Review** — web UI shows suggestions with confidence indicators; all fields editable
5. **Move** — chunked copy with `fsync` (accurate speed on NFS/SMB), source cleanup after move
6. **Log** — every move written to `history.jsonl`

## License

MIT
