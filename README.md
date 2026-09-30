# GitHub Push App

A professional PyQt5 GUI application to push local project folders to GitHub.

Push any folder to a new GitHub repository with one click — public or private.

## Features

- **Select any local project folder** and push it to a new GitHub repo
- **Public / Private** visibility toggle
- **Custom repo name** (auto-derived from folder name)
- **Custom commit message** and repo description
- **.env configuration** for GITHUB_TOKEN, username, and email
- **Dry-run mode** — validate before pushing
- **Live operation log** with progress bar
- **Clean, draggable frameless UI** (consistent with other desktop tools)

## Setup

### 1. Install dependencies

```bash
cd ~/Desktop/GitHubPushApp
pip install -r requirements.txt
```

### 2. Get a GitHub Token

1. Go to https://github.com/settings/tokens
2. Click **Generate new token (classic)**
3. Enable scopes: `repo` (full control) — required for private repos
4. Copy the generated token

### 3. Configure

Set your token in one of two ways:

**Option A — .env file** (recommended):
```bash
# Create ~/Desktop/.env or a .env in the app folder
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
GITHUB_USER=your_username
GITHUB_EMAIL=your_email@example.com
```

**Option B — GUI config panel**:
Open the app, fill in the fields under "GitHub Configuration (.env)", and click "Save .env".

## Usage

```bash
cd ~/Desktop/GitHubPushApp
python main.py
```

1. Click **Select Project Folder** and choose your project
2. Adjust repo name, visibility, commit message as needed
3. Click **Test Connection (Dry Run)** to validate
4. Click **Push to GitHub** — the repo is created and pushed

After a successful push, the repo URL opens in your browser.

## Build as .app

```bash
cd ~/Desktop/GitHubPushApp
pip install pyinstaller
pyinstaller --windowed --onefile --name GitHubPush \
  --add-data "static:static" \
  --icon static/favicon.ico \
  main.py

