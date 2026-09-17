"""Stub CLI binaries on PATH, for testing subprocess-backed code.

Copy this file into your feature's ``tests/harness/`` directory. It is vendored,
not imported from the library — features stay standalone by construction.

**Why not mock ``subprocess.run``?** Because then you test the mock. Argument
ordering, stdin routing, exit-code handling, and "binary missing from PATH" are
exactly the things that break against a real tool, and a mock asserts only that
you called it the way you thought you did. A real executable on a real PATH
exercises the actual plumbing at the cost of a few milliseconds.

Usage::

    from harness.stub_cli import stub_clis  # noqa: F401  (pytest fixture)

    def test_argv(stub_clis):
        stub_clis.install("mytool")
        MyProvider().run("hello")
        assert stub_clis.last()["argv"] == ["-p", "hello"]

    def test_failure(stub_clis):
        stub_clis.install("mytool")
        stub_clis.configure("mytool", exit_code=2, stderr="not signed in")
        with pytest.raises(RuntimeError, match="not signed in"):
            MyProvider().run("hello")
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

# The stub itself. Records how it was invoked, then behaves as configured.
_STUB_SOURCE = '''#!{python}
import json, os, sys, time

name = os.path.basename(sys.argv[0])

config = {{}}
config_path = os.environ.get("STUB_CLI_CONFIG")
if config_path and os.path.exists(config_path):
    with open(config_path) as fh:
        config = json.load(fh)
settings = {{**config.get("*", {{}}), **config.get(name, {{}})}}

stdin_text = None
# only read stdin when something was piped in; under `pytest -s` stdin is the
# terminal and reading it would hang the suite
if not sys.stdin.isatty():
    try:
        stdin_text = sys.stdin.read()
    except Exception:
        stdin_text = None

with open(os.environ["STUB_CLI_LOG"], "a") as fh:
    fh.write(json.dumps({{
        "binary": name, "argv": sys.argv[1:], "stdin": stdin_text,
    }}) + "\\n")

if settings.get("sleep"):
    time.sleep(float(settings["sleep"]))

sys.stderr.write(settings.get("stderr", ""))
sys.stdout.write(settings.get("reply", "stub reply"))
sys.exit(int(settings.get("exit_code", 0)))
'''


class StubCLIs:
    """Handle for installing, configuring, and inspecting stub binaries."""

    def __init__(self, bin_dir: Path, log_path: Path, config_path: Path):
        self.bin_dir = bin_dir
        self.log_path = log_path
        self.config_path = config_path
        self._config: dict[str, dict] = {}

    # ------------------------------------------------------------ setup ---

    def install(self, *names: str) -> StubCLIs:
        """Put stub executables named ``names`` on PATH."""
        source = _STUB_SOURCE.format(python=sys.executable)
        for name in names:
            script = self.bin_dir / name
            script.write_text(source)
            script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return self

    def remove(self, name: str) -> None:
        """Take a binary off PATH, simulating it not being installed."""
        (self.bin_dir / name).unlink(missing_ok=True)

    def configure(
        self,
        name: str = "*",
        *,
        reply: str | None = None,
        exit_code: int | None = None,
        stderr: str | None = None,
        sleep: float | None = None,
    ) -> None:
        """Set stub behavior. ``name="*"`` applies to every binary."""
        settings = self._config.setdefault(name, {})
        for key, value in (
            ("reply", reply), ("exit_code", exit_code),
            ("stderr", stderr), ("sleep", sleep),
        ):
            if value is not None:
                settings[key] = value
        self.config_path.write_text(json.dumps(self._config))

    # ------------------------------------------------------ inspection ---

    def calls(self) -> list[dict]:
        """Every invocation, as ``{binary, argv, stdin}`` dicts."""
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text().splitlines()
            if line.strip()
        ]

    def calls_to(self, binary: str) -> list[dict]:
        return [c for c in self.calls() if c["binary"] == binary]

    def last(self) -> dict:
        calls = self.calls()
        assert calls, "no stub binary was invoked"
        return calls[-1]

    def argv(self) -> list[str]:
        """Shorthand for the last invocation's arguments."""
        return self.last()["argv"]

    def flag_value(self, flag: str) -> str:
        """The value following ``flag`` in the last invocation."""
        argv = self.argv()
        assert flag in argv, f"{flag!r} not in {argv!r}"
        return argv[argv.index(flag) + 1]


@pytest.fixture
def stub_clis(tmp_path, monkeypatch) -> StubCLIs:
    """An isolated PATH containing only stubs you install.

    Isolation is the point: the real tools must not leak in and make a test
    pass on your machine and fail in CI, or vice versa.
    """
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    log_path = tmp_path / "stub-calls.jsonl"
    config_path = tmp_path / "stub-config.json"
    config_path.write_text("{}")

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("STUB_CLI_LOG", str(log_path))
    monkeypatch.setenv("STUB_CLI_CONFIG", str(config_path))

    return StubCLIs(bin_dir, log_path, config_path)


@pytest.fixture
def empty_path(tmp_path, monkeypatch) -> None:
    """A PATH with nothing on it — every binary is missing."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
