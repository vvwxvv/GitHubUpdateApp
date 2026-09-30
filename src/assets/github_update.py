"""
GitHub Push Engine (production-hardened)
─────────────────────────────────────────
Core logic for initializing/reusing a local git repo, creating a GitHub
repo (via API), and pushing a project folder to it.

Also supports UPDATING an existing GitHub repository:
    list_github_repos()  -> fetch the user's repositories for a selector
    update_project()     -> push a local folder into an existing repo,
                            with folder-name / repo-name match detection
                            and a two-phase confirm flow when they differ.

Hardening in this version (over the original):
    - No hardcoded 120s ceiling. Timeouts for `add`/`commit`/`push` scale
      with folder size and are individually configurable.
    - subprocess.TimeoutExpired is caught explicitly and turned into an
      actionable RuntimeError (instead of leaking a raw traceback that a
      thin UI shell can misreport as "success").
    - Works whether the target folder ALREADY has a `.git` directory
      (from a prior run, a manual `git init`, or a real existing repo)
      or does NOT. Never blindly re-`git init`s over a folder that's
      already a valid repo; never assumes one is is present either.
    - `.gitignore` handling MERGES missing common patterns into an
      existing file instead of skipping it entirely, and never clobbers
      user content.
    - A pre-flight scan (`analyze_folder`) reports file count / size /
      the largest files BEFORE staging, so callers can warn the user or
      auto-exclude heavy paths instead of hitting a mystery timeout.
    - Transient push failures (network blips) get a small retry with
      backoff. Non-transient failures (auth, rejected non-fast-forward,
      name conflicts) fail fast with a clear message, no retry.
    - Branch handling is safe when a pre-existing local repo's current
      branch differs from the intended default branch.
"""

import os
import re
import json
import time
import subprocess
import logging
import shutil
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ── Tunables ───────────────────────────────────────────────────────────────

# Baseline + per-MB scaling for `git add -A` / `git commit` timeouts.
# Hashing is the slow part; large binary/media folders need real headroom.
ADD_TIMEOUT_BASE_SECONDS = 180
ADD_TIMEOUT_PER_MB_SECONDS = 0.5      # +0.5s per MB of working-tree content
ADD_TIMEOUT_MAX_SECONDS = 1800        # hard ceiling: 30 min

COMMIT_TIMEOUT_SECONDS = 300
PUSH_TIMEOUT_SECONDS = 900            # network-bound; generous by default
PUSH_MAX_RETRIES = 2
PUSH_RETRY_BACKOFF_SECONDS = 5

DEFAULT_GIT_TIMEOUT_SECONDS = 60      # init/config/remote/status/etc.

# Paths that are almost never meant to be committed. Used both for the
# generated .gitignore AND for the pre-flight size warning.
HEAVY_DIR_NAMES = {
    "node_modules", ".venv", "venv", "appenv", "env",
    "__pycache__", "dist", "build", ".next", ".nuxt",
    "target", ".cache", ".pytest_cache", ".mypy_cache",
    "site-packages", ".git",  # .git excluded from OUR size scan, not from git itself
}

# Individual files above this size (bytes) get flagged in the pre-flight report.
LARGE_FILE_WARN_BYTES = 50 * 1024 * 1024  # 50 MB

# GitHub refuses any single file/blob larger than 100 MB and *warns* above
# 50 MB. Anything at/over the hard limit MUST be kept out of the push, or the
# server-side pre-receive hook rejects the whole push (GH001) -- no amount of
# `--force` helps, because the oversized blob is still being sent.
GITHUB_HARD_LIMIT_BYTES = 100 * 1024 * 1024  # 100 MB hard ceiling

# GitHub API networking: give it real headroom and retry reads, since the
# REST API is frequently slow/unreachable from some networks (VPN, CN, etc.).
GH_API_TIMEOUT_SECONDS = 60
GH_API_MAX_ATTEMPTS = 5
GH_API_RETRY_BACKOFF_SECONDS = 4


# ── Load .env ──────────────────────────────────────────────────────────────

def load_env(filepath=None):
    if filepath and os.path.isfile(filepath):
        load_dotenv(filepath)
        return
    load_dotenv()


def get_github_token():
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN is not set. Create a .env file or set the environment variable.")
    return token.strip()


def get_github_user():
    return os.getenv("GITHUB_USER", "").strip() or None


def get_github_email():
    return os.getenv("GITHUB_EMAIL", "").strip() or "user@github.local"


# ── Repository name formatter ─────────────────────────────────────────────

