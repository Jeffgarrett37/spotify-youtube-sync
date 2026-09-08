"""``python -m app [command]``

    (no args) | run     start the long-lived scheduler + status server
    sync                run exactly one sync and exit (honours dry_run)
    status              print the current status JSON and exit
    authorize [target]  interactive OAuth helper (spotify | google | both)
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    rest = argv[1:]

    if command in ("run", "daemon", "serve"):
        from .main import main as run_main

        run_main()
        return 0

    if command == "authorize":
        from .authorize import run as run_authorize

        return run_authorize(rest)

    # commands below need config + logging but not the daemon
    from .config import Config
    from .logging_setup import configure

    config = Config.from_env()
    configure(config.log_level)

    if command == "sync":
        from .main import Application

        app = Application(config)
        outcome = app.run_once("cli")
        app.db.close()
        print(json.dumps(outcome.__dict__, indent=2, default=str))
        return 0 if outcome.status in ("success", "dry_run") else 1

    if command == "status":
        from .main import Application

        app = Application(config)
        print(json.dumps(app._status(), indent=2, default=str))  # noqa: SLF001
        app.db.close()
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
