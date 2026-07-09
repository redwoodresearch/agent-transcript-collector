"""Runtime environment configuration for command-line entry points."""

from pathlib import Path

from dotenv import load_dotenv


def load_local_env() -> None:
    """Load ``.env`` from the working directory without replacing shell values."""
    load_dotenv(Path.cwd() / ".env", override=False)
