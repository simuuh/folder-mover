"""Guards for the deployment files (Dockerfile, install.sh, entrypoint)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_every_app_module_is_installed_and_in_the_image():
    # CLAUDE.md: new Python files must be added to install.sh AND the Dockerfile
    modules = {p.name for p in ROOT.glob("*.py")}
    copy = " ".join(re.findall(r"^COPY (.+?) \./$", _read("Dockerfile"), re.M))
    cp = re.search(r"cp -r (.+?)\"\$APP_DIR/\"", _read("install.sh"), re.S).group(1)
    for module in modules:
        assert module in copy.split(), f"{module} missing in Dockerfile COPY"
        assert module in cp.split(), f"{module} missing in install.sh cp"
    for folder in ("templates", "static", "translations"):
        assert f"COPY {folder} {folder}" in _read("Dockerfile")
        assert folder in cp.split()


def test_entrypoint_does_not_follow_symlinks_when_chowning():
    chown = [line for line in _read("docker-entrypoint.sh").splitlines() if "chown" in line and "find" in line]
    assert chown and all("chown -h" in line for line in chown)


def test_healthcheck_follows_the_configured_port():
    assert "FOLDER_MOVER_PORT" in _read("Dockerfile").split("HEALTHCHECK", 1)[1]


def test_install_uses_the_primary_group_of_the_user():
    script = _read("install.sh")
    assert 'id -gn "$SERVICE_USER"' in script and ":$SERVICE_USER\"" not in script
