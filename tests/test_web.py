"""Tests de la web cifrada (sin red: la publicación se prueba contra un repositorio git local)."""

from __future__ import annotations

import json
import subprocess

import pytest

from garmin_coach import queries, web
from garmin_coach.api import Caller
from garmin_coach.sync import Syncer

from .conftest import TODAY, FakeGarmin

PW = "contraseña-de-prueba"


@pytest.fixture
def synced(conn, activities, monkeypatch):
    monkeypatch.setattr(queries, "_today", lambda: TODAY)
    monkeypatch.setenv("RACE_DATE", "2027-02-14")
    monkeypatch.setenv("RACE_NAME", "Media de prueba")
    monkeypatch.setenv("RACE_TARGET", "1:59:59")
    Syncer(FakeGarmin(activities), conn, caller=Caller(delay=0, sleep=lambda s: None),
           progress=lambda m: None, today=TODAY).run()
    return conn


def _envelope(html: str) -> dict:
    return json.loads(html.split('<script id="payload" type="application/json">')[1].split("</script>")[0])


def test_payload_contents(synced):
    p = web.build_payload(synced, TODAY)
    assert p["race"]["days_left"] == 137
    assert p["race"]["target_pace_s_km"] == pytest.approx(341.2, abs=0.1)
    assert p["race"]["pred_s"] == 7550
    assert len(p["health"]["days"]) == web.HEALTH_DAYS and p["health"]["days"][-1] == "2026-09-30"
    assert len(p["weeks"]) == web.WEEKS
    assert len(p["activities"]) == 20
    gym = next(a for a in p["activities"] if a["type"] == "strength_training")
    assert gym["sets"][0] == ["SQUAT", 10, 60.0]
    run = next(a for a in p["activities"] if a["type"] == "running")
    assert len(run["laps"]) == 2 and len(run["zones"]) == 5
    # Nada de coordenadas GPS en lo que se publica.
    assert "latitude" not in json.dumps(p).lower()


def test_months_are_continuous():
    rows = [{"month": "2025-11", "run_km": 10}, {"month": "2026-02", "run_km": 5}]
    assert [m["month"] for m in web._fill_months(rows)] == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_site_is_encrypted_and_roundtrips(synced, tmp_path):
    out = web.build_site(tmp_path / "site", password=PW, conn=synced, today=TODAY)
    html = (out / "index.html").read_text()
    assert "Act 1000" not in html and "SQUAT" not in html and "Media de prueba" not in html
    env = _envelope(html)
    assert env["iter"] == 600_000 and env["kdf"] == "PBKDF2-SHA256"
    data = web.decrypt_payload(env, PW)
    assert data["race"]["name"] == "Media de prueba"
    with pytest.raises(Exception):
        web.decrypt_payload(env, "otra-contraseña")
    for f in ("robots.txt", ".nojekyll", "manifest.webmanifest", "icon.svg"):
        assert (out / f).exists()


def test_salt_is_stable_between_builds(synced, tmp_path):
    a = _envelope(web.build_site(tmp_path / "a", password=PW, conn=synced, today=TODAY).joinpath("index.html").read_text())
    b = _envelope(web.build_site(tmp_path / "b", password=PW, conn=synced, today=TODAY).joinpath("index.html").read_text())
    assert a["salt"] == b["salt"] and a["iv"] != b["iv"]


def test_password_required_and_minimum_length(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    with pytest.raises(web.WebError, match="web-password"):
        web.web_password()
    monkeypatch.setenv("WEB_PASSWORD", "corta")
    with pytest.raises(web.WebError, match="al menos"):
        web.web_password()


def test_set_web_password_replaces_line(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GARMIN_EMAIL=a@b.c\nWEB_PASSWORD=vieja\n")
    web.set_web_password("nueva-clave-larga", env)
    assert env.read_text() == "GARMIN_EMAIL=a@b.c\nWEB_PASSWORD=nueva-clave-larga\n"
    assert oct(env.stat().st_mode)[-3:] == "600"


@pytest.mark.parametrize("remote,url", [
    ("https://github.com/ana/garmin-coach.git", "https://ana.github.io/garmin-coach/"),
    ("git@github.com:ana/garmin-coach.git", "https://ana.github.io/garmin-coach/"),
    ("https://github.com/ana/ana.github.io", "https://ana.github.io/"),
    ("https://gitlab.com/ana/x.git", None),
])
def test_pages_url(remote, url):
    assert web.pages_url(remote) == url


def test_publish_pushes_single_commit_to_gh_pages(synced, tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    site = web.build_site(tmp_path / "site", password=PW, conn=synced, today=TODAY)
    web.publish_site(site, remote=str(bare))
    web.publish_site(site, remote=str(bare))  # segunda publicación: reemplaza, no acumula historial
    log = subprocess.run(["git", "--git-dir", str(bare), "log", "--oneline", "gh-pages"],
                         capture_output=True, text=True, check=True).stdout.strip().splitlines()
    assert len(log) == 1
    files = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "--name-only", "gh-pages"],
                           capture_output=True, text=True, check=True).stdout.split()
    assert {"index.html", ".nojekyll", "robots.txt"} <= set(files)


def test_publish_without_remote_explains(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "origin_url", lambda root=None: None)  # nunca tocar el repositorio real
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("x")
    with pytest.raises(web.WebError, match="GitHub"):
        web.publish_site(tmp_path / "site", remote="")


def test_schedule_plist_runs_daily_command():
    from garmin_coach import schedule

    d = schedule.plist_dict(7, 30)
    assert d["ProgramArguments"][-2:] == ["garmin_coach", "daily"]
    assert d["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}
