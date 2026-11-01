#!/usr/bin/env python3
"""
main.py — 1-button "smart" GitHub Push/Update shell.

Pick a project folder. The tool derives a repo name from the folder,
checks whether that repo already exists on GitHub, and automatically:

    - repo does NOT exist  -> push_project()   (create + initial push)
    - repo DOES exist      -> update_project() (push into existing repo)

Force push is ALWAYS enabled: updating an existing
repo overwrites the remote history with this folder's contents — no
toggle, no confirmation prompt.

Both functions live in the single hardened engine file
(src/assets/github_update_engine.py) — there is no separate
push-only engine module.
"""

import os
import sys
import traceback
from pathlib import Path

from src.ui_styles.ui_style_shell import run_shell
from src.assets.github_update import (
    load_env,
    get_github_token,
    get_repo_info,
    push_project,
    update_project,
    _format_repo_name,
)


def resolve_config(relative_path: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).parent
    return str(base_path / relative_path)


def _repo_exists(repo_name: str, token: str) -> bool:
    """
    True  -> repo exists and belongs to the authenticated user.
    False -> repo does not exist (404).
    Any other failure (bad token, network, etc.) is re-raised so it
    surfaces as a real error instead of being silently treated as
    "doesn't exist" and triggering an unwanted repo creation.
    """
    try:
        get_repo_info(repo_name, token=token)
        return True
    except RuntimeError as e:
        if "(404)" in str(e) or " 404" in str(e):
            return False
        raise


def smart_push_task(folder_path: str, progress_callback=None) -> str:
    force_push = True  # always force push; no UI toggle
    def cb(message, percent):
        if progress_callback and isinstance(percent, (int, float)) and percent >= 0:
            progress_callback(int(percent))

    load_env(resolve_config(".env"))

    folder_name = os.path.basename(str(Path(folder_path).expanduser().resolve()))
    repo_name = _format_repo_name(folder_name)

    if progress_callback:
        progress_callback(0)

    token = get_github_token()
    exists = _repo_exists(repo_name, token)

    if exists:
        result = update_project(
            folder_path,
            repo_name=repo_name,
            confirmed=True,   # folder name == derived repo name, already the match check
            force=force_push, # always force push (overwrites remote history)
            token=token,
            progress_callback=cb,
        )
    else:
        # Brand-new repo, nothing to force-push over yet.
        result = push_project(
            folder_path,
            repo_name=repo_name,
            token=token,
            progress_callback=cb,
        )

    # IMPORTANT: only a normal `return` is treated as success by the shell.
    # A failed result must raise, or the UI will wrongly show "success".
    if not result.get("success"):
        raise RuntimeError(result.get("message", "Operation failed for an unknown reason."))

    return result.get("message", "Completed successfully.")


if __name__ == "__main__":
    try:
        config_file = "src/configs/github_update_ui_config.json"
        resolved_path = resolve_config(config_file)
        sys.exit(run_shell(smart_push_task, resolved_path))
    except Exception as e:
        print("A fatal error occurred before the UI could launch:\n")
        traceback.print_exc()
        input("\nPress Enter to exit...")
        sys.exit(1)