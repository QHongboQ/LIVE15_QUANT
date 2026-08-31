from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from live15_quant import cli
from live15_quant.config import Settings


@pytest.mark.parametrize(
    ("entrypoint", "program"),
    [
        (cli.recorder_main, "live15-record"),
        (cli.status_main, "live15-status"),
        (cli.coverage_main, "live15-coverage"),
        (cli.paper_shadow_main, "live15-paper-shadow"),
    ],
)
def test_long_running_entrypoint_help_has_no_side_effects(
    entrypoint: Callable[[Sequence[str] | None], None],
    program: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_settings_load() -> None:
        raise AssertionError("--help must exit before loading runtime configuration")

    monkeypatch.setattr(cli, "load_settings", unexpected_settings_load)

    with pytest.raises(SystemExit) as exit_info:
        entrypoint(["--help"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith(f"usage: {program}")


def test_recorder_entrypoint_rejects_unknown_arguments_before_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: (_ for _ in ()).throw(
            AssertionError("invalid arguments must not start the recorder")
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.recorder_main(["--unexpected"])

    assert exit_info.value.code == 2


def test_status_is_safe_before_recorder_creates_health_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health_path = tmp_path / "health.json"
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: Settings(recorder_health_path=health_path),
    )

    with pytest.raises(SystemExit, match="start live15-record first"):
        cli.status_main([])

    assert not health_path.exists()


def test_paper_shadow_once_uses_forward_runtime_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeForwardRuntime:
        def __init__(self, _settings, *, allow_model_materialization: bool) -> None:
            assert allow_model_materialization is False

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def run_once(self) -> dict[str, int]:
            calls.append("run_once")
            return {"processed": 0}

    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "ForwardShadowRuntime", FakeForwardRuntime)

    cli.paper_shadow_main(["--once"])

    assert calls == ["run_once"]
