# Security Policy

## Intended deployment

Folder Mover is a **single-user tool for your LAN or VPN**. It can delete and
move files inside the configured download and media directories, so treat
access to it like shell access to those folders.

- Do **not** expose it directly to the internet. If you need remote access, use
  a VPN (WireGuard, Tailscale, …) or a reverse proxy with its own
  authentication and TLS.
- Use `auth.password_hash` (`python app.py --hash-password`) instead of a
  plaintext password, and set a stable `secret_key`.
- Run the service as an unprivileged user that only has write access to the
  download directory and the media mounts.

## Built-in protections

- Session login with constant-time comparison, optional password hashing and
  a simple lockout after repeated failed logins (behind a reverse proxy set
  `proxy_count`, otherwise all clients share the proxy's IP)
- Login is disabled until a password (hash) is set; the old default is refused
- Sessions expire after `session_days` and become invalid on logout or when
  the username/password changes
- Every move/delete request is validated server-side: sources must lie inside
  `download_dir`, targets inside `movies_dir` / `series_dir` (symlinks and
  `..` are resolved first)
- `SameSite=Lax`, `HttpOnly` session cookie, CSP and anti-framing headers
- File names from downloads are never interpolated into HTML attributes or
  inline JavaScript

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's
[private vulnerability reporting](https://github.com/simuuh/folder-mover/security/advisories/new)
instead. You can expect a first response within a week.
