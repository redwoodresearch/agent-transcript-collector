"""Unified command-line entry point for rr-trans."""

from __future__ import annotations

import argparse
import json
import webbrowser

from .storage import STORAGE_PREFIX


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rr-trans",
        description="Collect and browse AI coding-agent transcripts.",
    )
    commands = parser.add_subparsers(dest="command")

    commands.add_parser(
        "install",
        help="Install or update the CLI and always-on local UI (default).",
    )

    ui = commands.add_parser("ui", help="Open the local upload and consent UI.")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int)
    ui.add_argument("--no-open", action="store_true")
    ui.add_argument("--strict-port", action="store_true")

    ui_service = commands.add_parser(
        "ui-service", help="Manage the always-on local UI service."
    )
    ui_service.add_argument("action", choices=("install", "status", "uninstall"))

    tui = commands.add_parser("tui", help="Browse uploaded transcript folders in S3.")
    tui.add_argument(
        "--prefix",
        default=f"{STORAGE_PREFIX}/",
        help=f"S3 prefix to browse (default: {STORAGE_PREFIX}/).",
    )

    watcher = commands.add_parser("watcher", help="Manage automatic uploads.")
    watcher_commands = watcher.add_subparsers(dest="watcher_command", required=True)
    run = watcher_commands.add_parser("run", help="Run one upload check.")
    run.add_argument("--config")
    watcher_commands.add_parser("status", help="Show watcher status.")
    uninstall = watcher_commands.add_parser("uninstall", help="Remove the watcher.")
    uninstall.add_argument("--purge", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command in {None, "install"}:
        from .ui_service import install_and_update

        result = install_and_update()
        print(json.dumps(result, indent=2))
        webbrowser.open(result["url"])
        return 0
    if args.command == "ui":
        from .app import main as run_ui

        return run_ui(
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            strict_port=args.strict_port,
        )
    if args.command == "ui-service":
        from .ui_service import install_service, status, uninstall

        operation = {
            "install": install_service,
            "status": status,
            "uninstall": uninstall,
        }[args.action]
        print(json.dumps(operation(), indent=2))
        return 0
    if args.command == "watcher":
        from .watcher import main as run_watcher

        watcher_args = [args.watcher_command]
        if args.watcher_command == "run" and args.config:
            watcher_args.extend(["--config", args.config])
        if args.watcher_command == "uninstall" and args.purge:
            watcher_args.append("--purge")
        return run_watcher(watcher_args)
    from .tui import main as run_tui

    return run_tui(args.prefix)


if __name__ == "__main__":
    raise SystemExit(main())
