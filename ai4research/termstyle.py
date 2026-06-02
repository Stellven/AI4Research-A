"""Minimal terminal styling for the CLIs: ANSI colour + status glyphs, auto-disabled when
stdout is not a TTY or NO_COLOR is set, so piped/redirected output (and tests) stay plain.
Standard library only — the compiler core never imports this; only the CLI entry points do.
"""
from __future__ import annotations

import os
import sys

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32",
          "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36"}

# Unicode glyphs are used only when styling is enabled (a capable TTY); a plain ASCII
# fallback is emitted otherwise so redirected output never depends on the terminal's encoding.
_GLYPHS = {
    "finalized": ("✓", "OK"), "diagnostic_only": ("◐", "~"), "failed": ("✗", "X"),
    "pass": ("✓", "+"), "warning": ("⚠", "!"), "hard_fail": ("✗", "x"),
    "youtube": ("⏱", "t="), "github": ("#", "#"), "bullet": ("·", "-"),
}


class Style:
    """Callable styler: ``s("text", "green", "bold")`` colours when enabled, else returns text."""

    def __init__(self, stream=None, force: bool | None = None) -> None:
        if force is not None:
            self.enabled = force
            return
        if os.environ.get("NO_COLOR"):          # NO_COLOR always wins (https://no-color.org)
            self.enabled = False
            return
        if os.environ.get("CLICOLOR_FORCE"):    # force colour even when piped (e.g. into `less -R`)
            self.enabled = True
            return
        stream = stream if stream is not None else sys.stdout
        self.enabled = bool(getattr(stream, "isatty", lambda: False)())

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        codes = ";".join(_CODES[s] for s in styles if s in _CODES)
        return f"\033[{codes}m{text}\033[0m" if codes else text

    def glyph(self, name: str) -> str:
        uni, asc = _GLYPHS[name]
        return uni if self.enabled else asc
