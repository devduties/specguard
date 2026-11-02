from pathlib import Path

import pytest

from specguard.config import ServerSettings, parse_settings
from specguard.forwarding import ProxySettings


def test_cli_defaults_are_local_and_bounded():
    settings = parse_settings(
        ["--spec", "api.yaml", "--upstream", "http://localhost:9000"], environ={}
    )
    assert settings.spec_path == Path("api.yaml")
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.proxy == ProxySettings("http://localhost:9000")


def test_environment_can_configure_every_setting_without_mutation():
    env = {
        "SPECGUARD_SPEC": "api.json",
        "SPECGUARD_UPSTREAM": "https://up.test/api",
        "SPECGUARD_HOST": "::1",
        "SPECGUARD_PORT": "8081",
        "SPECGUARD_TIMEOUT": "2.5",
        "SPECGUARD_MAX_REQUEST_BYTES": "123",
        "SPECGUARD_MAX_RESPONSE_BYTES": "456",
    }
    before = dict(env)
    settings = parse_settings([], environ=env)
    assert settings == ServerSettings(
        Path("api.json"), ProxySettings("https://up.test/api", 2.5, 123, 456), "::1", 8081
    )
    assert env == before


def test_flags_override_environment_including_invalid_environment_defaults():
    settings = parse_settings(
        [
            "--spec",
            "chosen.yaml",
            "--upstream",
            "http://up.test",
            "--host",
            "127.0.0.1",
            "--port",
            "8080",
            "--timeout",
            "3",
            "--max-request-bytes",
            "100",
            "--max-response-bytes",
            "200",
        ],
        environ={
            "SPECGUARD_SPEC": "wrong.yaml",
            "SPECGUARD_UPSTREAM": "bad",
            "SPECGUARD_PORT": "invalid",
            "SPECGUARD_TIMEOUT": "invalid",
            "SPECGUARD_HOST": "invalid host",
            "SPECGUARD_MAX_REQUEST_BYTES": "invalid",
            "SPECGUARD_MAX_RESPONSE_BYTES": "invalid",
        },
    )
    assert settings == ServerSettings(
        Path("chosen.yaml"), ProxySettings("http://up.test", 3, 100, 200), "127.0.0.1", 8080
    )


@pytest.mark.parametrize("options", [[], ["--spec", "api.yaml"], ["--upstream", "http://up.test"]])
def test_required_configuration_has_clear_errors(options, capsys):
    with pytest.raises(SystemExit) as caught:
        parse_settings(options, environ={})
    assert caught.value.code == 2
    assert "--spec and --upstream" in capsys.readouterr().err


@pytest.mark.parametrize(
    "options",
    [
        ["--port", "0"],
        ["--port", "65536"],
        ["--port", "oops"],
        ["--timeout", "0"],
        ["--timeout", "nan"],
        ["--timeout", "inf"],
        ["--max-request-bytes", "-1"],
        ["--max-response-bytes", "0"],
        ["--host", ""],
        ["--host", "invalid host"],
        ["--host", "http://localhost"],
        ["--host", "localhost:8080"],
        ["--upstream", "file:///tmp/a"],
        ["--upstream", "https://user:secret@up.test"],
    ],
)
def test_invalid_configuration_is_reported_without_traceback(options, capsys):
    with pytest.raises(SystemExit) as caught:
        parse_settings(["--spec", "api.yaml", "--upstream", "http://up.test", *options], environ={})
    assert caught.value.code == 2
    message = capsys.readouterr().err
    assert "error:" in message
    assert "Traceback" not in message


def test_help_does_not_require_configuration(capsys):
    with pytest.raises(SystemExit) as caught:
        parse_settings(["--help"], environ={})
    assert caught.value.code == 0
    assert "SPECGUARD_SPEC" in capsys.readouterr().out
