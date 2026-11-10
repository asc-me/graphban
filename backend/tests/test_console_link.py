"""GRPH-1453 / GRPH-1455: link help and the web image name the console."""
import contextlib
import io
from pathlib import Path

CONSOLE = "https://graphban-console-console-production-ascme-labs.unkey.app"


def test_link_help_and_the_image_build_name_the_console():
    from app.cli import build_parser

    root = Path(__file__).resolve().parents[2]
    docker = (root / "web" / "Dockerfile").read_text()
    assert f"ARG VITE_CONSOLE_URL={CONSOLE}" in docker
    assert "ENV VITE_CONSOLE_URL=$VITE_CONSOLE_URL" in docker
    cli = (root / "backend" / "app" / "cli.py").read_text()
    assert CONSOLE in cli
    assert "cloud.example" not in cli
    routes = (root / "web" / "src" / "lib" / "routes.ts").read_text()
    assert CONSOLE in routes
    deploy = (root / "docs" / "deploy.md").read_text()
    assert f"--cloud-url {CONSOLE}" in deploy
    assert "cloud.example" not in deploy
    config = (root / "backend" / "app" / "config.py").read_text()
    assert "cloud.example" not in config
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with contextlib.suppress(SystemExit):
            build_parser().parse_args(["link", "-h"])
    text = buf.getvalue().replace("\n", "")
    assert CONSOLE in text
    assert "console" in text.lower()
