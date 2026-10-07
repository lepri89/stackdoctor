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
