# SyncBridge

A macOS menu bar app that keeps local folders and cloud folders (OneDrive, iCloud Drive,
Dropbox, Google Drive — anything under `~/Library/CloudStorage`) in sync — safely.

SyncBridge is for setups where you want your working files in a normal local folder
(fast, works with every tool, no placeholder files) and a mirrored copy in the cloud.
It runs a two-way sync on a schedule, propagates edits, renames and small batches of
deletions, and stops to ask whenever something could lose data.

## Features

- **Sync variants per pair**, as in FreeFileSync: *Two way*, *Mirror*, *Update* (copy new
  and changed files, never delete) and *Custom* (a direction for each kind of change).
- **Compare & Preview**: see what the next sync would do and change the action per file
  (copy the other way, delete, skip) before synchronizing.
- **Real-time sync** (optional): a pair syncs shortly after its files change.
- **Filters per pair**: include/exclude patterns, file size and "changed within N days".
  Filtered files are left alone on both sides, never deleted.
- **Versioning** of every deleted *and* overwritten file: folder per sync, time stamp in the
  file name or replace; maximum age, maximum and minimum number of versions per file.
  Alternatively move to the macOS Trash or delete permanently.
- **Fail-safe copies**: files are copied to a temporary name first, so an interrupted copy
  never damages the target.
- **Conflict rules** (two way): ask, keep the newer version, always keep local, or always
  keep the cloud version. The overwritten version is versioned first.
- **Conflict window** with modification dates, sizes, a short diff for text files and
  bulk actions ("Keep newer for all").
- **Suggestions without AI**: SyncBridge reads the text, last author and revision of
  Word, Excel and PowerPoint files and of text files. If one version contains everything
  in the other plus more, it is suggested with high confidence; if both sides have their
  own changes, it tells you a merge is needed.
