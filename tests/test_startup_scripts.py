from pathlib import Path


def test_pilot_launcher_does_not_force_interactive_telegram_setup():
    script = (Path(__file__).parents[1] / "START_PILOT.cmd").read_text(encoding="utf-8")
    launch = next(line for line in script.splitlines() if "start_platform.py --pilot" in line)
    assert "--setup-telegram" not in launch
    assert "%TELEGRAM_SETUP%" in launch
    assert 'if /I "%~1"=="telegram" set "TELEGRAM_SETUP=--setup-telegram"' in script
