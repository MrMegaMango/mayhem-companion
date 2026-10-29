"""Entry point for the Windows bundle and its maintenance commands."""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    if "--self-test" in arguments:
        remaining = [argument for argument in arguments if argument != "--self-test"]
        if (arguments.count("--self-test") != 1 or len(remaining) != 2
                or remaining[0] != "--report"):
            return 2
        report = Path(remaining[1])
        if not report.is_absolute():
            return 2
        from mayhem.diagnostics import run_self_test
        return run_self_test(report)

    if "--watch-league" in arguments:
        if arguments != ["--watch-league"]:
            return 2
        from mayhem.autostart import main as watch_league
        return watch_league()

    if "--uninstall-cleanup" in arguments:
        if arguments != ["--uninstall-cleanup"]:
            return 2
        from mayhem.autostart import remove_owned_startup
        return 0 if remove_owned_startup() else 1

    if "--resume-owned-startup" in arguments:
        if arguments != ["--resume-owned-startup"]:
            return 2
        from mayhem.autostart import resume_owned_startup
        return 0 if resume_owned_startup() else 1

    from mayhem.app import main as run_app
    original_arguments = sys.argv
    try:
        # The normal app parser keeps its existing --browse/--auto-start API.
        sys.argv = [original_arguments[0], *arguments]
        run_app()
        return 0
    finally:
        sys.argv = original_arguments


if __name__ == "__main__":
    raise SystemExit(main())