def _format_repo_name(name):
    """
    Clean up a repository name:
      - Remove all spaces
      - Capitalize first letter (if it's a letter)
      - Replace invalid characters (anything not alnum, '-', '_', '.') with '-'
      - Strip leading/trailing hyphens
    """
    name = name.replace(" ", "")
    name = re.sub(r"[^a-zA-Z0-9_.-]", "-", name)
    name = name.strip("-")
    if name and name[0].islower():
        name = name[0].upper() + name[1:]
    return name


# ── Git availability check ────────────────────────────────────────────────

def _check_git():
    if not shutil.which("git"):
        raise RuntimeError(
            "Git is not installed or not in your system PATH.\n"
            "Please install Git from https://git-scm.com/download/win\n"
            "and make sure it's added to PATH during installation."
        )


# ── GitHub API helpers ──────────────────────────────────────────────────────

def _gh_opener():
    """
    urllib opener for api.github.com. Uses the proxy from GH_PROXY (set in
    .env, e.g. GH_PROXY=http://127.0.0.1:7890) if present; otherwise falls
    back to HTTPS_PROXY / HTTP_PROXY / the system proxy settings.
    """
    import urllib.request
    proxy = (os.environ.get("GH_PROXY") or "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _gh_api(method, endpoint, data=None, token=None, timeout=GH_API_TIMEOUT_SECONDS):
    import socket
    import urllib.request
    import urllib.error

    token = token or get_github_token()
    url = f"https://api.github.com{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitHubPushApp/1.0",
    }
    body = json.dumps(data).encode("utf-8") if data else None

    # GET/HEAD are safe to retry. Mutating verbs are NOT: a retried POST after
    # a timeout could create a duplicate resource server-side.
    retryable = method.upper() in ("GET", "HEAD")
    attempts = GH_API_MAX_ATTEMPTS if retryable else 1
    last_err = None

    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with _gh_opener().open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            # Retry only transient 5xx on idempotent calls.
            if e.code in (500, 502, 503, 504) and retryable and attempt < attempts:
                last_err = f"HTTP {e.code}"
                time.sleep(GH_API_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise RuntimeError(f"GitHub API {method} {endpoint} failed ({e.code}): {detail}")
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as e:
            # A read/handshake timeout can surface as a raw TimeoutError
            # (socket.timeout) instead of being wrapped in URLError -- the old
            # code only caught URLError, so these leaked out as raw tracebacks.
            reason = getattr(e, "reason", None) or e
            last_err = str(reason)
            if attempt < attempts:
                time.sleep(GH_API_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise RuntimeError(
                f"GitHub API {method} {endpoint} unreachable after {attempts} "
                f"attempt(s): {reason}"
            )

    # Unreachable, but keep a sane fallback.
    raise RuntimeError(f"GitHub API {method} {endpoint} failed: {last_err}")


def _get_authenticated_user(token):
    data = _gh_api("GET", "/user", token=token)
    return data.get("login")


# ── List existing repositories (for the UI selector) ───────────────────────

def list_github_repos(token=None, max_pages=3):
    token = token or get_github_token()
    repos = []
    for page in range(1, max_pages + 1):
        batch = _gh_api(
            "GET",
            f"/user/repos?per_page=100&page={page}&sort=updated&affiliation=owner",
            token=token,
        )
        if not batch:
            break
        for r in batch:
            repos.append({
                "name": r.get("name"),
                "full_name": r.get("full_name"),
                "clone_url": r.get("clone_url"),
                "html_url": r.get("html_url"),
                "default_branch": r.get("default_branch") or "main",
                "private": bool(r.get("private")),
            })
        if len(batch) < 100:
            break
    return repos


def get_repo_info(repo_name, token=None):
    """Fetch a single repository owned by the authenticated user."""
    token = token or get_github_token()
    user = _get_authenticated_user(token)
    r = _gh_api("GET", f"/repos/{user}/{repo_name}", token=token)
    return {
        "name": r.get("name"),
        "full_name": r.get("full_name"),
        "clone_url": r.get("clone_url"),
        "html_url": r.get("html_url"),
        "default_branch": r.get("default_branch") or "main",
        "private": bool(r.get("private")),
    }


# ── Folder-name / repo-name match check ─────────────────────────────────────

def names_match(folder_path, repo_name):
    folder_name = os.path.basename(str(Path(folder_path).resolve()))
    return _format_repo_name(folder_name).lower() == (repo_name or "").lower()


# ── Validate project folder ─────────────────────────────────────────────────

def validate_project_folder(folder_path):
    path = Path(folder_path).expanduser().resolve()
    if not path.is_dir():
        raise NotADirectoryError(f"Project folder does not exist:\n{path}")
    items = list(path.iterdir())
    if not items:
        raise ValueError(f"Project folder is empty:\n{path}")
    return str(path)


# ── Pre-flight folder analysis ──────────────────────────────────────────────

def analyze_folder(folder_path):
    """
    Walk the folder (skipping .git) and report size/file-count stats so
    callers can warn the user or auto-exclude heavy paths BEFORE staging,
    instead of discovering the problem via a timeout.

    Returns:
        {
          "total_bytes": int,
          "total_mb": float,
          "file_count": int,
          "heavy_dirs_present": [names...],   # e.g. ["node_modules", ".venv"]
          "large_files": [{"path": rel_path, "mb": float}, ...],  # top 10
        }
    """
    folder = Path(folder_path).resolve()
    total_bytes = 0
    file_count = 0
    heavy_dirs_present = set()
    large_files = []

    for root, dirs, files in os.walk(folder):
        # Prune traversal into heavy/irrelevant dirs (still counted once as "present")
        pruned = []
        for d in list(dirs):
            if d in HEAVY_DIR_NAMES:
                heavy_dirs_present.add(d)
                pruned.append(d)
        for d in pruned:
            dirs.remove(d)

        if ".git" in dirs:
            dirs.remove(".git")

        for fname in files:
            fpath = Path(root) / fname
            try:
                size = fpath.stat().st_size
            except OSError:
                continue
            total_bytes += size
            file_count += 1
            if size >= LARGE_FILE_WARN_BYTES:
                large_files.append({
                    "path": str(fpath.relative_to(folder)),
                    "mb": round(size / (1024 * 1024), 1),
                })

    large_files.sort(key=lambda x: x["mb"], reverse=True)

    return {
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 1),
        "file_count": file_count,
        "heavy_dirs_present": sorted(heavy_dirs_present),
        "large_files": large_files[:10],
    }


def _adaptive_add_timeout(folder_path):
    """Scale the `git add -A` timeout to the folder's actual size."""
    try:
        stats = analyze_folder(folder_path)
        timeout = ADD_TIMEOUT_BASE_SECONDS + stats["total_mb"] * ADD_TIMEOUT_PER_MB_SECONDS
        return int(min(timeout, ADD_TIMEOUT_MAX_SECONDS))
    except Exception:
        # If analysis itself fails for any reason, fall back to a safe default
        # rather than letting the whole operation blow up here.
        return ADD_TIMEOUT_BASE_SECONDS


# ── .gitignore handling (merge, never clobber) ──────────────────────────────

COMMON_GITIGNORE_LINES = [
    "# Python",
    "__pycache__/",
    "*.py[cod]",
    "*.so",
    "*.egg-info/",
    "dist/",
    "build/",
    "*.egg",
    ".env",
    ".venv/",
    "venv/",
    "appenv/",
    "",
    "# Node",
    "node_modules/",
    ".next/",
    "",
    "# Large/binary artifacts",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.7z",
    "*.pt",
    "*.pth",
    "*.onnx",
    "*.bin",
    "*.safetensors",
    "",
    "# macOS",
    ".DS_Store",
    "*.swp",
    "*.swo",
    "",
    "# IDE",
    ".idea/",
    ".vscode/",
    "*.sublime-*",
    "",
    "# Logs",
    "*.log",
]


def _ensure_gitignore(folder_path):
    """
    Create .gitignore if missing, OR merge in any of our common patterns
    that aren't already present in an existing .gitignore. Never removes
    or reorders anything the user already has.

    Returns True if the file was created or modified, False if untouched.
    """
    gitignore_path = os.path.join(folder_path, ".gitignore")

    if not os.path.exists(gitignore_path):
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write("\n".join(COMMON_GITIGNORE_LINES) + "\n")
        return True

    with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
        existing_lines = f.read().splitlines()
    existing_set = {line.strip() for line in existing_lines if line.strip()}

    missing = [
        line for line in COMMON_GITIGNORE_LINES
        if line.strip() and line.strip() not in existing_set
    ]
    if not missing:
        return False

    with open(gitignore_path, "a", encoding="utf-8") as f:
        f.write("\n\n# --- appended by GitHubPushApp ---\n")
        f.write("\n".join(missing) + "\n")
    return True


def _append_gitignore_lines(folder_path, lines):
    """Append exact paths to .gitignore (used to exclude oversized files).

    Skips anything already present so repeated runs don't duplicate entries.
    Returns True if the file was modified.
    """
    if not lines:
        return False
    gitignore_path = os.path.join(folder_path, ".gitignore")
    existing = set()
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
            existing = {ln.strip() for ln in f.read().splitlines() if ln.strip()}
    new_lines = [ln for ln in lines if ln.strip() and ln.strip() not in existing]
    if not new_lines:
        return False
    with open(gitignore_path, "a", encoding="utf-8") as f:
        f.write("\n\n# --- auto-added by GitHubPushApp (oversized files) ---\n")
        f.write("\n".join(new_lines) + "\n")
    return True


def _is_git_ignored(folder_path, rel_path):
    """True if git would ignore rel_path under the repo's ignore rules."""
    try:
        r = subprocess.run(
            ["git", "check-ignore", "-q", "--", rel_path],
            cwd=folder_path, capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _exclude_oversize_files(folder_path, progress_callback=None):
    """Find files that GitHub would reject (>=100 MB) and add them to
    .gitignore so they never enter the commit. Also returns a list of 50-100 MB
    files to warn about (GitHub accepts those but flags them).

    Returns (offenders, warnings) as lists of (relative_path, size_bytes).
    """
    folder = Path(folder_path).resolve()
    offenders = []
    warnings = []

    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != ".git" and d not in HEAVY_DIR_NAMES]
        for fname in files:
            fpath = Path(root) / fname
            try:
                size = fpath.stat().st_size
            except OSError:
                continue
            if size < LARGE_FILE_WARN_BYTES:
                continue
            rel = fpath.relative_to(folder).as_posix()
            if _is_git_ignored(folder_path, rel):
                continue  # already excluded (e.g. inside .next/ or node_modules/)
            if size >= GITHUB_HARD_LIMIT_BYTES:
                offenders.append((rel, size))
            else:
                warnings.append((rel, size))

    if offenders:
        _append_gitignore_lines(folder_path, [rel for rel, _ in offenders])
        if progress_callback:
            names = ", ".join(rel for rel, _ in offenders[:5])
            more = "..." if len(offenders) > 5 else ""
            progress_callback(
                f"Excluded {len(offenders)} file(s) over 100 MB from the push "
                f"({names}{more}).", 25)
    return offenders, warnings


def _clean_tracked_heavy(folder_path, progress_callback=None):
    """Un-track files that are already committed but now excluded by
    .gitignore (typically node_modules/ and .next/ added before the ignore
    rules existed). `.gitignore` alone does NOT stop tracked files, so this
    step is required.

    Returns the number of files removed from the index.
    """
    removed = 0

    # 1) Known heavy top-level dirs in one shot (avoids listing thousands of
    #    files, which can blow the Windows command-line limit).
    heavy = sorted(d for d in HEAVY_DIR_NAMES if d != ".git")
    try:
        _run_git(["rm", "-r", "--cached", "-q", "--ignore-unmatch", "--"] + heavy,
                 folder_path, timeout=DEFAULT_GIT_TIMEOUT_SECONDS)
    except RuntimeError:
        pass

    # 2) Anything else that is tracked but now matched by ignore rules.
    try:
        out = _run_git(["ls-files", "-c", "-i", "--exclude-standard"], folder_path)
    except RuntimeError:
        out = ""
    files = [f for f in out.splitlines() if f.strip()]
    for i in range(0, len(files), 200):
        chunk = files[i:i + 200]
        try:
            _run_git(["rm", "--cached", "-q", "--ignore-unmatch", "--"] + chunk,
                     folder_path, timeout=DEFAULT_GIT_TIMEOUT_SECONDS)
            removed += len(chunk)
        except RuntimeError:
            pass

    if removed and progress_callback:
        progress_callback(
            f"Un-tracked {removed} previously committed file(s) now excluded "
            f"by .gitignore.", 28)
    return removed


def _has_commits(folder_path):
    """True if the repo already has at least one commit (HEAD resolves)."""
    try:
        _run_git(["rev-parse", "--verify", "HEAD"], folder_path)
        return True
    except RuntimeError:
        return False


def _rebuild_single_commit(folder_path, branch, commit_message, progress_callback=None):
    """Collapse the repository into ONE fresh commit containing only the
    current (cleaned) working tree.

    Needed because a previously-committed 287 MB blob is still reachable from
    the old history, so every push keeps trying to upload it and GitHub keeps
    rejecting it (GH001) -- `--force` included. By creating a new root commit
    with no parents, the old oversized blobs are no longer reachable and are
    never sent.

    The pre-existing history is preserved first on a backup branch, and the
    working tree on disk is never modified (only the index/history change).
    """
    backup = f"{branch}__backup"
    try:
        _run_git(["branch", "-f", backup, branch], folder_path)
    except RuntimeError:
        pass

    ts = time.strftime("%Y%m%d-%H%M%S")
    temp = f"__clean_push_{ts}"
    try:
        _run_git(["branch", "-D", temp], folder_path)
    except RuntimeError:
        pass

    if progress_callback:
        progress_callback("Building clean snapshot (this can take a while)...", 32)

    _run_git(["checkout", "--orphan", temp], folder_path)

    # Wipe the index so the merged .gitignore is honoured for EVERY file,
    # including ones that were tracked in older commits.
    try:
        _run_git(["rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", "."],
                 folder_path, timeout=DEFAULT_GIT_TIMEOUT_SECONDS)
    except RuntimeError:
        pass

    add_timeout = _adaptive_add_timeout(folder_path)
    _run_git(["add", "-A"], folder_path, timeout=add_timeout)

    if _run_git(["status", "--porcelain"], folder_path):
        _run_git(["commit", "-m", commit_message], folder_path,
                 timeout=COMMIT_TIMEOUT_SECONDS)
    else:
        _run_git(["commit", "--allow-empty", "-m", commit_message], folder_path,
                 timeout=COMMIT_TIMEOUT_SECONDS)

    # Move the fresh single-commit history onto the target branch name.
    _run_git(["branch", "-M", temp, branch], folder_path)
    return branch


# ── Git repo state detection ────────────────────────────────────────────────

def _is_valid_git_repo(folder_path):
    """True if folder_path is inside a working, non-corrupt git repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=folder_path, capture_output=True, text=True, timeout=15,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except Exception:
        return False


def _current_branch(folder_path):
    try:
        out = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=folder_path, capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


# ── Low-level git runner ────────────────────────────────────────────────────

def _git_process_running():
    """True if another git process appears to be running (best effort)."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq git.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, errors="replace", timeout=10,
            ).stdout.lower()
            return "git.exe" in out
        return subprocess.run(["pgrep", "-x", "git"], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return False  # can't tell -> assume stale


def _clear_stale_git_locks(cwd):
    """
    Remove leftover *.lock files inside .git (e.g. index.lock from a crashed
    or killed git run). Skipped if a git process is still running.
    Returns the list of removed lock paths.
    """
    removed = []
    git_dir = os.path.join(cwd, ".git")
    if not os.path.isdir(git_dir) or _git_process_running():
        return removed
    for root, _dirs, files in os.walk(git_dir):
        for name in files:
            if name.endswith(".lock"):
                path = os.path.join(root, name)
                try:
                    os.remove(path)
                    removed.append(path)
                except OSError:
                    pass
    return removed


def _run_git(cmd, cwd, capture_output=True, timeout=DEFAULT_GIT_TIMEOUT_SECONDS,
             _lock_retried=False):
    try:
        result = subprocess.run(
            ["git"] + cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"git {' '.join(cmd)} timed out after {timeout}s.\n"
            f"This usually means the folder contains something very large "
            f"(a venv/, node_modules/, build artifacts, model weights, etc.) "
            f"that git had to hash or transfer. Run analyze_folder() on this "
            f"path to see what's heavy, add it to .gitignore, and try again."
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "Git executable not found. Please install Git and ensure it's in your PATH.\n"
            "You can download it from https://git-scm.com/download/win"
        ) from e

    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else "(no error output)"
        if (not _lock_retried and ".lock" in err
                and ("File exists" in err or "Unable to create" in err)):
            if _clear_stale_git_locks(cwd):
                return _run_git(cmd, cwd, capture_output, timeout,
                                _lock_retried=True)
        raise RuntimeError(f"git {' '.join(cmd)} failed:\n{err}")

    out = result.stdout
    if out is None:
        return ""
    return out.strip() if capture_output else None


# ── Init-or-reuse, commit (handles pre-existing .git gracefully) ───────────

def _init_and_commit(folder_path, commit_message, default_branch="main",
                     progress_callback=None, purge_history=False):
    """
    Flexible: works whether folder_path already has a valid .git or not.
      - No .git, or .git present but corrupt/invalid -> fresh `git init -b <branch>`.
      - Valid .git already present -> reuse it. If its current branch
        differs from default_branch, rename to default_branch (keeps
        history; this only matters for a repo that's local-only so far).

    Before committing it also:
      - merges .gitignore rules,
      - excludes any >100 MB file GitHub would reject,
      - un-tracks already-committed files that .gitignore now covers.

    When purge_history=True and the repo already has commits, the history is
    rebuilt as a single clean commit so old oversized blobs are not re-sent.
    """
    if progress_callback:
        progress_callback("Checking repository state...", 8)

    already_valid = _is_valid_git_repo(folder_path)

    if not already_valid:
        if progress_callback:
            progress_callback("Initializing git repository...", 10)
        _run_git(["init", "-b", default_branch], folder_path)
    else:
        if progress_callback:
            progress_callback("Existing git repository detected, reusing it...", 10)
        branch = _current_branch(folder_path)
        if branch and branch != default_branch:
            # Rename local branch to match the intended default branch.
            # Safe: only renames, doesn't touch history or remotes.
            try:
                _run_git(["branch", "-m", branch, default_branch], folder_path)
            except RuntimeError:
                # If rename fails for any reason (e.g. target already exists),
                # fall back to just using whatever branch is currently checked out.
                default_branch = branch

    _ensure_gitignore(folder_path)

    if progress_callback:
        progress_callback("Configuring git user...", 20)

    user = get_github_user()
    email = get_github_email()
    _run_git(["config", "user.name", user or "GitHub Push App"], folder_path)
    _run_git(["config", "user.email", email], folder_path)

    # ── Keep oversized / heavy artifacts out of the push ──────────────────
    if progress_callback:
        progress_callback("Scanning for oversized files...", 24)
    _exclude_oversize_files(folder_path, progress_callback)

    had_commits = _has_commits(folder_path)
    if had_commits:
        _clean_tracked_heavy(folder_path, progress_callback)

    if purge_history and had_commits:
        if progress_callback:
            progress_callback("Rebuilding a clean single-commit history...", 30)
        _rebuild_single_commit(folder_path, default_branch, commit_message, progress_callback)
        if progress_callback:
            progress_callback("Clean history ready.", 55)
        return default_branch

    if progress_callback:
        progress_callback("Staging files (this can take a while for large folders)...", 30)

    add_timeout = _adaptive_add_timeout(folder_path)
    _run_git(["add", "-A"], folder_path, timeout=add_timeout)

    if progress_callback:
        progress_callback("Checking staged files...", 40)

    status = _run_git(["status", "--porcelain"], folder_path)
    if not status:
        try:
            _run_git(["rev-parse", "HEAD"], folder_path)
            if progress_callback:
                progress_callback("No new changes, repository already has commits.", 50)
            return default_branch
        except RuntimeError:
            if progress_callback:
                progress_callback("No changes to commit, creating empty commit...", 45)
            _run_git(["commit", "--allow-empty", "-m", commit_message], folder_path,
                      timeout=COMMIT_TIMEOUT_SECONDS)
            if progress_callback:
                progress_callback("Empty commit successful.", 55)
            return default_branch

    if progress_callback:
        progress_callback(f"Committing with message: '{commit_message}'", 50)

    _run_git(["commit", "-m", commit_message], folder_path, timeout=COMMIT_TIMEOUT_SECONDS)

    if progress_callback:
        progress_callback("Commit successful.", 55)

    return default_branch


# ── GitHub repo creation ─────────────────────────────────────────────────────

def create_github_repo(repo_name, visibility, description="", token=None, progress_callback=None):
    token = token or get_github_token()
    if progress_callback:
        progress_callback("Fetching authenticated user...", 60)

    _get_authenticated_user(token)  # validates token early, fails fast if bad
    data = {
        "name": repo_name,
        "private": visibility == "private",
        "auto_init": False,
        "description": description or "Repository auto-created by GitHubPushApp",
    }

    if progress_callback:
        progress_callback(f"Creating {visibility} repository '{repo_name}' on GitHub...", 70)

    result = _gh_api("POST", "/user/repos", data=data, token=token)

    if progress_callback:
        progress_callback("Repository created.", 80)

    return {
        "url": result.get("html_url"),
        "ssh_url": result.get("ssh_url"),
        "clone_url": result.get("clone_url"),
    }


# ── Push (HTTPS with token, with retry for transient failures) ─────────────

def _auth_url(clone_url, token):
    if clone_url.startswith("https://"):
        parts = clone_url.split("://", 1)
        return f"{parts[0]}://{token}@{parts[1]}"
    return clone_url


_TRANSIENT_ERROR_MARKERS = (
    "could not resolve host",
    "connection reset",
    "connection timed out",
    "timed out",
    "temporary failure",
    "unable to access",
    "the remote end hung up unexpectedly",
    "http 500",
    "http 502",
    "http 503",
)

_NON_RETRYABLE_MARKERS = (
    "rejected",
    "non-fast-forward",
    "fetch first",
    "permission denied",
    "authentication failed",
    "403",
    "404",
)


def _is_transient_push_error(err_text):
    low = err_text.lower()
    if any(m in low for m in _NON_RETRYABLE_MARKERS):
        return False
    return any(m in low for m in _TRANSIENT_ERROR_MARKERS)


def _setup_remote_and_push(folder_path, repo_info, branch="main",
                            progress_callback=None, token=None, force=True):
    clone_url = repo_info.get("clone_url")
    if not clone_url:
        raise RuntimeError("No clone URL available")

    token = token or get_github_token()
    if not token:
        raise RuntimeError("GitHub token missing for authentication")

    auth_url = _auth_url(clone_url, token)

    if progress_callback:
        progress_callback("Setting remote origin...", 85)

    try:
        _run_git(["remote", "remove", "origin"], folder_path)
    except RuntimeError:
        pass  # no origin existed yet, fine

    _run_git(["remote", "add", "origin", auth_url], folder_path)

    cmd = ["git", "push", "-u", "origin", branch]
    if force:
        cmd.insert(2, "--force")

    attempt = 0
    last_err = None
    while attempt <= PUSH_MAX_RETRIES:
        attempt += 1
        if progress_callback:
            label = f"Pushing to {branch} branch..." if attempt == 1 else \
                    f"Retrying push (attempt {attempt}/{PUSH_MAX_RETRIES + 1})..."
            progress_callback(label, 90)

        try:
            result = subprocess.run(
                cmd,
                cwd=folder_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PUSH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            last_err = (f"Push timed out after {PUSH_TIMEOUT_SECONDS}s. "
                        f"This is usually a slow/unstable connection or a very large payload.")
            if attempt <= PUSH_MAX_RETRIES:
                time.sleep(PUSH_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise RuntimeError(last_err)

        if result.returncode == 0:
            if progress_callback:
                progress_callback("Push completed successfully.", 100)
            return result.stdout.strip() if result.stdout else ""

        err = result.stderr.strip() if result.stderr else "(no error output)"
        last_err = err

        if _is_transient_push_error(err) and attempt <= PUSH_MAX_RETRIES:
            if progress_callback:
                progress_callback(f"Push failed transiently, retrying in "
                                   f"{PUSH_RETRY_BACKOFF_SECONDS * attempt}s...", 90)
            time.sleep(PUSH_RETRY_BACKOFF_SECONDS * attempt)
            continue

        raise RuntimeError(f"Push failed:\n{err}")

    raise RuntimeError(f"Push failed after {PUSH_MAX_RETRIES + 1} attempts:\n{last_err}")


# ── High-level push workflow (create NEW repo) ──────────────────────────────

def push_project(
    folder_path,
    repo_name=None,
    visibility="public",
    commit_message=None,
    description="",
    default_branch="main",
    token=None,
    progress_callback=None,
):
    try:
        _check_git()
    except Exception as e:
        if progress_callback:
            progress_callback(str(e), -1)
        return {
            "success": False, "repo_url": None, "repo_name": "unknown",
            "branch": default_branch, "visibility": visibility,
            "message": str(e), "push_output": None, "conflict": False,
        }

    folder = validate_project_folder(folder_path)
    if progress_callback:
        progress_callback(f"Validated folder: {folder}", 5)

    if not repo_name:
        repo_name = os.path.basename(folder)
    repo_name = _format_repo_name(repo_name)
    if not repo_name:
        raise ValueError("Could not derive a valid repository name.")

    if not commit_message:
        commit_message = f"Initial commit — {repo_name}"

    try:
        actual_branch = _init_and_commit(folder, commit_message, default_branch,
                                         progress_callback, purge_history=True)
        repo_info = create_github_repo(repo_name, visibility, description, token, progress_callback)
        push_output = _setup_remote_and_push(folder, repo_info, actual_branch, progress_callback, token)

        if progress_callback:
            progress_callback(f"Successfully pushed to {repo_info['url']}", 100)

        return {
            "success": True,
            "repo_url": repo_info["url"],
            "repo_name": repo_name,
            "branch": actual_branch,
            "visibility": visibility,
            "message": f"Successfully pushed to:\n{repo_info['url']}",
            "push_output": push_output,
            "conflict": False,
        }
    except Exception as e:
        error_msg = str(e)
        if "name already exists" in error_msg:
            suggested = _format_repo_name(repo_name + "_1")
            return {
                "success": False, "repo_url": None, "repo_name": repo_name,
                "branch": default_branch, "visibility": visibility,
                "message": f"Repository '{repo_name}' already exists on GitHub.",
                "push_output": None, "conflict": True, "suggested_name": suggested,
            }
        return {
            "success": False, "repo_url": None, "repo_name": repo_name,
            "branch": default_branch, "visibility": visibility,
            "message": error_msg, "push_output": None, "conflict": False,
        }


# ── High-level UPDATE workflow (push into EXISTING repo) ───────────────────

def update_project(
    folder_path,
    repo_name,
    commit_message=None,
    force=True,
    confirmed=False,
    token=None,
    progress_callback=None,
):
    try:
        _check_git()
    except Exception as e:
        if progress_callback:
            progress_callback(str(e), -1)
        return {
            "success": False, "needs_confirm": False, "name_mismatch": False,
            "repo_url": None, "repo_name": repo_name or "unknown", "folder_name": "",
            "branch": "main", "message": str(e), "push_output": None,
        }

    if not repo_name:
        raise ValueError("repo_name is required for update_project().")

    folder = validate_project_folder(folder_path)
    folder_name = os.path.basename(folder)
    if progress_callback:
        progress_callback(f"Validated folder: {folder}", 5)

    mismatch = not names_match(folder, repo_name)
    if mismatch and not confirmed:
        if progress_callback:
            progress_callback(
                f"Folder '{folder_name}' does not match repo '{repo_name}' "
                f"— waiting for confirmation.", 8)
        return {
            "success": False, "needs_confirm": True, "name_mismatch": True,
            "repo_url": None, "repo_name": repo_name, "folder_name": folder_name,
            "branch": "main",
            "message": (
                f"The selected folder '{folder_name}' does not match the "
                f"GitHub repository '{repo_name}'.\n"
                f"Confirm to update '{repo_name}' with this folder's contents."),
            "push_output": None,
        }

    token = token or get_github_token()

    try:
        if progress_callback:
            progress_callback(f"Fetching repository '{repo_name}'...", 12)
        info = get_repo_info(repo_name, token=token)
        branch = info["default_branch"]

        if not commit_message:
            commit_message = f"Update — {repo_name}"
        actual_branch = _init_and_commit(folder, commit_message, branch,
                                         progress_callback, purge_history=force)

        push_output = _setup_remote_and_push(
            folder,
            {"clone_url": info["clone_url"]},
            branch=actual_branch,
            progress_callback=progress_callback,
            token=token,
            force=force,
        )

        if progress_callback:
            progress_callback(f"Successfully updated {info['html_url']}", 100)

        return {
            "success": True, "needs_confirm": False, "name_mismatch": mismatch,
            "repo_url": info["html_url"], "repo_name": repo_name,
            "folder_name": folder_name, "branch": actual_branch,
            "message": f"Successfully updated:\n{info['html_url']}",
            "push_output": push_output,
        }

    except Exception as e:
        error_msg = str(e)
        hint = ""
        low = error_msg.lower()
        if ("gh001" in low
                or "exceeds github's file size limit" in low
                or "larger than github's recommended" in low):
            hint = ("\n\nGitHub rejected this push because one or more files "
                    "exceed its 100 MB limit. The tool now auto-excludes files "
                    "over 100 MB; if a file is still tracked from an earlier "
                    "commit, re-run the push so the history gets cleaned.")
        elif "rejected" in low or "non-fast-forward" in low or "fetch first" in low:
            if force:
                hint = ("\n\nThe remote rejected the push even with force. "
                        "If the message mentions large files, they were "
                        "excluded automatically -- re-run the push. Otherwise "
                        "check the repo's branch protection rules.")
            else:
                hint = ("\n\nThe remote rejected the push. Re-run the update; "
                        "force push is on by default.")
        elif "timed out" in low:
            hint = ("\n\nTry re-running — this folder may just need a larger "
                    "timeout, or check analyze_folder() output for unusually "
                    "large files/directories to exclude via .gitignore.")
        return {
            "success": False, "needs_confirm": False, "name_mismatch": mismatch,
            "repo_url": None, "repo_name": repo_name, "folder_name": folder_name,
            "branch": "main", "message": f"{error_msg}{hint}", "push_output": None,
        }


# ── Dry-run ──────────────────────────────────────────────────────────────────

def dry_run(folder_path, repo_name=None):
    _check_git()
    folder = validate_project_folder(folder_path)
    if not repo_name:
        repo_name = os.path.basename(folder)
    repo_name_clean = _format_repo_name(repo_name)
    stats = analyze_folder(folder)
    return {
        "valid": True,
        "folder": folder,
        "repo_name": repo_name_clean,
        "file_count": stats["file_count"],
        "total_mb": stats["total_mb"],
        "heavy_dirs_present": stats["heavy_dirs_present"],
        "large_files": stats["large_files"],
        "already_git_repo": _is_valid_git_repo(folder),
    }


def auto_prepare_git(folder_path):
    """
    Initialize git + ensure a .gitignore for a folder on demand (e.g.
    right after the user selects the folder in the UI). Safe to call
    repeatedly:
      - already a valid git repo  -> skips `git init`
      - .gitignore present        -> merges missing patterns, never clobbers

    Returns a small summary dict:
      { folder, initialized, gitignore_created_or_updated }
    """
    _check_git()
    folder = validate_project_folder(folder_path)
    already = _is_valid_git_repo(folder)
    if not already:
        _run_git(["init", "-b", "main"], folder)
    made_gitignore = _ensure_gitignore(folder)
    return {
        "folder": folder,
        "initialized": not already,
        "gitignore_created_or_updated": made_gitignore,
    }