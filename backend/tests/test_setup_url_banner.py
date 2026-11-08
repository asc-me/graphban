import pathlib

from app.main import setup_url_banner


def test_banner_prints_url_and_prompt(monkeypatch):
    monkeypatch.setenv("PUBLIC_WEB_URL", "http://localhost:9090")
    line = setup_url_banner()
    assert "http://localhost:9090" in line
    assert "open this to continue setup" in line


def test_banner_default_url(monkeypatch):
    monkeypatch.delenv("PUBLIC_WEB_URL", raising=False)
    assert "http://localhost:8080" in setup_url_banner()


def test_compose_passes_public_web_url():
    root = pathlib.Path(__file__).resolve().parents[2]
    assert "PUBLIC_WEB_URL" in (root / "docker-compose.yml").read_text()
