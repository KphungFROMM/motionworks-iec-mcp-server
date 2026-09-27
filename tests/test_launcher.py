"""The launcher must survive a hostile environment.

A venv's ``python.exe`` reads ``PYTHONHOME`` and ``PYTHONPATH`` from its
environment. If a host application sets ``PYTHONHOME`` to something that is not a
valid Python home, the interpreter dies during startup — before any of this
project's code runs — and the MCP client reports only that the server would not
connect. Some agent harnesses set ``PYTHONHOME`` for their own bundled Python,
which is exactly that situation, and it was observed here: the bare console script
returned no response at all, while the launcher beside it started normally.

These tests pin the launcher's behaviour so the wrapper cannot be "simplified" away.
They need a built virtual environment, and are skipped without one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "scripts" / "motionworks-iec-mcp-server.cmd"
VENV_EXE = ROOT / ".venv" / "Scripts" / "motionworks-iec-mcp-server.exe"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not LAUNCHER.is_file() or not VENV_EXE.is_file(),
    reason="needs Windows and a built .venv with the console script",
)

HOSTILE = {
    "PYTHONHOME": r"C:\nonexistent\python",
    "PYTHONPATH": r"C:\nonexistent\lib",
}


def _run(argv: list[str], hostile: bool) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if hostile:
        env.update(HOSTILE)
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=90, check=False
    )


def _ok(result: subprocess.CompletedProcess) -> bool:
    return "usage:" in (result.stdout or "")


class TestLauncher:
    def test_the_launcher_is_pure_crlf(self):
        """cmd.exe parses LF-only batch files unreliably.

        This shipped broken once: ``.gitattributes`` applied ``eol=lf`` to everything,
        so the launcher was LF-only in the repository and would be checked out that
        way — multi-line constructs and labels can then be misread as commands. Batch
        files are the one exception to the LF rule and ``.gitattributes`` pins
        ``*.cmd text eol=crlf``; this asserts the working copy matches.
        """

        raw = LAUNCHER.read_bytes()
        crlf = raw.count(b"\r\n")
        lone_lf = raw.count(b"\n") - crlf
        assert crlf > 0 and lone_lf == 0, (
            f"launcher has {crlf} CRLF pairs and {lone_lf} bare LF; a batch file must "
            "be pure CRLF (see .gitattributes)"
        )

    def test_launcher_starts_with_a_clean_environment(self):
        assert _ok(_run(["cmd", "/c", str(LAUNCHER), "--help"], hostile=False))

    def test_launcher_survives_a_hostile_pythonhome(self):
        """The regression this file exists for."""

        result = _run(["cmd", "/c", str(LAUNCHER), "--help"], hostile=True)
        assert _ok(result), (
            "the launcher did not start under a hostile PYTHONHOME. It must clear "
            f"PYTHONHOME and PYTHONPATH before invoking the server.\nstderr: "
            f"{(result.stderr or '').strip()[-400:]}"
        )

    def test_the_bare_console_script_is_affected_which_is_why_the_launcher_exists(self):
        """Documents the reason, and fails loudly if the premise ever changes.

        If a future Python release stops honouring PYTHONHOME here, this test will
        fail and the launcher can be reconsidered — better than keeping a wrapper
        nobody can justify.
        """

        result = _run([str(VENV_EXE), "--help"], hostile=True)
        assert not _ok(result), (
            "the bare console script now tolerates a hostile PYTHONHOME, so the "
            "launcher may no longer be necessary"
        )

    def test_launcher_passes_arguments_through(self):
        result = _run(["cmd", "/c", str(LAUNCHER), "--transport", "sse", "--help"],
                      hostile=True)
        assert _ok(result)

    def test_it_does_not_swallow_the_servers_exit_code(self):
        """A non-zero exit must reach the MCP client, not be masked as success."""

        result = _run(["cmd", "/c", str(LAUNCHER), "--not-a-real-flag"], hostile=True)
        assert result.returncode != 0
