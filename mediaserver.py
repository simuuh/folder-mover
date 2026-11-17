"""
Tell Jellyfin and/or Plex to rescan their libraries after a move.

A full library refresh instead of a path-based one on purpose: Folder Mover
often sees the media under different paths than the media server (/movies in
this container, /data/movies in the Jellyfin container), so a targeted path
would silently match nothing. Both servers skip unchanged files quickly.

Best effort: runs in a background thread, never fails or delays a move;
problems end up in the log only. Uses urllib — no extra dependency.
"""

import logging
import threading
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


def _request(url: str, method: str, headers: dict[str, str]) -> int:
    req = urllib.request.Request(url, method=method, headers=headers, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:   # URL only ever from the config
        return resp.status


def _targets(config: dict) -> list[tuple[str, str, str, dict[str, str]]]:
    """(name, method, url, headers) of every configured media server."""
    targets = []
    jellyfin = config.get("jellyfin") or {}
    if jellyfin.get("url") and jellyfin.get("api_key"):
        targets.append(("Jellyfin", "POST", jellyfin["url"].rstrip("/") + "/Library/Refresh",
                        {"Authorization": f'MediaBrowser Token="{jellyfin["api_key"]}"'}))
    plex = config.get("plex") or {}
    if plex.get("url") and plex.get("token"):
        # Token as header, not in the query string → never ends up in proxy/access logs
        targets.append(("Plex", "GET", plex["url"].rstrip("/") + "/library/sections/all/refresh",
                        {"X-Plex-Token": plex["token"], "Accept": "application/json"}))
    return targets


def configured(config: dict) -> list[str]:
    """Names of the media servers that will be refreshed."""
    return [name for name, *_ in _targets(config)]


def refresh(config: dict) -> dict[str, str | None]:
    """Trigger a library refresh on every configured server. {name: error or None}."""
    outcome: dict[str, str | None] = {}
    for name, method, url, headers in _targets(config):
        try:
            status = _request(url, method, headers)
            log.info("%s library refresh triggered (HTTP %s)", name, status)
            outcome[name] = None
        except urllib.error.HTTPError as e:
            hint = " — check the API key/token" if e.code in (401, 403) else ""
            log.warning("%s library refresh failed: HTTP %s%s", name, e.code, hint)
            outcome[name] = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            log.warning("%s library refresh failed: %s (%s)", name, reason, url)
            outcome[name] = str(reason)
    return outcome


def refresh_in_background(config: dict) -> threading.Thread | None:
    """refresh() in a daemon thread, so a slow server never holds up the move."""
    if not _targets(config):
        return None
    thread = threading.Thread(target=refresh, args=(config,), daemon=True, name="mediaserver-refresh")
    thread.start()
    return thread
