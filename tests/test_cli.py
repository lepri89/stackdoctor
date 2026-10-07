import pytest

from stackdoctor import server


@pytest.mark.parametrize("flag,expected", [("--help", "usage: stackdoctor"), ("--version", "stackdoctor ")])
def test_flags_print_and_exit_without_starting_server(flag, expected, capsys, monkeypatch):
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: pytest.fail("server must not start"))
    with pytest.raises(SystemExit) as exit_info:
        server.main([flag])
    assert exit_info.value.code == 0
    assert expected in capsys.readouterr().out


def test_unknown_flag_is_an_error(monkeypatch):
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: pytest.fail("server must not start"))
    with pytest.raises(SystemExit) as exit_info:
        server.main(["--nope"])
    assert exit_info.value.code == 2


def test_version_matches_pyproject(capsys):
    """pyproject.toml is the single source of truth for the version."""
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    with pytest.raises(SystemExit):
        server.main(["--version"])
    assert capsys.readouterr().out.strip() == f"stackdoctor {pyproject['project']['version']}"


def test_version_fallback_when_not_installed(monkeypatch):
    import importlib
    import importlib.metadata

    import stackdoctor

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    try:
        assert importlib.reload(stackdoctor).__version__ == "0.0.0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(stackdoctor)