- **Optional AI help**: suggests which version to keep, explains what each side changed and
  proposes merges for text files. A merge is only applied after you review and approve it; both
  old versions are kept.
  - **Your instructions**: tell the AI how to judge content ("ignore formatting-only changes",
    "never drop a requirement") and try them on the open conflicts without changing anything.
  - **Learns from corrections**: when you choose differently, SyncBridge asks why and suggests a
    new instruction for you to approve.
  - **Automatic decisions only after earning them**: you can turn them on only when the AI agreed
    with every one of at least 20 of your decisions in the last 30 days, and only after confirming
    the agreement rates. One different choice or changed instructions switch them off. Deletions,
    name conflicts and merges always wait for you.
  Use the plan you already pay for, or a key:
  - **ChatGPT plan** (Go, Plus, Pro) via OpenAI's "Sign in with ChatGPT" for open-source apps
  - **Claude plan** (Pro, Max) via your own signed-in Claude Code, run with all tools off;
    several accounts in `~/.claude*` folders can be picked
  - Apple's on-device model, or an Anthropic, OpenAI or OpenRouter API key

  Sign-ins and keys are stored in the Keychain. The subscription approach follows the
  open-source PDF reader [Folio](https://github.com/Forward-Future/folio).
- **Name conflicts** (names that differ only by capitalisation or by how a letter like
  "ä" is encoded) are settled with Keep Local / Keep OneDrive: the kept side's content
  and spelling go to both sides.
- **Safe deletions**: up to *N* new deletions per run are applied automatically; larger
  batches wait in *Pending Deletions*, where you can restore or delete by folder.
  Cloud deletions additionally land in the provider's recycle bin.
- **Rename detection**, including case-only folder renames on case-insensitive volumes.
- **Empty folders** are synced too (created and removed like files). A folder is only
  removed while it holds no files, and deleting the last file of a folder keeps the folder.
- **Never mistakes an outage for a deletion**: a missing or unreadable root, or a folder
  that cannot be listed, stops that pair. Single unreadable files are left untouched.
- **Exclusions** for build output, caches, app bundles and anything you configure.
- **Run a command after each sync** (gets `$SYNCBRIDGE_STATUS`).

## Requirements

- macOS 13 or later
- Nothing else to run it: the app ships its own Python
  ([python-build-standalone](https://github.com/astral-sh/python-build-standalone), standard
  library only). The build downloads it once and checks its SHA-256 checksum.
- To build: Xcode Command Line Tools (`xcode-select --install`) and internet access on the first build
- To use the engine without the app: Python 3.9+
- Optional: [`terminal-notifier`](https://github.com/julienXX/terminal-notifier) for notifications with the app icon

## Install

```bash
git clone https://github.com/sternbj/syncbridge.git
cd syncbridge
scripts/install.sh
```

This builds `SyncBridge.app`, copies it to `~/Applications`, creates
`~/Library/Application Support/SyncBridge/config.json` from `config.example.json`
and starts the app at login. Open **Settings…** from the menu bar icon to add your folder pairs.

## Configuration

Settings are stored in `~/Library/Application Support/SyncBridge/config.json`
(override with the `SYNCBRIDGE_CONFIG` environment variable). Everything can be edited
in the app; the file format is:

| Key | Meaning |
|---|---|
| `pairs` | List of folder pairs, see below |
| `conflict_rule` | `ask`, `newer`, `local` or `onedrive` |
| `folder_rules` | Fixed rules per folder, checked first: `[{"pattern": "Clients/**", "rule": "protect"}]`. Rules: `protect` (never delete; a deleted file is copied back), `keep_local`, `keep_onedrive`, `keep_newer`, `keep_older` (decide conflicts). The first match counts |
| `author_priority` | Names, highest rank first. When both versions of a Word, Excel or PowerPoint file were last saved by listed people, the higher-ranked one's version is kept |
| `auto_delete_limit` | New deletions applied automatically per run (0 = always review) |
| `deletion_handling` | `versioning` (default), `trash` (macOS Trash) or `permanent` |
| `versioning_style` | `timestamp_folder` (default), `timestamp_file` or `replace` |
| `trash_dir` | Versioning folder (default: `<state_dir>/trash`) |
| `trash_retention_days` | Remove versions older than this (0 = keep forever) |
| `versioning_max_count` / `versioning_min_count` | Versions kept per file (0 = no limit) |
| `run_after_sync` | Shell command run after each sync |
| `sync_interval_minutes` | Schedule of the menu bar app |
| `exclude_dirs` | Folder names excluded anywhere (wildcards allowed) |
| `exclude_path_patterns` | Path patterns relative to a pair, e.g. `Projects/*/output` |
| `exclude_names` | File names excluded anywhere |
| `staged_exclude_path_patterns` | Exclusions activated only after their existing files were cleaned up in *Pending Deletions* |
| `state_dir` | Where the sync state lives (default: `…/SyncBridge/state`) |

A pair is `{name, local, onedrive, enabled}` plus optional sync options:

```json
{
  "name": "Photos",
  "local": "~/Pictures/Export",
  "onedrive": "~/Library/CloudStorage/OneDrive-Personal/Photos",
  "variant": "update",
  "direction": "to_onedrive",
  "filter": {"exclude": ["*.tmp", "Drafts/**"], "max_size_bytes": 2000000000, "max_age_days": 0}
}
```

`variant` is `two-way` (default), `mirror`, `update` or `custom`. Mirror and update need a
`direction` (`to_onedrive` or `to_local`). Custom needs `directions` with a value
(`to_onedrive`, `to_local` or `none`) for each of `local_new`, `local_changed`,
`local_deleted`, `onedrive_new`, `onedrive_changed` and `onedrive_deleted`.

## Command line

The engine is a single Python file and can be used without the app:

```bash
python3 engine/syncbridge.py --dry-run --verbose   # show what would happen
python3 engine/syncbridge.py --auto-delete         # sync like the app does
python3 engine/syncbridge.py --status              # last recorded state
python3 engine/syncbridge.py --preview             # planned actions as JSON
python3 engine/syncbridge.py --auto-delete --pairs Photos   # sync one pair
python3 engine/syncbridge.py --list-conflicts      # JSON, used by the app
python3 engine/syncbridge.py --help
```

## Development

```bash
python3 -m unittest discover -s engine/tests   # engine tests
scripts/build-app.sh                           # build build/SyncBridge.app
```

The app (`app/`, SwiftUI) only calls the engine's JSON commands; all sync logic and its
safety rules live in `engine/syncbridge.py` and are covered by the tests.

## Feedback and contributing

Found a bug or have an idea? Use **Help › Report a Bug…** or **Suggest an Idea…** in the app, or open an
[issue](https://github.com/sternbj/syncbridge/issues/new/choose). Pull requests are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

Copyright (c) 2026 sternbj.

SyncBridge is licensed under the [GNU General Public License, version 3 only](LICENSE)
(`GPL-3.0-only`). See [third-party notices](THIRD_PARTY_NOTICES.md) for attribution and separate licenses.
