"""Root .env loading and repository safety checks without secret disclosure."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

from .protocol import ROOT


class CredentialPreflightError(RuntimeError):
    pass


def verify_env_git_safety(root: Path = ROOT) -> dict[str, bool]:
    env_path = root / ".env"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0
    if tracked:
        raise CredentialPreflightError("repository root .env is tracked by Git")
    if not ignored:
        raise CredentialPreflightError("repository root .env is not ignored by Git")
    return {"root_env_exists": env_path.is_file(), "env_tracked": tracked,
            "env_ignored": ignored}


def load_root_credentials(root: Path = ROOT) -> dict[str, Any]:
    """Load only the root .env without overriding existing environment values."""
    safety = verify_env_git_safety(root)
    env_path = root / ".env"
    if not env_path.is_file():
        return {**safety, "root_env_key_present": False,
                "openai_api_key_available": bool(os.environ.get("OPENAI_API_KEY"))}
    try:
        root_env_key_present = bool(dotenv_values(dotenv_path=env_path, encoding="utf-8").get("OPENAI_API_KEY"))
    except Exception as exc:
        raise CredentialPreflightError(f"could not parse repository root .env ({type(exc).__name__})") from None
    load_dotenv(dotenv_path=env_path, override=False, encoding="utf-8")
    return {**safety, "root_env_key_present": root_env_key_present,
            "openai_api_key_available": bool(os.environ.get("OPENAI_API_KEY"))}


def require_openai_credential(root: Path = ROOT) -> None:
    status = load_root_credentials(root)
    if not status["root_env_key_present"] or not status["openai_api_key_available"]:
        raise CredentialPreflightError("OPENAI_API_KEY is unavailable after root .env loading")
