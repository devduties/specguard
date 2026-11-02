import json

import pytest

from specguard.cli import main


@pytest.fixture
def valid_spec(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(
        json.dumps({"openapi": "3.1.0", "info": {"title": "Demo", "version": "1"}, "paths": {}}),
        encoding="utf-8",
    )
    return path


def test_cli_launches_with_validated_settings(valid_spec, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "specguard.cli.uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs))
    )
    assert main(["--spec", str(valid_spec), "--upstream", "http://up.test", "--port", "8080"]) == 0
    app, options = calls[0]
    assert app.state.settings.spec_path == valid_spec
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 8080
    assert options["lifespan"] == "on"
    assert options["workers"] == 1
    assert options["access_log"] is False


@pytest.mark.parametrize("kind", ["missing", "invalid"])
def test_cli_reports_spec_errors_without_starting_server(tmp_path, monkeypatch, capsys, kind):
    path = tmp_path / "spec.json"
    if kind == "invalid":
        path.write_text("{}", encoding="utf-8")

    def start(*_args, **_kwargs):
        pytest.fail("Server started with an invalid contract")

    monkeypatch.setattr("specguard.cli.uvicorn.run", start)
    assert main(["--spec", str(path), "--upstream", "http://up.test"]) == 2
    message = capsys.readouterr().err
    assert "cannot load contract" in message
    assert "Traceback" not in message


def test_listener_failure_has_clear_nonzero_exit(valid_spec, monkeypatch, capsys):
    def start(*_args, **_kwargs):
        raise OSError("Address already in use")

    monkeypatch.setattr("specguard.cli.uvicorn.run", start)
    assert main(["--spec", str(valid_spec), "--upstream", "http://up.test"]) == 1
    assert "cannot start listener" in capsys.readouterr().err
