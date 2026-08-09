"""Unified command-line entry point for rr-trans."""

from __future__ import annotations

import argparse

from .storage import STORAGE_PREFIX


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rr-trans",
        description="Collect and browse AI coding-agent transcripts.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ui = commands.add_parser("ui", help="Open the local upload and consent UI.")
    ui.add_argument(
        "--all",
        action="store_true",
        help="Upload every discovered transcript without opening the UI.",
    )
    ui.add_argument(
        "--name",
        default="anonymous",
        metavar="CONTRIBUTOR",
        help="Contributor name used with --all (default: anonymous).",
    )

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
    if args.command == "ui":
        from .app import main as run_ui

        return run_ui(headless=args.all, contributor_name=args.name)
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
