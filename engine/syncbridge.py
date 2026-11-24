#!/usr/bin/env python3
"""SyncBridge engine: safe two-way sync between local folders and OneDrive.

Each configured pair mirrors a local folder with a folder inside the OneDrive
(FileProvider) directory. The engine keeps a per-file baseline, propagates
edits, renames and small batches of deletions, records conflicts, and holds
anything risky for review. The SyncBridge menu bar app drives it through the
command line and its JSON output.
"""

from __future__ import annotations

import argparse
import difflib
import fcntl
import fnmatch
import hashlib
import json
import lzma
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import re
import zipfile
import zlib
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path


APP_NAME = "SyncBridge"
APP_BUNDLE_ID = "io.github.sternbj.syncbridge"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
DEFAULT_CONFIG_FILE = APP_SUPPORT_DIR / "config.json"
DEFAULT_STATE_DIR = APP_SUPPORT_DIR / "state"
# The app passes its icon so AppleScript dialogs (CLI --interactive) can show it.
APP_ICON = Path(os.environ.get("SYNCBRIDGE_APP_ICON", "/nonexistent"))

DEFAULT_EXCLUDE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
DEFAULT_EXCLUDE_PREFIXES = ("~$", ".~lock.")
TEMP_COPY_SUFFIX = ".syncbridge-tmp"
DEFAULT_EXCLUDE_SUFFIXES = (".tmp", ".swp", ".swo", ".crdownload", ".part", TEMP_COPY_SUFFIX)
DEFAULT_EXCLUDE_PATH_PATTERNS = (
    "**/.env",
    "**/.env.*",
    "**/.claude/settings.local.json",
    "**/*.db-shm",
    "**/*.db-wal",
    "**/*.sqlite-shm",
    "**/*.sqlite-wal",
    "**/*.tsbuildinfo",
    "**/.wrangler/**",
    "**/.docx-update/**",
    # App bundles are directories that OneDrive turns into unreadable package files.
    "**/*.app",
    "**/*.framework",
)
DEFAULT_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".next",
    ".next-*",
    ".local-test",
    "clang-cache",
    ".nuxt",
    "bower_components",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "*.egg-info",
    "dist",
    "build",
    "out",
    ".output",
    ".idea",
    ".vscode",
    ".Trash-1000",
    ".cache",
    ".parcel-cache",
    ".turbo",
    ".svelte-kit",
}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".sh",
    ".bash",
    ".zsh",
    ".csv",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".vue",
    ".svelte",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".sql",
    ".env",
    ".gitignore",
    ".dockerignore",
    ".editorconfig",
    ".bak",
    ".log",
}
SUMMARY_PATH_LIMIT = 100
# Unreadable single files are frozen instead of aborting the pair, unless so many
# fail that the location itself is probably unhealthy (e.g. OneDrive offline).
UNREADABLE_LIMIT_RATIO = 0.01
# Local deletions are moved into a per-run trash batch and kept this long.
# OneDrive deletions go to the OneDrive recycle bin instead.
TRASH_RETENTION_DAYS = 30
DEFAULT_SYNC_INTERVAL_MINUTES = 60
# ask: keep conflicts open for review; newer/local/onedrive: resolve automatically;
# safe: resolve only where no content can be lost (see safe_decision), keep the rest open.
CONFLICT_RULES = ("ask", "safe", "newer", "local", "onedrive")
DEFAULT_AUTO_DELETE_LIMIT = 50

# Sync variants as in FreeFileSync. Every variant is a mapping from the six
# change categories (relative to the last synchronized state) to a direction.
CHANGE_CATEGORIES = (
    "local_new",
    "local_changed",
    "local_deleted",
    "onedrive_new",
    "onedrive_changed",
    "onedrive_deleted",
)
DIRECTIONS = ("to_onedrive", "to_local", "none")
SYNC_VARIANTS = ("two-way", "mirror", "update", "custom")
DELETION_HANDLINGS = ("versioning", "trash", "permanent")
# Fixed rules per folder: protect = never delete (restore instead); keep_* decides conflicts.
FOLDER_RULES = ("protect", "keep_local", "keep_onedrive", "keep_newer", "keep_older")
VERSIONING_STYLES = ("timestamp_folder", "timestamp_file", "replace")
RUN_ID_PATTERN = r"\d{4}-\d{2}-\d{2}_\d{6}"
RUN_ID = datetime.now().strftime("%Y-%m-%d_%H%M%S")


@dataclass(frozen=True)
class PairFilter:
    """Per-pair filter. Sizes in bytes and age in days; 0 means no limit."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    min_size_bytes: int = 0
    max_size_bytes: int = 0
    max_age_days: int = 0

    def excludes_path(self, rel_path: str, is_dir: bool = False) -> bool:
        if any(rel_path_matches_pattern(rel_path, pattern) for pattern in self.exclude):
            return True
        if is_dir or not self.include:
            return False
        return not any(rel_path_matches_pattern(rel_path, pattern) for pattern in self.include)

    def excludes_file(self, stat: os.stat_result) -> bool:
        if self.min_size_bytes and stat.st_size < self.min_size_bytes:
            return True
        if self.max_size_bytes and stat.st_size > self.max_size_bytes:
            return True
        return bool(self.max_age_days) and stat.st_mtime < time.time() - self.max_age_days * 86400


@dataclass(frozen=True)
class SyncPair:
    name: str
    local_dir: Path
    onedrive_dir: Path
    variant: str = "two-way"
    direction: str = "to_onedrive"
    directions: dict | None = field(default=None, compare=False, hash=False)
    filter: PairFilter = PairFilter()

    @property
    def key(self) -> str:
        return self.name

    @property
    def label(self) -> str:
        return f"{self.name}: {self.local_dir} <-> {self.onedrive_dir}"


@dataclass(frozen=True)
class FolderRule:
    pattern: str
    rule: str


@dataclass(frozen=True)
class StagedExclusion:
    pair: str
    label: str
    pattern: str

    @property
    def root(self) -> str:
        return self.pattern.removesuffix("/**").rstrip("/")


@dataclass(frozen=True)
class PendingDeletion:
    pair: str
    action: str
    file: str
    expected_hash: str | None = None
    expected_signature: str | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.pair, self.action, self.file)


@dataclass
class DeletionGroup:
    pair: str
    action: str
    path: str
    label: str
    recommended: bool
    items: list[PendingDeletion]


@dataclass
class AppConfig:
    config_path: Path
    state_dir: Path
    state_file: Path
    log_file: Path
    lock_file: Path
    conflicts_file: Path
    summary_file: Path
    exclusions_file: Path
    pending_deletions_file: Path
    pairs: list[SyncPair]
    exclude_names: set[str]
    exclude_prefixes: tuple[str, ...]
    exclude_suffixes: tuple[str, ...]
    exclude_dirs: set[str]
    exclude_path_patterns: tuple[str, ...]
    staged_exclusions: tuple[StagedExclusion, ...]
    trash_dir: Path | None = None
    auto_delete_limit: int = DEFAULT_AUTO_DELETE_LIMIT
    trash_retention_days: int = TRASH_RETENTION_DAYS
    sync_interval_minutes: int = DEFAULT_SYNC_INTERVAL_MINUTES
    conflict_rule: str = "ask"
    deletion_handling: str = "versioning"
    versioning_style: str = "timestamp_folder"
    versioning_max_count: int = 0
    versioning_min_count: int = 0
    run_after_sync: str = ""
    folder_rules: tuple[FolderRule, ...] = ()
    author_priority: tuple[str, ...] = ()


def effective_directions(pair: SyncPair) -> dict[str, str]:
    """Return the direction for each change category of a pair's variant."""

    if pair.variant == "custom":
        return {category: (pair.directions or {}).get(category, "none") for category in CHANGE_CATEGORIES}
    if pair.variant == "two-way":
        return {
            category: "to_onedrive" if category.startswith("local_") else "to_local"
            for category in CHANGE_CATEGORIES
        }
    source = "local_" if pair.direction == "to_onedrive" else "onedrive_"
    directions = {}
    for category in CHANGE_CATEGORIES:
        if pair.variant == "mirror":
            directions[category] = pair.direction
        else:  # update: only new and changed files of the source side
            copies = category.startswith(source) and not category.endswith("_deleted")
            directions[category] = pair.direction if copies else "none"
    return directions


def pair_options_from_entry(entry: dict) -> dict:
    """Parse and validate the sync options of one config pair entry."""

    options: dict = {}
    variant = entry.get("variant", "two-way")
    if variant not in SYNC_VARIANTS:
        raise ValueError(f"variant must be one of {', '.join(SYNC_VARIANTS)}")
    options["variant"] = variant
    if variant in ("mirror", "update"):
        direction = entry.get("direction")
        if direction not in ("to_onedrive", "to_local"):
            raise ValueError(f"{variant} needs direction 'to_onedrive' or 'to_local'")
        options["direction"] = direction
    if variant == "custom":
        directions = entry.get("directions")
        if (
            not isinstance(directions, dict)
            or set(directions) != set(CHANGE_CATEGORIES)
            or not all(value in DIRECTIONS for value in directions.values())
        ):
            raise ValueError(
                f"custom directions need {', '.join(CHANGE_CATEGORIES)}, "
                f"each one of {', '.join(DIRECTIONS)}"
            )
        options["directions"] = dict(directions)
    raw_filter = entry.get("filter") or {}
    if not isinstance(raw_filter, dict):
        raise ValueError("filter must be an object")
    filter_args: dict = {}
    for key in ("include", "exclude"):
        value = raw_filter.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"filter {key} must be a list of text entries")
        filter_args[key] = tuple(value)
    for key in ("min_size_bytes", "max_size_bytes", "max_age_days"):
        value = raw_filter.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"filter {key} must be a whole number of at least 0")
        filter_args[key] = value
    options["filter"] = PairFilter(**filter_args)
    return options


def choice_error(raw: dict, key: str, choices: tuple[str, ...], default: str) -> str | None:
    if raw.get(key, default) not in choices:
        return f"{key} must be one of {', '.join(choices)}"
    return None


class ScanIncompleteError(RuntimeError):
    """Raised when a directory scan could not inspect every included file."""

    def __init__(self, base_dir: Path, errors: list[str]):
        self.base_dir = base_dir
        self.errors = errors
        preview = "; ".join(errors[:3])
        if len(errors) > 3:
            preview += f"; ... and {len(errors) - 3} more"
        super().__init__(f"Incomplete scan of {base_dir}: {preview}")


CONFIG: AppConfig | None = None


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def load_activated_exclusion_patterns(
    exclusions_file: Path,
    allowed_patterns: set[str],
) -> tuple[str, ...]:
    if not exclusions_file.exists():
        return ()
    with open(exclusions_file, encoding="utf-8") as handle:
        raw = json.load(handle)
    patterns = raw.get("patterns", []) if isinstance(raw, dict) else []
    if not isinstance(patterns, list) or not all(
        isinstance(pattern, str) for pattern in patterns
    ):
        raise ValueError(f"Invalid activated exclusions file: {exclusions_file}")
    return tuple(
        pattern for pattern in dict.fromkeys(patterns) if pattern in allowed_patterns
    )


def default_config_path() -> Path:
    override = os.environ.get("SYNCBRIDGE_CONFIG")
    return expand_path(override) if override else DEFAULT_CONFIG_FILE


def load_config(config_path: str | None = None) -> AppConfig:
    path = default_config_path() if config_path is None else expand_path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)

    pairs = []
    for entry in raw.get("pairs", []):
        if not entry.get("enabled", True):
            continue
        name = entry["name"].strip()
        try:
            options = pair_options_from_entry(entry)
        except ValueError as exc:
            raise ValueError(f"Pair {name}: {exc}") from exc
        pairs.append(
            SyncPair(
                name=name,
                local_dir=expand_path(entry["local"]),
                onedrive_dir=expand_path(entry["onedrive"]),
                **options,
            )
        )

    if not pairs:
        raise ValueError(f"No enabled sync pairs in config: {path}")

    state_dir = expand_path(raw["state_dir"]) if raw.get("state_dir") else DEFAULT_STATE_DIR
    exclude_names = set(DEFAULT_EXCLUDE_NAMES)
    exclude_names.update(raw.get("exclude_names", []))
    exclude_prefixes = tuple(
        dict.fromkeys((*DEFAULT_EXCLUDE_PREFIXES, *raw.get("exclude_prefixes", [])))
    )
    exclude_suffixes = tuple(
        dict.fromkeys((*DEFAULT_EXCLUDE_SUFFIXES, *raw.get("exclude_suffixes", [])))
    )
    exclude_dirs = set(DEFAULT_EXCLUDE_DIRS)
    exclude_dirs.update(raw.get("exclude_dirs", []))
    staged_exclusions = tuple(
        StagedExclusion(
            pair=entry["pair"],
            label=entry["label"],
            pattern=entry["pattern"],
        )
        for entry in raw.get("staged_exclude_path_patterns", [])
    )
    for exclusion in staged_exclusions:
        if not exclusion.pattern.endswith("/**") or not exclusion.root:
            raise ValueError(
                f"Staged exclusion must end with '/**': {exclusion.pattern}"
            )
        if exclusion.pair not in {pair.key for pair in pairs}:
            raise ValueError(
                f"Staged exclusion references unknown pair: {exclusion.pair}"
            )
    for error in [*config_choice_errors(raw), *rule_config_errors(raw)]:
        raise ValueError(error)
    exclusions_file = state_dir / "activated-exclusions.json"
    activated_patterns = load_activated_exclusion_patterns(
        exclusions_file,
        {exclusion.pattern for exclusion in staged_exclusions},
    )
    exclude_path_patterns = tuple(
        dict.fromkeys(
            (
                *DEFAULT_EXCLUDE_PATH_PATTERNS,
                *raw.get("exclude_path_patterns", []),
                *activated_patterns,
            )
        )
    )

    return AppConfig(
        config_path=path,
        state_dir=state_dir,
        state_file=state_dir / "state.json",
        log_file=state_dir / "sync.log",
        lock_file=state_dir / "sync.lock",
        conflicts_file=state_dir / "conflicts.json",
        summary_file=state_dir / "last-sync-summary.txt",
        exclusions_file=exclusions_file,
        pending_deletions_file=state_dir / "pending-deletions.json",
        pairs=pairs,
        exclude_names=exclude_names,
        exclude_prefixes=exclude_prefixes,
        exclude_suffixes=exclude_suffixes,
        exclude_dirs=exclude_dirs,
        exclude_path_patterns=exclude_path_patterns,
        staged_exclusions=staged_exclusions,
        trash_dir=expand_path(raw["trash_dir"]) if raw.get("trash_dir") else None,
        auto_delete_limit=int(raw.get("auto_delete_limit", DEFAULT_AUTO_DELETE_LIMIT)),
        trash_retention_days=int(raw.get("trash_retention_days", TRASH_RETENTION_DAYS)),
        sync_interval_minutes=int(
            raw.get("sync_interval_minutes", DEFAULT_SYNC_INTERVAL_MINUTES)
        ),
        conflict_rule=raw.get("conflict_rule", "ask"),
        deletion_handling=raw.get("deletion_handling", "versioning"),
        versioning_style=raw.get("versioning_style", "timestamp_folder"),
        versioning_max_count=int(raw.get("versioning_max_count", 0)),
        versioning_min_count=int(raw.get("versioning_min_count", 0)),
        run_after_sync=str(raw.get("run_after_sync", "")),
        folder_rules=tuple(
            FolderRule(entry["pattern"].strip(), entry["rule"]) for entry in raw.get("folder_rules", [])
        ),
        author_priority=tuple(name.strip() for name in raw.get("author_priority", [])),
    )


def rule_config_errors(raw: dict) -> list[str]:
    """Problems with folder_rules and author_priority, as readable messages."""

    errors: list[str] = []
    rules = raw.get("folder_rules", [])
    if not isinstance(rules, list):
        return ["folder_rules must be a list"]
    for index, entry in enumerate(rules, start=1):
        pattern = entry.get("pattern") if isinstance(entry, dict) else None
        rule = entry.get("rule") if isinstance(entry, dict) else None
        if not isinstance(pattern, str) or not pattern.strip():
            errors.append(f"folder_rules {index}: a folder pattern is required")
        if rule not in FOLDER_RULES:
            errors.append(f"folder_rules {index}: rule must be one of {', '.join(FOLDER_RULES)}")
    authors = raw.get("author_priority", [])
    if not isinstance(authors, list) or not all(isinstance(name, str) and name.strip() for name in authors):
        errors.append("author_priority must be a list of names, none empty")
    return errors


def config_choice_errors(raw: dict) -> list[str]:
    checks = (
        ("conflict_rule", CONFLICT_RULES, "ask"),
        ("deletion_handling", DELETION_HANDLINGS, "versioning"),
        ("versioning_style", VERSIONING_STYLES, "timestamp_folder"),
    )
    return [error for key, choices, default in checks if (error := choice_error(raw, key, choices, default))]


def require_config() -> AppConfig:
    if CONFIG is None:
        raise RuntimeError("Config not initialized")
    return CONFIG


def md5_file(filepath: Path) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def should_exclude(name: str) -> bool:
    config = require_config()
    if name in config.exclude_names:
        return True
    for prefix in config.exclude_prefixes:
        if name.startswith(prefix):
            return True
    for suffix in config.exclude_suffixes:
        if name.endswith(suffix):
            return True
    return False


def should_exclude_directory(name: str) -> bool:
    config = require_config()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in config.exclude_dirs)


def rel_path_matches_pattern(rel_path: str, pattern: str) -> bool:
    normalized = rel_path.replace(os.sep, "/")
    candidates = {normalized, normalized.rstrip("/"), normalized.rstrip("/") + "/"}
    patterns = {pattern}
    if pattern.startswith("**/"):
        patterns.add(pattern[3:])
    return any(
        fnmatch.fnmatchcase(candidate, candidate_pattern)
        for candidate in candidates
        for candidate_pattern in patterns
    )


def should_exclude_rel_path(rel_path: str) -> bool:
    config = require_config()
    return any(
        rel_path_matches_pattern(rel_path, pattern)
        for pattern in config.exclude_path_patterns
    )


def staged_exclusion_for_path(
    pair_name: str,
    rel_path: str,
) -> StagedExclusion | None:
    config = require_config()
    for exclusion in config.staged_exclusions:
        if exclusion.pair == pair_name and rel_path_matches_pattern(
            rel_path, exclusion.pattern
        ):
            return exclusion
    return None


def stat_signature(stat: os.stat_result) -> str:
    """Return metadata that changes for normal edits and FileProvider replacements."""

    return f"{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_size}:{stat.st_ino}"


def file_signature(filepath: Path) -> str:
    return stat_signature(filepath.stat())


def scan_directory(
    base_dir: Path,
) -> tuple[dict[str, str], dict[str, float], dict[str, str]]:
    checksums, mtimes, signatures, _ = fast_scan_directory(
        base_dir, {}, {}, force_full=True
    )
    return checksums, mtimes, signatures


def fast_scan_directory(
    base_dir: Path,
    prev_checksums: dict[str, str],
    prev_signatures: dict[str, str],
    force_full: bool = False,
    force_paths: set[str] | None = None,
    path_filter: PairFilter | None = None,
    filtered_out: set[str] | None = None,
    dirs_out: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, float], dict[str, str], list[str]]:
    """Scan a tree and return checksums, mtimes, signatures and unreadable paths.

    Files left out by the size or age filter of path_filter are added to
    filtered_out, so callers can drop them on both sides instead of treating
    them as deleted. Folders that are scanned (not excluded) are added to dirs_out.

    A single unreadable file is returned as unreadable so callers can freeze it.
    A directory that cannot be listed always aborts the scan, because the files
    below it are unknown and would otherwise look deleted.
    """

    checksums: dict[str, str] = {}
    mtimes: dict[str, float] = {}
    signatures: dict[str, str] = {}
    unreadable: dict[str, str] = {}
    listing_errors: list[str] = []
    force_paths = force_paths or set()
    if not base_dir.exists():
        raise ScanIncompleteError(base_dir, ["root directory does not exist"])

    def on_walk_error(exc: OSError) -> None:
        listing_errors.append(f"{exc.filename}: {exc}")

    for root, dirs, files in os.walk(base_dir, onerror=on_walk_error):
        dirs[:] = [
            directory
            for directory in dirs
            if not should_exclude_directory(directory)
            and not should_exclude_rel_path(
                str((Path(root) / directory).relative_to(base_dir)) + "/"
            )
            and not (
                path_filter
                and path_filter.excludes_path(
                    str((Path(root) / directory).relative_to(base_dir)) + "/", is_dir=True
                )
            )
        ]
        if dirs_out is not None:
            dirs_out.update(str((Path(root) / directory).relative_to(base_dir)) for directory in dirs)
        for name in files:
            if should_exclude(name):
                continue
            full_path = Path(root) / name
            rel_path = str(full_path.relative_to(base_dir))
            if should_exclude_rel_path(rel_path) or full_path.is_symlink():
                continue
            if path_filter and path_filter.excludes_path(rel_path):
                continue
            try:
                stat = full_path.stat()
                if path_filter and path_filter.excludes_file(stat):
                    if filtered_out is not None:
                        filtered_out.add(rel_path)
                    continue
                signature = stat_signature(stat)
                if (
                    not force_full
                    and rel_path not in force_paths
                    and rel_path in prev_checksums
                    and prev_signatures.get(rel_path) == signature
                ):
                    checksum = prev_checksums[rel_path]
                else:
                    checksum = md5_file(full_path)
            except OSError as exc:
                unreadable[rel_path] = str(exc)
                continue
            checksums[rel_path] = checksum
            mtimes[rel_path] = stat.st_mtime
            signatures[rel_path] = signature

    if listing_errors:
        raise ScanIncompleteError(base_dir, listing_errors)
    if len(unreadable) > UNREADABLE_LIMIT_RATIO * (len(checksums) + len(unreadable)):
        raise ScanIncompleteError(
            base_dir, [f"{path}: {error}" for path, error in unreadable.items()]
        )
    return checksums, mtimes, signatures, sorted(unreadable)


def load_state() -> dict | None:
    config = require_config()
    if not config.state_file.exists():
        return None
    with open(config.state_file, encoding="utf-8") as handle:
        raw_state = json.load(handle)
    raw_state.setdefault("pairs", {})
    return raw_state


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = handle.name
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))


def save_state(pair_states: dict[str, dict]) -> None:
    config = require_config()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now().isoformat(),
        "pairs": pair_states,
    }
    if config.state_file.exists():
        shutil.copy2(config.state_file, config.state_file.with_suffix(".json.bak"))
    atomic_write_json(config.state_file, payload)


def load_conflicts() -> list[dict]:
    config = require_config()
    if not config.conflicts_file.exists():
        return []
    with open(config.conflicts_file, encoding="utf-8") as handle:
        raw = json.load(handle)
    if isinstance(raw, dict):
        return raw.get("conflicts", [])
    return raw


def save_conflicts(conflicts: list[dict]) -> None:
    config = require_config()
    if conflicts:
        atomic_write_json(
            config.conflicts_file,
            {
                "timestamp": datetime.now().isoformat(),
                "conflicts": conflicts,
            },
        )
    elif config.conflicts_file.exists():
        config.conflicts_file.unlink()


APP_NOTIFICATION_PREFIX = "SYNCBRIDGE_NOTIFY "


NOTIFICATION_TARGETS = ("summary", "conflicts", "deletions")


def notify(title: str, message: str, open: str = "summary") -> None:
    """Send a macOS notification shown as coming from the sync app.

    open names the app window a click shows (one of NOTIFICATION_TARGETS).
    Without the app, terminal-notifier's -sender shows the app icon and opens
    the app when the notification is clicked.
    """

    if os.environ.get("SYNCBRIDGE_APP_NOTIFICATIONS"):
        # The menu bar app posts it, so it carries the SyncBridge icon.
        payload = {"title": title, "message": message, "open": open}
        print(APP_NOTIFICATION_PREFIX + json.dumps(payload), flush=True)
        return
    try:
        notifier = shutil.which("terminal-notifier")
        if notifier is None:
            homebrew_notifier = Path("/opt/homebrew/bin/terminal-notifier")
            if homebrew_notifier.exists():
                notifier = str(homebrew_notifier)

        if notifier:
            command = [
                notifier,
                "-title",
                title,
                "-message",
                message,
                "-sound",
                "Glass",
                "-group",
                APP_BUNDLE_ID,
                "-sender",
                APP_BUNDLE_ID,
            ]
            subprocess.run(command, capture_output=True, timeout=5)
            return

        script = """on run argv
display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"
end run"""
        subprocess.run(
            ["osascript", "-e", script, title, message],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def log_msg(message: str, verbose: bool = False) -> None:
    config = require_config()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    config.state_dir.mkdir(parents=True, exist_ok=True)
    if config.log_file.exists() and config.log_file.stat().st_size >= 20 * 1024 * 1024:
        for index in range(3, 0, -1):
            source = config.log_file.with_name(
                config.log_file.name if index == 1 else f"{config.log_file.name}.{index - 1}"
            )
            destination = config.log_file.with_name(f"{config.log_file.name}.{index}")
            if source.exists():
                if destination.exists():
                    destination.unlink()
                source.replace(destination)
    with open(config.log_file, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if verbose:
        print(line)


def copy_file(
    src_base: Path,
    dst_base: Path,
    rel_path: str,
    verbose: bool = False,
    dry_run: bool = False,
    keep_old=None,
) -> bool:
    """Copy via a temporary file, so an interrupted copy never damages the target.

    keep_old is called right before an existing target is replaced.
    """

    src = src_base / rel_path
    dst = dst_base / rel_path
    if dry_run:
        log_msg(f"  [DRY RUN] Would copy: {rel_path}", verbose)
        return True
    temporary = dst.with_name(f".{dst.name}{TEMP_COPY_SUFFIX}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(temporary))
        if keep_old and dst.exists():
            keep_old()
        os.replace(temporary, dst)
        log_msg(f"  Copied: {rel_path}", verbose)
        return True
    except Exception as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        log_msg(f"  ERROR copying {rel_path}: {exc}", verbose)
        return False


def side_dir(pair: SyncPair, side: str) -> Path:
    return pair.local_dir if side == "local" else pair.onedrive_dir


def copy_for_action(
    pair: SyncPair, action: str, rel_path: str, verbose: bool = False, dry_run: bool = False
) -> bool:
    """Run a copy action; the replaced target version is kept per deletion handling."""

    target = "onedrive" if action == "copy_local_to_onedrive" else "local"
    source = other_side(target)
    return copy_file(
        side_dir(pair, source),
        side_dir(pair, target),
        rel_path,
        verbose,
        dry_run,
        keep_old=lambda: keep_version(pair, target, rel_path, move=False),
    )


def trash_root() -> Path:
    config = require_config()
    return config.trash_dir or config.state_dir / "trash"


def macos_trash_dir() -> Path:
    return Path.home() / ".Trash"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    return path.with_name(f"{path.stem} {RUN_ID}{path.suffix}")


def version_destination(pair: SyncPair, side: str, rel_path: str) -> Path:
    """Where a deleted or overwritten version is kept (FreeFileSync naming styles)."""

    config = require_config()
    root = trash_root()
    if config.versioning_style == "timestamp_file":
        rel = Path(rel_path)
        return root / pair.key / side / rel.parent / f"{rel.stem} {RUN_ID}{rel.suffix}"
    if config.versioning_style == "replace":
        return root / pair.key / side / rel_path
    return root / RUN_ID / pair.key / side / rel_path


def keep_version(pair: SyncPair, side: str, rel_path: str, move: bool) -> None:
    """Keep the current file before SyncBridge deletes (move) or overwrites (copy) it."""

    path = side_dir(pair, side) / rel_path
    if not path.exists():
        return
    handling = require_config().deletion_handling
    if handling == "permanent":
        if move:
            path.unlink()
        return
    if handling == "trash":
        destination = unique_path(macos_trash_dir() / path.name)
    else:
        destination = version_destination(pair, side, rel_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(path), str(destination))
    else:
        shutil.copy2(str(path), str(destination))


def version_timestamp(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d_%H%M%S").timestamp()


def prune_trash(retention_days: int | None = None) -> None:
    """Apply the version limits: maximum age (0 = keep), at most
    versioning_max_count and at least versioning_min_count versions per file."""

    config = require_config()
    if retention_days is None:
        retention_days = config.trash_retention_days
    root = trash_root()
    if not root.exists():
        return
    versions: dict[tuple, list[tuple[float, Path]]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if len(parts) > 1 and re.fullmatch(RUN_ID_PATTERN, parts[0]):
            key, stamp = ("folder", *parts[1:]), parts[0]
        else:
            match = re.fullmatch(rf"(.+) ({RUN_ID_PATTERN})(\.[^.]+)?", path.name)
            if not match:
                continue  # "replace" style keeps exactly one version
            key = ("file", *parts[:-1], match.group(1) + (match.group(3) or ""))
            stamp = match.group(2)
        versions.setdefault(key, []).append((version_timestamp(stamp), path))

    cutoff = time.time() - retention_days * 86400
    removed = 0
    for items in versions.values():
        for index, (stamp, path) in enumerate(sorted(items, reverse=True)):
            too_many = config.versioning_max_count and index >= config.versioning_max_count
            too_old = retention_days and stamp < cutoff and index >= config.versioning_min_count
            if too_many or too_old:
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    log_msg(f"ERROR pruning version {path}: {exc}")
    for directory, _, _ in sorted(os.walk(root), key=lambda entry: -len(entry[0])):
        if Path(directory) != root:
            try:
                Path(directory).rmdir()
            except OSError:
                pass
    if removed:
        log_msg(f"Pruned {removed} old version(s) from {root}")


def remove_empty_parents(
    base_dir: Path, rel_path: str, keep_if_in: Path | None = None
) -> None:
    """Remove folders emptied by a delete or rename, never the pair root.

    Folders holding only Finder/Windows metadata files count as empty. A folder
    that still exists below keep_if_in (the other side) is kept, so deleting the
    last file of a folder never removes the folder itself.
    """

    current = (base_dir / rel_path).parent
    while current != base_dir and base_dir in current.parents:
        if keep_if_in and (keep_if_in / current.relative_to(base_dir)).is_dir():
            return
        try:
            entries = os.listdir(current)
            if any(
                entry not in DEFAULT_EXCLUDE_NAMES or not (current / entry).is_file()
                for entry in entries
            ):
                return
            for entry in entries:
                (current / entry).unlink()
            current.rmdir()
        except OSError:
            return
        current = current.parent


def delete_from_side(
    pair: SyncPair,
    action: str,
    rel_path: str,
    verbose: bool = False,
    dry_run: bool = False,
) -> bool:
    """Run a delete action. The file is kept per deletion handling (versioning,
    macOS Trash) or removed permanently. OneDrive also keeps it in its recycle bin."""

    if action not in ("delete_onedrive", "delete_local"):
        raise ValueError(f"Unknown deletion action: {action}")
    side = "onedrive" if action == "delete_onedrive" else "local"
    base = side_dir(pair, side)
    if dry_run:
        log_msg(f"  [DRY RUN] Would delete: {rel_path}", verbose)
        return True
    try:
        keep_version(pair, side, rel_path, move=True)
        log_msg(f"  Deleted ({require_config().deletion_handling}): {rel_path}", verbose)
        other = side_dir(pair, other_side(side))
        remove_empty_parents(base, rel_path, keep_if_in=other)
        return True
    except Exception as exc:
        log_msg(f"  ERROR deleting {rel_path}: {exc}", verbose)
        return False


def replace_via_temporary(source: Path, destination: Path) -> None:
    """Rename through a temporary name so case-only renames work on APFS/OneDrive."""

    temporary = source.with_name(
        f".{source.name}.sync-rename-{os.getpid()}-{time.time_ns()}"
    )
    os.replace(source, temporary)
    try:
        os.replace(temporary, destination)
    except OSError:
        os.replace(temporary, source)
        raise


def match_name_case(base_dir: Path, rel_path: str, exact: bool = False) -> None:
    """Give every component of rel_path exactly the requested spelling on disk.

    On a case-insensitive file system, renaming "internal/x" to "Internal/x"
    only touches the file, so the parent folder keeps its old case and the
    rename would be detected again on every run. With `exact`, names that only
    differ in their Unicode form (e.g. how "ä" is stored) are renamed too.
    """

    current = base_dir
    for part in Path(rel_path).parts:
        wanted = part if exact else unicodedata.normalize("NFC", part)
        entries = os.listdir(current)
        actual = next(
            (
                entry
                for entry in entries
                if (entry if exact else unicodedata.normalize("NFC", entry)) == wanted
            ),
            None,
        )
        if actual is None:
            actual = next(
                (
                    entry
                    for entry in entries
                    if canonical_rel_path(entry) == canonical_rel_path(part)
                ),
                None,
            )
            if actual is None:
                raise FileNotFoundError(f"Missing after rename: {current / part}")
            replace_via_temporary(current / actual, current / part)
            actual = part
        current = current / actual


def rename_file(
    base_dir: Path,
    source_rel_path: str,
    destination_rel_path: str,
    verbose: bool = False,
    dry_run: bool = False,
) -> bool:
    """Rename without data loss, including case-only renames on macOS."""

    source = base_dir / source_rel_path
    destination = base_dir / destination_rel_path
    if dry_run:
        log_msg(
            f"  [DRY RUN] Would rename: {source_rel_path} -> {destination_rel_path}",
            verbose,
        )
        return True

    try:
        if not source.exists():
            raise FileNotFoundError(f"Rename source does not exist: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            try:
                same_entry = source.samefile(destination)
            except OSError:
                same_entry = False

            if same_entry:
                replace_via_temporary(source, destination)
            elif md5_file(source) == md5_file(destination):
                # A previous protected run may already have copied the new name.
                # Removing the identical old name completes the rename safely.
                source.unlink()
            else:
                raise FileExistsError(
                    f"Rename destination already exists with different content: {destination}"
                )
        else:
            os.replace(source, destination)
        match_name_case(base_dir, destination_rel_path)
        remove_empty_parents(base_dir, source_rel_path)

        log_msg(f"  Renamed: {source_rel_path} -> {destination_rel_path}", verbose)
        return True
    except Exception as exc:
        log_msg(
            f"  ERROR renaming {source_rel_path} -> {destination_rel_path}: {exc}",
            verbose,
        )
        return False


def is_text_file(rel_path: str) -> bool:
    return Path(rel_path).suffix.lower() in TEXT_EXTENSIONS


def dialog_icon_clause() -> str:
    """AppleScript suffix that shows the app icon instead of the script icon."""

    return f' with icon (POSIX file "{APP_ICON}")' if APP_ICON.exists() else ""


def _osascript_with_tempfile(
    message: str,
    title: str,
    buttons: list[str],
    default_button: str,
    timeout: int = 300,
) -> str:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(message)
            tmp_path = handle.name
        buttons_str = ", ".join(f'"{button}"' for button in buttons)
        script = f"""set msgText to read POSIX file "{tmp_path}" as «class utf8»
display dialog msgText with title "{title}" buttons {{{buttons_str}}} default button "{default_button}"{dialog_icon_clause()}
return button returned of result"""
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except Exception:
        return ""
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def version_info(path: Path) -> dict | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"modified": int(stat.st_mtime), "size": stat.st_size}


# A save date further ahead than this comes from a wrong clock; "newer" then means nothing.
CLOCK_TOLERANCE_SECONDS = 300


def in_future(version: dict | None) -> bool:
    return version is not None and version["modified"] > time.time() + CLOCK_TOLERANCE_SECONDS


def versions_of(paths: dict[str, Path]) -> tuple[dict | None, dict | None, str | None]:
    """Return (local, onedrive, newer side); no side is newer within a second or when a
    date lies in the future."""

    local, onedrive = version_info(paths["local"]), version_info(paths["onedrive"])
    if not local or not onedrive or in_future(local) or in_future(onedrive):
        return local, onedrive, None
    try:
        # Exact times: two saves 0.1 s apart can fall on either side of a whole second.
        gap = paths["local"].stat().st_mtime - paths["onedrive"].stat().st_mtime
    except OSError:
        return local, onedrive, None
    newer = None if abs(gap) < 1 else ("local" if gap > 0 else "onedrive")
    return local, onedrive, newer


def conflict_paths(pair: SyncPair, rel_path: str) -> dict[str, Path]:
    return {"local": pair.local_dir / rel_path, "onedrive": pair.onedrive_dir / rel_path}


def conflict_versions(pair: SyncPair, rel_path: str) -> tuple[dict | None, dict | None, str | None]:
    """Return (local, onedrive, newer side) for a conflicted path."""

    return versions_of(conflict_paths(pair, rel_path))


def conflict_versions_text(pair: SyncPair, rel_path: str) -> str:
    """One line per side with modification time and size; the newer one is marked."""

    local, onedrive, newer = conflict_versions(pair, rel_path)
    lines = []
    for side, label, info in (("local", "Local", local), ("onedrive", "OneDrive", onedrive)):
        if info is None:
            details = "deleted"
        else:
            details = (
                f"{datetime.fromtimestamp(info['modified']):%Y-%m-%d %H:%M} · "
                f"{format_size(info['size'])}"
            )
            if side == newer:
                details += "  ← newer"
        lines.append(f"{label + ':':<10}{details}")
    return "\n".join(lines)


def conflict_diff_preview(pair: SyncPair, rel_path: str, max_lines: int = 12) -> str:
    """Short change summary plus the first changed lines, Local vs. OneDrive."""

    try:
        local_lines = (pair.local_dir / rel_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        onedrive_lines = (pair.onedrive_dir / rel_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError as exc:
        return f"(could not generate diff: {exc})"
    diff = [
        line
        for line in difflib.unified_diff(onedrive_lines, local_lines, lineterm="", n=0)
        if not line.startswith(("---", "+++", "@@"))
    ]
    if not diff:
        return "(files have the same content — metadata differs)"
    added = sum(line.startswith("+") for line in diff)
    removed = sum(line.startswith("-") for line in diff)
    lines = [f"Local has {added} added and {removed} removed line(s) vs. OneDrive."]
    lines.extend(line[:100] for line in diff[:max_lines])
    if len(diff) > max_lines:
        lines.append(f"... and {len(diff) - max_lines} more line(s)")
    return "\n".join(lines)


# --- Conflict analysis (suggestions without AI, texts for AI help) ---

SIDE_LABELS = {"local": "The laptop version", "onedrive": "The OneDrive version"}
# Office file -> (XML parts holding the content, tag of one paragraph or cell)
OFFICE_TEXT_PARTS = {
    ".docx": (
        ("word/document.xml", "word/header", "word/footer", "word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"),
        "p",
    ),
    ".pptx": (("ppt/slides/", "ppt/notesSlides/"), "p"),
    ".xlsx": (("xl/worksheets/",), "c"),
}
EXCEL_STRINGS_PART = "xl/sharedStrings.xml"
# The document properties of an Office file. Saving again changes only the save details
# in them (author, revision, dates, statistics); title, subject, company and the custom
# properties (docProps/custom.xml) are content.
OFFICE_PROPERTY_PARTS = ("docProps/core.xml", "docProps/app.xml")
OFFICE_SAVE_DETAILS = {
    "lastModifiedBy", "revision", "modified", "lastPrinted", "TotalTime", "Application", "AppVersion",
    "Pages", "Words", "Characters", "CharactersWithSpaces", "Lines", "Paragraphs", "Slides", "Notes",
}
# A password-protected Office file is no zip package but an OLE file starting with this.
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Media files that are never empty; a shorter copy that matches the start of the other
# version was interrupted. Text-like formats are left out: they may be shortened on purpose.
MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".heic", ".gif", ".tif", ".tiff", ".webp",
    ".mp4", ".mov", ".m4v", ".mp3", ".m4a", ".wav", ".zip",
}
NEVER_EMPTY_SUFFIXES = MEDIA_SUFFIXES | set(OFFICE_TEXT_PARTS) | {".pdf", ".png"}
# An MP3 cut at a frame boundary is the beginning of the full song and still plays; a zip
# file can start with another complete one and hide its files behind a new directory.
INTERRUPTED_COPY_SUFFIXES = MEDIA_SUFFIXES - {".mp3", ".zip"}
# Larger files are not compared byte by byte when looking for an interrupted copy.
INCOMPLETE_COPY_LIMIT = 512 * 1024 * 1024
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
# Text formats where inserted lines only add; in code or settings an added line can
# change what the existing lines mean (a repeated key, an early return), in CSV a line
# can belong to a quoted cell.
ADDITIVE_TEXT_SUFFIXES = {".txt", ".md", ".log"}
# Line endings inside a quoted CSV cell are part of its value, so CSV is never "the same".
NO_TEXT_COMPARISON_SUFFIXES = {".csv"}
# Reading a zip file that is damaged inside, or whose names are no valid text.
ZIP_ERRORS = (zipfile.BadZipFile, ValueError, EOFError, zlib.error, lzma.LZMAError)


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_text(element: ElementTree.Element) -> str:
    return "".join(part.text or "" for part in element.iter() if xml_local_name(part.tag) == "t").strip()


def excel_cell_text(cell: ElementTree.Element, sheet: str, strings: list[str]) -> str:
    """One Excel cell as "sheet1!B2: content"; a formula counts instead of its last result."""

    parts = {xml_local_name(child.tag): child.text or "" for child in cell}
    if parts.get("f"):
        content = "=" + parts["f"]
    elif cell.get("t") == "s" and parts.get("v", "").isdigit() and int(parts["v"]) < len(strings):
        content = strings[int(parts["v"])]
    else:
        content = parts.get("v") or xml_text(cell)
    return f"{sheet}!{cell.get('r', '')}: {content}" if content.strip() else ""


def office_info(path: Path, content: bool = True) -> dict | None:
    """Last author and revision of an Office file; with content also its paragraphs
    (Excel: cells)."""

    spec = OFFICE_TEXT_PARTS.get(path.suffix.lower())
    if spec is None:
        return None
    prefixes, paragraph_tag = spec
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(archive.namelist())
            info = {"author": None, "revision": None}
            if "docProps/core.xml" in names:
                for element in ElementTree.fromstring(archive.read("docProps/core.xml")).iter():
                    name = xml_local_name(element.tag)
                    if name == "lastModifiedBy" and element.text:
                        info["author"] = element.text.strip()
                    elif name == "revision" and (element.text or "").strip().isdigit():
                        info["revision"] = int(element.text.strip())
            if not content:
                return info
            strings = []
            if paragraph_tag == "c" and EXCEL_STRINGS_PART in names:
                root = ElementTree.fromstring(archive.read(EXCEL_STRINGS_PART))
                strings = [xml_text(item) for item in root.iter() if xml_local_name(item.tag) == "si"]
            paragraphs = []
            for name in names:
                if not name.startswith(prefixes) or not name.endswith(".xml"):
                    continue
                for element in ElementTree.fromstring(archive.read(name)).iter():
                    if xml_local_name(element.tag) != paragraph_tag:
                        continue
                    text = excel_cell_text(element, Path(name).stem, strings) if paragraph_tag == "c" else xml_text(element)
                    if text:
                        paragraphs.append(text)
    except (OSError, ElementTree.ParseError, KeyError, *ZIP_ERRORS):
        return None
    return {**info, "paragraphs": paragraphs}


def text_lines(path: Path, strict: bool = False) -> list[str] | None:
    """Lines of a text file. Line endings, a BOM and the way accents are stored never count;
    unless strict, neither do trailing spaces, blank lines and unreadable characters.
    Strict reading gives None for a file that is not valid UTF-8."""

    try:
        data = path.read_bytes()
        text = unicodedata.normalize("NFC", data.decode("utf-8-sig", errors="strict" if strict else "replace"))
    except (OSError, UnicodeDecodeError):
        return None
    if not strict:
        return [line.rstrip() for line in text.splitlines() if line.strip()]
    # Only CRLF and LF end a line; a lone CR (inside a quoted CSV field) is part of the text.
    lines = text.replace("\r\n", "\n").split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def document_paragraphs(path: Path) -> list[str] | None:
    """Comparable text units of a file: Office paragraphs or non-empty text lines."""

    if path.suffix.lower() in OFFICE_TEXT_PARTS:
        info = office_info(path)
        return info["paragraphs"] if info else None
    return text_lines(path) if is_text_file(path.name) else None


def file_health(path: Path) -> str | None:
    """"intact" or "damaged" for a file whose type shows it, None when it can't be told.

    Damaged: an empty file of a type that is never empty, an Office or zip file that does
    not open or whose parts fail their checksum, a PDF without its end marker or a PNG that
    does not end with its end chunk.
    """

    suffix = path.suffix.lower()
    if suffix not in NEVER_EMPTY_SUFFIXES:
        return None
    try:
        size = path.stat().st_size
        if size == 0:
            return "damaged"
        with path.open("rb") as handle:
            if suffix in OFFICE_TEXT_PARTS and handle.read(len(OLE_SIGNATURE)) == OLE_SIGNATURE:
                return None
            handle.seek(max(0, size - 1024))
            tail = handle.read()
        if suffix in OFFICE_TEXT_PARTS or suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None:
                    return "damaged"
        elif (suffix == ".pdf" and b"%%EOF" not in tail) or (suffix == ".png" and not tail.endswith(PNG_END)):
            return "damaged"
    except ZIP_ERRORS:
        return "damaged"
    except (OSError, NotImplementedError, RuntimeError):
        # Unreadable, or a compression or encryption Python can't check.
        return None
    return "intact"


def other_side(side: str) -> str:
    return "onedrive" if side == "local" else "local"


def intact_side(paths: dict[str, Path]) -> str | None:
    """The side to keep when one version is shown to be intact and the other damaged."""

    health = {side: file_health(path) for side, path in paths.items()}
    for side in ("local", "onedrive"):
        if health[side] == "intact" and health[other_side(side)] == "damaged":
            return side
    return None


def is_incomplete_copy(short: Path, full: Path) -> bool:
    """True when the media file `short` is only the beginning of `full`, as after an
    interrupted copy, and `full` is not damaged itself."""

    if short.suffix.lower() not in INTERRUPTED_COPY_SUFFIXES:
        return False
    try:
        size, full_size = short.stat().st_size, full.stat().st_size
        if not 0 < size < full_size <= INCOMPLETE_COPY_LIMIT or file_health(full) == "damaged":
            return False
        with short.open("rb") as a, full.open("rb") as b:
            while chunk := a.read(1 << 20):
                if chunk != b.read(len(chunk)):
                    return False
        return True
    except OSError:
        return False


def cut_off_side(paths: dict[str, Path]) -> str | None:
    """The side whose text file is the beginning of the other one and stops mid-line."""

    if not all(is_text_file(path.name) for path in paths.values()):
        return None
    try:
        texts = {side: path.read_text(encoding="utf-8-sig", errors="replace") for side, path in paths.items()}
    except OSError:
        return None
    for side, other in (("local", "onedrive"), ("onedrive", "local")):
        short, full = texts[side], texts[other]
        if short and len(short) < len(full) and full.startswith(short) \
                and not short.endswith("\n") and full[len(short)] != "\n":
            return side
    return None


def office_parts(path: Path) -> dict[str, object] | None:
    """Every part of an Office file by checksum and size; the document properties by
    their values without the save details."""

    def properties(data: bytes) -> list[tuple]:
        return [
            (element.tag, (element.text or "").strip(), sorted(element.attrib.items()))
            for element in ElementTree.fromstring(data).iter()
            if xml_local_name(element.tag) not in OFFICE_SAVE_DETAILS
        ]

    try:
        with zipfile.ZipFile(path) as archive:
            return {
                item.filename: properties(archive.read(item))
                if item.filename in OFFICE_PROPERTY_PARTS
                else (item.CRC, item.file_size)
                for item in archive.infolist()
            }
    except (OSError, ElementTree.ParseError, *ZIP_ERRORS):
        return None


def json_data(path: Path) -> tuple | None:
    """The data of a JSON file, comparable whatever the layout and key order, or None
    when it is no clean JSON (also with a repeated key, where readers disagree)."""

    def without_repeated_keys(pairs: list[tuple[str, object]]) -> dict:
        if len({key for key, _ in pairs}) < len(pairs):
            raise ValueError("repeated key")
        return dict(pairs)

    def typed(value: object) -> tuple:
        # Every value with its type: == alone takes true for 1 and 1 for 1.0.
        if isinstance(value, dict):
            return ("object", tuple(sorted((key, typed(item)) for key, item in value.items())))
        if isinstance(value, list):
            return ("array", tuple(typed(item) for item in value))
        return (type(value).__name__, value)

    try:
        # Decimal keeps fractions as written: float() rounds 1.00000000000000001 to 1.0.
        return typed(json.loads(
            path.read_bytes().decode("utf-8-sig"), object_pairs_hook=without_repeated_keys, parse_float=Decimal
        ))
    except (OSError, ValueError, RecursionError):
        return None


def same_known(read, paths: dict[str, Path]) -> bool:
    """True when `read` gives the same, readable result for both versions."""

    local = read(paths["local"])
    return local is not None and local == read(paths["onedrive"])


def same_content(paths: dict[str, Path]) -> bool:
    """True when both versions hold the same content: an Office file saved again (only its
    save details differ), JSON with the same data in another layout, or another text file
    with other line endings, BOM or accent storage."""

    suffix = paths["local"].suffix.lower()
    if suffix in OFFICE_TEXT_PARTS:
        return same_known(office_parts, paths)
    if suffix == ".json":
        # Not compared as text: storing accents alike could merge two keys into a repeated one.
        return same_known(json_data, paths)
    return is_text_file(paths["local"].name) and suffix not in NO_TEXT_COMPARISON_SUFFIXES \
        and same_known(lambda path: text_lines(path, strict=True), paths)


def only_adds(older: list[str], newer: list[str]) -> bool:
    """True when `newer` is `older` with units inserted: same order, nothing removed."""

    rest = iter(newer)
    return all(unit in rest for unit in older)


def safe_decision(pair: SyncPair, conflict: dict) -> tuple[str | None, str]:
    """Decide a conflict only where no content can be lost; returns (side, reason).

    Kept: the intact version when the other is empty or damaged, the complete media file
    when the other is an interrupted copy, and the newer version when it has the same
    content or, in plain text, only adds lines. Name conflicts, deletions, same-second
    saves, dates in the future and everything else stay open.
    """

    if conflict.get("related_file"):
        return None, ""
    paths = conflict_paths(pair, conflict["file"])
    local, onedrive, newer = versions_of(paths)
    if local is None or onedrive is None:
        return None, ""
    if intact := intact_side(paths):
        return intact, "the other version is empty or damaged"
    if any(file_health(path) == "damaged" for path in paths.values()):
        return None, ""
    for side in ("local", "onedrive"):
        if is_incomplete_copy(paths[other_side(side)], paths[side]):
            return side, "the other version is an incomplete copy"
    if newer is None:
        return None, ""
    if same_content(paths):
        return newer, "same content, newer version"
    if paths["local"].suffix.lower() in ADDITIVE_TEXT_SUFFIXES:
        lines = {side: text_lines(path, strict=True) for side, path in paths.items()}
        old_lines, new_lines = lines[other_side(newer)], lines[newer]
        if old_lines is not None and new_lines is not None and only_adds(old_lines, new_lines):
            return newer, "the newer version only adds lines"
    return None, ""


def plural(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def analyze_conflict(pair: SyncPair, rel_path: str) -> dict:
    """Suggest which version to keep, with a confidence and a reason (no AI)."""

    paths = conflict_paths(pair, rel_path)
    local, onedrive, newer = versions_of(paths)
    infos = {side: office_info(path) if path.exists() else None for side, path in paths.items()}
    result = {
        "suggestion": None,
        "confidence": None,
        "reason": "",
        "mergeable": is_text_file(rel_path) and local is not None and onedrive is not None,
        **{
            f"{side}_info": {"author": info["author"], "revision": info["revision"]} if info else None
            for side, info in infos.items()
        },
    }

    def suggest(side: str | None, confidence: str, reason: str) -> dict:
        authors = [
            f"{'OneDrive' if s == 'onedrive' else 'Laptop'} last saved by {info['author']}"
            for s, info in infos.items()
            if info and info["author"]
        ]
        if authors:
            reason += " (" + "; ".join(authors) + ")"
        result.update(suggestion=side, confidence=confidence if side else None, reason=reason)
        return result

    if local is None or onedrive is None:
        deleted = "laptop" if local is None else "OneDrive"
        return suggest(None, "", f"The file was deleted on the {deleted} and edited on the other side. Decide whether the edit or the deletion wins.")
    future = [side for side, version in (("local", local), ("onedrive", onedrive)) if in_future(version)]
    if future:
        newer_text = f"{SIDE_LABELS[future[0]]} is dated in the future, so it is unclear which one is newer."
    else:
        newer_text = f"{SIDE_LABELS[newer]} is newer." if newer else "Both have the same date."
    if intact := intact_side(paths):
        return suggest(intact, "high", f"{SIDE_LABELS[other_side(intact)]} is empty or damaged.")
    texts = {side: infos[side]["paragraphs"] if infos[side] else document_paragraphs(path) for side, path in paths.items()}
    if texts["local"] is None or texts["onedrive"] is None:
        return suggest(newer, "low", f"The content can't be compared. {newer_text}")
    unit = "line" if is_text_file(rel_path) else "paragraph"
    sets = {side: set(lines) for side, lines in texts.items()}
    only = {
        side: [line for line in texts[side] if line not in sets[other]]
        for side, other in (("local", "onedrive"), ("onedrive", "local"))
    }
    if not only["local"] and not only["onedrive"]:
        if infos["local"] and infos["onedrive"] and not same_known(office_parts, paths):
            return suggest(
                newer,
                "low",
                f"Both versions have the same text, but other parts differ, such as pictures, links or layout. {newer_text}",
            )
        return suggest(newer, "medium", f"Both versions have the same text; only formatting, order or metadata differs. {newer_text}")
    for side, other in (("local", "onedrive"), ("onedrive", "local")):
        if not only[other]:
            return suggest(
                side,
                "high",
                f"{SIDE_LABELS[side]} contains everything in the other version plus {plural(len(only[side]), unit)} more.",
            )
    if cut := cut_off_side(paths):
        return suggest(
            other_side(cut),
            "medium",
            f"{SIDE_LABELS[cut]} looks cut off: it stops in the middle of a line that the other version continues.",
        )
    return suggest(
        "merge",
        "high",
        f"Both sides have their own changes: the laptop has {plural(len(only['local']), unit)} that OneDrive lacks, "
        f"OneDrive has {plural(len(only['onedrive']), unit)} that the laptop lacks.",
    )


def conflict_text(path: Path, max_chars: int) -> tuple[str | None, bool]:
    """Readable text of one version for AI help, shortened to max_chars."""

    if not path.exists():
        return None, False
    if path.suffix.lower() in OFFICE_TEXT_PARTS:
        paragraphs = document_paragraphs(path)
        text = "\n".join(paragraphs) if paragraphs is not None else None
    elif is_text_file(path.name):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = None
    else:
        text = None
    if text is None or len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "\n[… shortened]", True


def conflict_texts_payload(pair_name: str, rel_path: str, max_chars: int = 20000) -> dict:
    pair = get_pair_by_name(pair_name)
    local_text, local_cut = conflict_text(pair.local_dir / rel_path, max_chars)
    onedrive_text, onedrive_cut = conflict_text(pair.onedrive_dir / rel_path, max_chars)
    return {
        "pair": pair_name,
        "file": rel_path,
        "local_text": local_text,
        "onedrive_text": onedrive_text,
        "truncated": local_cut or onedrive_cut,
        "mergeable": is_text_file(rel_path) and local_text is not None and onedrive_text is not None,
        "analysis": analyze_conflict(pair, rel_path),
    }


def apply_merge(pair: SyncPair, rel_path: str, merged_text: str) -> None:
    """Write an approved merge to both sides; both previous versions are kept."""

    if not is_text_file(rel_path):
        raise ValueError("Only text files can be merged automatically; keep one version instead")
    for side in ("local", "onedrive"):
        if not (side_dir(pair, side) / rel_path).is_file():
            raise ValueError("Cannot merge: the file is missing on one side")
    for side in ("local", "onedrive"):
        keep_version(pair, side, rel_path, move=False)
        atomic_write_text(side_dir(pair, side) / rel_path, merged_text)
    if md5_file(pair.local_dir / rel_path) != md5_file(pair.onedrive_dir / rel_path):
        raise RuntimeError(f"Merged files differ for [{pair.name}] {rel_path}")


def show_conflict_dialog(
    pair: SyncPair,
    conflict: dict,
    verbose: bool = False,
    dry_run: bool = False,
) -> str | None:
    rel_path = conflict["file"]
    reason = conflict["reason"]
    local_path = pair.local_dir / rel_path
    onedrive_path = pair.onedrive_dir / rel_path
    header = (
        f"Pair: {pair.name}\n"
        f"Conflict: {rel_path}\n"
        f"Reason: {reason}\n\n"
        f"{conflict_versions_text(pair, rel_path)}\n\n"
    )

    if is_text_file(rel_path) and local_path.exists() and onedrive_path.exists():
        message = f"{header}{conflict_diff_preview(pair, rel_path)}"
    else:
        message = f"{header}(Binary file — no diff available)"

    if dry_run:
        log_msg(f"  [DRY RUN] Would show conflict dialog for: {pair.name}/{rel_path}", verbose)
        return None

    answer = _osascript_with_tempfile(
        message,
        title="Resolve Conflict",
        buttons=["Keep OneDrive", "Skip", "Keep Local"],
        default_button="Keep Local",
        timeout=300,
    )
    if answer == "Keep Local":
        return "local"
    if answer == "Keep OneDrive":
        return "onedrive"
    return None


def show_deletion_dialog(items: list[str]) -> bool:
    total = len(items)
    if total == 0:
        return True

    lines = [
        f"{total} file(s) will be deleted "
        f"(local files are kept in the sync trash for {require_config().trash_retention_days} days, "
        "OneDrive files go to the OneDrive recycle bin):"
    ]
    shown = 0
    for item in items:
        if shown >= 20:
            break
        lines.append(f"  {item}")
        shown += 1
    if total > shown:
        lines.append(f"  ... and {total - shown} more")

    answer = _osascript_with_tempfile(
        "\n".join(lines),
        title="Confirm Deletions",
        buttons=["Cancel", "Delete All"],
        default_button="Cancel",
        timeout=60,
    )
    return answer == "Delete All"


def wait_for_path(
    path: Path,
    max_wait: int = 120,
    check_interval: int = 15,
    verbose: bool = False,
) -> bool:
    if path.exists():
        return True
    log_msg(f"Waiting for path to mount: {path} (up to {max_wait}s)", verbose)
    waited = 0
    while waited < max_wait:
        time.sleep(check_interval)
        waited += check_interval
        if path.exists():
            log_msg(f"Path available after {waited}s: {path}", verbose)
            return True
    log_msg(f"Path not available after {max_wait}s: {path}", verbose)
    return False


def path_has_content(path: Path) -> bool:
    try:
        return path.exists() and next(path.iterdir(), None) is not None
    except Exception:
        return False


def directory_readable(path: Path) -> bool:
    """Return True when the directory can be enumerated.

    A PermissionError must not be interpreted as an empty directory, otherwise a
    sync run could try to copy an entire tree into OneDrive by mistake.
    """

    if not path.exists():
        return False

    try:
        iterator = path.iterdir()
        next(iterator, None)
        return True
    except PermissionError:
        return False
    except NotADirectoryError:
        return False
    except FileNotFoundError:
        return False


def change_category(side: str, previous: str | None, current: str | None) -> str | None:
    if previous == current:
        return None
    if previous is None:
        return f"{side}_new"
    if current is None:
        return f"{side}_deleted"
    return f"{side}_changed"


def action_for_direction(direction: str, local_hash: str | None, onedrive_hash: str | None) -> str:
    """Return the action that makes the target side match the source side."""

    if direction == "to_onedrive":
        return "copy_local_to_onedrive" if local_hash is not None else "delete_onedrive"
    return "copy_onedrive_to_local" if onedrive_hash is not None else "delete_local"


def classify_changes(
    current_local: dict[str, str],
    current_onedrive: dict[str, str],
    prev_local: dict[str, str],
    prev_onedrive: dict[str, str],
    directions: dict[str, str] | None = None,
) -> tuple[list[tuple[str, str]], list[dict]]:
    """Derive actions and conflicts from the changes since the last sync.

    directions maps each change category to to_onedrive, to_local or none
    (default: two-way). Variants that ignore some changes accept lasting
    differences between the sides; two-way treats them as conflicts.
    """

    directions = directions or effective_directions(SyncPair("", Path(), Path()))
    accepts_differences = "none" in directions.values()
    all_files = (
        set(current_local.keys())
        | set(current_onedrive.keys())
        | set(prev_local.keys())
        | set(prev_onedrive.keys())
    )

    actions: list[tuple[str, str]] = []
    conflicts: list[dict] = []
    for rel_path in sorted(all_files):
        cur_local = current_local.get(rel_path)
        cur_onedrive = current_onedrive.get(rel_path)
        prev_local_hash = prev_local.get(rel_path)
        prev_onedrive_hash = prev_onedrive.get(rel_path)

        if cur_local == cur_onedrive:
            continue

        def conflict(reason: str) -> None:
            conflicts.append(
                {
                    "file": rel_path,
                    "reason": reason,
                    "local_hash": cur_local,
                    "onedrive_hash": cur_onedrive,
                }
            )

        if prev_local_hash != prev_onedrive_hash and not accepts_differences:
            conflict("Previous baseline was not synchronized")
            continue

        local_category = change_category("local", prev_local_hash, cur_local)
        onedrive_category = change_category("onedrive", prev_onedrive_hash, cur_onedrive)
        wanted = {
            directions[category]
            for category in (local_category, onedrive_category)
            if category
        } - {"none"}

        if not local_category and not onedrive_category:
            if not accepts_differences:
                conflict("Locations still differ from a previous run")
            continue
        if not wanted:
            continue
        if len(wanted) > 1:
            if cur_local is None:
                conflict("Deleted locally, modified on OneDrive")
            elif cur_onedrive is None:
                conflict("Modified locally, deleted on OneDrive")
            else:
                conflict("Modified in both locations")
            continue
        action = action_for_direction(wanted.pop(), cur_local, cur_onedrive)
        actions.append((action, rel_path))

    return actions, conflicts


def find_safe_renames(
    current_local: dict[str, str],
    current_onedrive: dict[str, str],
    prev_local: dict[str, str],
    prev_onedrive: dict[str, str],
) -> tuple[list[tuple[str, str, str]], set[str]]:
    """Find unique, content-preserving renames made on either side.

    A rename is safe when a previously synchronized path disappeared on one
    side and exactly one new path with the same content appeared under a
    different name. Requiring the destination to be new prevents an unrelated,
    pre-existing duplicate from being mistaken for a rename.
    """

    renames: list[tuple[str, str, str]] = []
    handled_paths: set[str] = set()

    def collect(
        action: str,
        changed: dict[str, str],
        other: dict[str, str],
        prev_changed: dict[str, str],
        prev_other: dict[str, str],
    ) -> None:
        deleted_by_hash: dict[str, list[str]] = {}
        destinations_by_hash: dict[str, list[str]] = {}

        for old_path in set(prev_changed) & set(prev_other):
            previous_hash = prev_changed.get(old_path)
            if not previous_hash or prev_other.get(old_path) != previous_hash:
                continue
            if old_path not in changed and other.get(old_path) == previous_hash:
                deleted_by_hash.setdefault(previous_hash, []).append(old_path)

        for destination_path, current_hash in changed.items():
            if destination_path in prev_changed or destination_path in prev_other:
                continue
            other_hash = other.get(destination_path)
            if other_hash is None or other_hash == current_hash:
                destinations_by_hash.setdefault(current_hash, []).append(
                    destination_path
                )

        for current_hash in sorted(set(deleted_by_hash) & set(destinations_by_hash)):
            old_paths = sorted(deleted_by_hash[current_hash])
            destination_paths = sorted(destinations_by_hash[current_hash])
            if len(old_paths) != 1 or len(destination_paths) != 1:
                continue
            old_path = old_paths[0]
            destination_path = destination_paths[0]
            if old_path == destination_path:
                continue
            if {old_path, destination_path} & handled_paths:
                continue
            renames.append((action, old_path, destination_path))
            handled_paths.update({old_path, destination_path})

    collect(
        "rename_onedrive",
        current_local,
        current_onedrive,
        prev_local,
        prev_onedrive,
    )
    collect(
        "rename_local",
        current_onedrive,
        current_local,
        prev_onedrive,
        prev_local,
    )
    return renames, handled_paths


def canonical_rel_path(rel_path: str) -> str:
    return unicodedata.normalize("NFC", rel_path).casefold()


def find_name_mismatch_conflicts(
    current_local: dict[str, str],
    current_onedrive: dict[str, str],
) -> tuple[list[dict], set[str]]:
    local_only = set(current_local) - set(current_onedrive)
    onedrive_only = set(current_onedrive) - set(current_local)
    local_by_key: dict[str, list[str]] = {}
    onedrive_by_key: dict[str, list[str]] = {}
    for path in local_only:
        local_by_key.setdefault(canonical_rel_path(path), []).append(path)
    for path in onedrive_only:
        onedrive_by_key.setdefault(canonical_rel_path(path), []).append(path)

    conflicts: list[dict] = []
    paths: set[str] = set()
    for key in sorted(set(local_by_key) & set(onedrive_by_key)):
        local_paths = sorted(local_by_key[key])
        onedrive_paths = sorted(onedrive_by_key[key])
        if len(local_paths) != 1 or len(onedrive_paths) != 1:
            continue
        local_path = local_paths[0]
        onedrive_path = onedrive_paths[0]
        paths.update({local_path, onedrive_path})
        if current_local[local_path] == current_onedrive[onedrive_path]:
            # APFS and OneDrive can expose the same filename with different case
            # or Unicode normalization. Identical contents are already in sync.
            continue
        conflicts.append(
            {
                "file": local_path,
                "related_file": onedrive_path,
                "reason": "Name differs by case or Unicode normalization",
                "local_hash": current_local[local_path],
                "onedrive_hash": current_onedrive[onedrive_path],
            }
        )
    return conflicts, paths


def paths_from_conflicts(conflicts: list[dict]) -> set[str]:
    paths = {conflict["file"] for conflict in conflicts}
    paths.update(
        conflict["related_file"]
        for conflict in conflicts
        if conflict.get("related_file")
    )
    return paths


def initial_sync(
    pair: SyncPair,
    current_local: dict[str, str],
    current_onedrive: dict[str, str],
    verbose: bool = False,
    dry_run: bool = False,
) -> tuple[list[str], list[str], list[dict], list[str]]:
    # Copying a name that differs only by case or Unicode form would overwrite the other
    # side's file under its own spelling, so these pairs wait (or are already in sync).
    name_conflicts, name_paths = find_name_mismatch_conflicts(current_local, current_onedrive)
    conflicts = [{"pair": pair.key, **conflict} for conflict in name_conflicts]
    for conflict in conflicts:
        log_msg(f"  CONFLICT [{pair.name}]: {conflict['file']} ({conflict['reason']})", verbose)
    all_files = (set(current_local) | set(current_onedrive)) - name_paths
    to_onedrive_files = []
    to_local_files = []
    failed_paths = []

    for rel_path in sorted(all_files):
        in_local = rel_path in current_local
        in_onedrive = rel_path in current_onedrive

        if in_local and in_onedrive:
            if current_local[rel_path] != current_onedrive[rel_path]:
                conflicts.append(
                    {
                        "pair": pair.key,
                        "file": rel_path,
                        "reason": "Files differ on initial sync",
                        "local_hash": current_local[rel_path],
                        "onedrive_hash": current_onedrive[rel_path],
                    }
                )
                log_msg(f"  CONFLICT [{pair.name}]: {rel_path} (files differ)", verbose)
        elif in_local:
            if copy_file(pair.local_dir, pair.onedrive_dir, rel_path, verbose, dry_run):
                to_onedrive_files.append(rel_path)
            else:
                failed_paths.append(rel_path)
        elif in_onedrive:
            if copy_file(pair.onedrive_dir, pair.local_dir, rel_path, verbose, dry_run):
                to_local_files.append(rel_path)
            else:
                failed_paths.append(rel_path)

    return to_onedrive_files, to_local_files, conflicts, failed_paths


def refresh_pair_state(
    pair: SyncPair,
    seed_state: dict | None = None,
    force_full: bool = False,
    force_paths: set[str] | None = None,
) -> dict:
    # Unreadable paths are omitted here. Callers preserve them from the previous
    # state through mismatched_paths_in_state() or an explicit blocked set.
    seed_state = seed_state or {}
    state, _, filtered = scan_pair(pair, seed_state, force_full, force_paths)
    return drop_state_paths(state, filtered)


def scan_pair(
    pair: SyncPair,
    seed_state: dict,
    force_full: bool = False,
    force_paths: set[str] | None = None,
) -> tuple[dict, set[str], set[str]]:
    """Scan both sides; return (state, unreadable paths, filtered-out paths)."""

    state: dict = {"timestamp": datetime.now().isoformat()}
    unreadable: set[str] = set()
    filtered: set[str] = set()
    for side, base in (("local", pair.local_dir), ("onedrive", pair.onedrive_dir)):
        dirs: set[str] = set()
        checksums, mtimes, signatures, side_unreadable = fast_scan_directory(
            base,
            seed_state.get(side, {}),
            seed_state.get(f"{side}_signatures", {}),
            force_full=force_full,
            force_paths=force_paths,
            path_filter=pair.filter,
            filtered_out=filtered,
            dirs_out=dirs,
        )
        state[side] = checksums
        state[f"{side}_dirs"] = sorted(dirs)
        state[f"{side}_mtimes"] = mtimes
        state[f"{side}_signatures"] = signatures
        unreadable.update(side_unreadable)
    return state, unreadable, filtered


def drop_state_paths(pair_state: dict, paths: set[str]) -> dict:
    """Remove paths from every per-path field of a pair state."""

    if not paths:
        return pair_state
    dropped = dict(pair_state)
    for name in STATE_PATH_FIELDS:
        dropped[name] = without_paths(pair_state.get(name, {}), paths)
    return dropped


STATE_PATH_FIELDS = (
    "local",
    "onedrive",
    "local_mtimes",
    "onedrive_mtimes",
    "local_signatures",
    "onedrive_signatures",
)


def filter_excluded_state(
    pair_state: dict | None, pair: SyncPair | None = None
) -> dict | None:
    """Drop excluded paths from a state, so an exclusion never looks like a deletion."""

    if pair_state is None:
        return None
    filtered = dict(pair_state)
    for name in STATE_PATH_FIELDS:
        values = pair_state.get(name, {})
        filtered[name] = {
            path: value
            for path, value in values.items()
            if not should_exclude_rel_path(path)
            and not (pair and pair.filter.excludes_path(path))
        }
    return filtered


def preserve_state_for_paths(
    next_state: dict,
    previous_state: dict | None,
    paths: set[str],
) -> None:
    previous_state = previous_state or {}
    for field in STATE_PATH_FIELDS:
        destination = next_state.setdefault(field, {})
        previous = previous_state.get(field, {})
        for path in paths:
            if path in previous:
                destination[path] = previous[path]
            else:
                destination.pop(path, None)


def mismatched_paths_in_state(pair_state: dict) -> set[str]:
    local = pair_state.get("local", {})
    onedrive = pair_state.get("onedrive", {})
    return {
        path
        for path in set(local) | set(onedrive)
        if local.get(path) != onedrive.get(path)
    }


def rule_decision(pair: SyncPair, conflict: dict, rule: str) -> str | None:
    """Return the side a conflict rule keeps, or None to leave it for review.

    A rule never deletes: if the side it would keep was deleted, the conflict
    stays open. Name conflicts and same-second edits are always left open.
    """

    if rule == "ask" or conflict.get("related_file"):
        return None
    if rule == "safe":
        return safe_decision(pair, conflict)[0]
    local, onedrive, newer = conflict_versions(pair, conflict["file"])
    if rule == "newer":
        if local is None or onedrive is None:
            return "local" if local else ("onedrive" if onedrive else None)
        return newer
    kept = local if rule == "local" else onedrive
    return rule if kept is not None else None


def folder_rule_for(rel_path: str) -> str | None:
    """The first folder rule whose pattern matches the path."""

    for folder_rule in require_config().folder_rules:
        if rel_path_matches_pattern(rel_path, folder_rule.pattern):
            return folder_rule.rule
    return None


def protect_deletions(actions: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """In protected folders a deletion is undone: the file is copied back instead."""

    restore = {"delete_local": "copy_local_to_onedrive", "delete_onedrive": "copy_onedrive_to_local"}
    return [
        (restore[action], rel_path)
        if action in restore and folder_rule_for(rel_path) == "protect"
        else (action, rel_path)
        for action, rel_path in actions
    ]


def author_decision(pair: SyncPair, rel_path: str) -> str | None:
    """Keep the version saved by the higher-ranked author (Office files only).

    Both authors must be in the list and differ, and neither version may be damaged;
    otherwise nothing is decided.
    """

    ranking = [name.casefold() for name in require_config().author_priority]
    ranks = {}
    for side in ("local", "onedrive"):
        path = side_dir(pair, side) / rel_path
        if file_health(path) == "damaged":
            return None
        info = office_info(path, content=False)
        author = (info or {}).get("author") or ""
        if author.strip().casefold() not in ranking:
            return None
        ranks[side] = ranking.index(author.strip().casefold())
    if ranks["local"] == ranks["onedrive"]:
        return None
    return "local" if ranks["local"] < ranks["onedrive"] else "onedrive"


def fixed_rule_decision(pair: SyncPair, conflict: dict) -> tuple[str | None, str]:
    """Decide a conflict by folder rule, then author priority; returns (side, rule name)."""

    if conflict.get("related_file"):
        return None, ""
    rel_path = conflict["file"]
    rule = folder_rule_for(rel_path)
    local, onedrive, newer = conflict_versions(pair, rel_path)
    if rule == "protect" and (local is None) != (onedrive is None):
        return ("local" if local else "onedrive"), "protect"
    if rule in {"keep_local", "keep_onedrive", "keep_newer"}:
        keep = rule_decision(pair, conflict, rule.removeprefix("keep_"))
        if keep:
            return keep, rule
    if rule == "keep_older" and local and onedrive and newer:
        return other_side(newer), rule
    if local and onedrive and require_config().author_priority:
        keep = author_decision(pair, rel_path)
        if keep:
            return keep, "author priority"
    return None, ""


def copy_action_for(keep: str) -> str:
    return "copy_local_to_onedrive" if keep == "local" else "copy_onedrive_to_local"


def resolve_conflicts_for_run(
    pair: SyncPair,
    conflicts: list[dict],
    interactive: bool,
    verbose: bool = False,
    dry_run: bool = False,
) -> tuple[list[tuple[str, str]], list[dict]]:
    """Apply the configured conflict rule, then ask about the rest if interactive."""

    rule = require_config().conflict_rule
    actions: list[tuple[str, str]] = []
    open_conflicts: list[dict] = []
    for conflict in conflicts:
        keep, reason = fixed_rule_decision(pair, conflict)
        if keep is None and rule == "safe":
            keep, detail = safe_decision(pair, conflict)
            reason = f"safe: {detail}"
        elif keep is None:
            keep, reason = rule_decision(pair, conflict, rule), rule
        if keep is None:
            open_conflicts.append(conflict)
            continue
        actions.append((copy_action_for(keep), conflict["file"]))
        log_msg(
            f"  AUTO-RESOLVED [{pair.name}] ({reason}) kept {keep}: {conflict['file']}",
            verbose,
        )
    if interactive and open_conflicts:
        asked_actions, open_conflicts = _resolve_conflicts_interactive(
            pair, open_conflicts, verbose, dry_run
        )
        actions.extend(asked_actions)
    return actions, open_conflicts


def run_copy_actions(
    pair: SyncPair,
    actions: list[tuple[str, str]],
    verbose: bool = False,
    dry_run: bool = False,
) -> tuple[list[str], list[str], set[str]]:
    """Run copy actions; return (to OneDrive, to local, failed paths)."""

    to_onedrive: list[str] = []
    to_local: list[str] = []
    failed: set[str] = set()
    for action, rel_path in actions:
        done = to_onedrive if action == "copy_local_to_onedrive" else to_local
        if copy_for_action(pair, action, rel_path, verbose, dry_run):
            done.append(rel_path)
        else:
            failed.add(rel_path)
    return to_onedrive, to_local, failed


def _resolve_conflicts_interactive(
    pair: SyncPair,
    conflicts: list[dict],
    verbose: bool = False,
    dry_run: bool = False,
) -> tuple[list[tuple[str, str]], list[dict]]:
    copy_actions: list[tuple[str, str]] = []
    unresolved: list[dict] = []
    for conflict in conflicts:
        if conflict.get("related_file"):
            unresolved.append(conflict)
            log_msg(
                f"  SKIPPED name conflict [{pair.name}]: {conflict['file']} <-> {conflict['related_file']}",
                verbose,
            )
            continue
        direction = show_conflict_dialog(pair, conflict, verbose, dry_run)
        rel_path = conflict["file"]
        if direction == "local":
            copy_actions.append(("copy_local_to_onedrive", rel_path))
            log_msg(f"  RESOLVED [{pair.name}] kept local: {rel_path}", verbose)
        elif direction == "onedrive":
            copy_actions.append(("copy_onedrive_to_local", rel_path))
            log_msg(f"  RESOLVED [{pair.name}] kept OneDrive: {rel_path}", verbose)
        else:
            unresolved.append(conflict)
            log_msg(f"  SKIPPED conflict [{pair.name}]: {rel_path}", verbose)
    return copy_actions, unresolved


def renames_follow_directions(action: str, directions: dict[str, str]) -> bool:
    side, target = ("local", "to_onedrive") if action == "rename_onedrive" else ("onedrive", "to_local")
    return directions[f"{side}_new"] == target and directions[f"{side}_deleted"] == target


OVERRIDE_ACTIONS = (
    "skip",
    "copy_local_to_onedrive",
    "copy_onedrive_to_local",
    "delete_local",
    "delete_onedrive",
)


def apply_overrides(
    pair: SyncPair,
    actions: list[tuple[str, str]],
    conflicts: list[dict],
    overrides: dict[str, str],
) -> tuple[list[tuple[str, str]], list[dict], list[tuple[str, str]], set[str]]:
    """Replace planned actions and conflicts by the actions chosen in the preview.

    Returns (actions, conflicts, chosen deletions, skipped paths). Skipped paths
    keep their previous state, so they show up again in the next preview.
    """

    if not overrides:
        return actions, conflicts, [], set()
    actions = [item for item in actions if item[1] not in overrides]
    conflicts = [item for item in conflicts if item["file"] not in overrides]
    deletes: list[tuple[str, str]] = []
    skipped: set[str] = set()
    for rel_path, action in sorted(overrides.items()):
        if action not in OVERRIDE_ACTIONS:
            raise ValueError(f"Unknown override action for {rel_path}: {action}")
        if action == "skip":
            skipped.add(rel_path)
        elif action.startswith("delete_"):
            deletes.append((action, rel_path))
        else:
            source = "local" if action == "copy_local_to_onedrive" else "onedrive"
            if (side_dir(pair, source) / rel_path).exists():
                actions.append((action, rel_path))
            else:
                skipped.add(rel_path)
    return actions, conflicts, deletes, skipped


FOLDER_RESULT_FIELDS = {
    "copy_local_to_onedrive": "created_onedrive_dirs",
    "copy_onedrive_to_local": "created_local_dirs",
    "delete_onedrive": "deleted_onedrive_dirs",
    "delete_local": "deleted_local_dirs",
}


def tree_has_files(path: Path) -> bool:
    """True if a folder holds any file other than Finder/Windows metadata."""

    for _, _, files in os.walk(path):
        if any(name not in DEFAULT_EXCLUDE_NAMES for name in files):
            return True
    return False


def sync_folders(
    pair: SyncPair,
    prev_state: dict | None,
    scan_state: dict,
    directions: dict[str, str],
    verbose: bool = False,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """Create and remove folders (in particular empty ones) like files.

    Folders are compared as if they were files with a fixed checksum, so the
    variant decides the same way. Only folders without files are created here:
    folders with files come with their files, or stay away while their
    deletion waits for review. A state without folder lists (written by an
    older version) takes the OneDrive folders as known: laptop-only folders
    are created in OneDrive, old empty OneDrive leftovers are left alone. A folder is only removed while it holds no files, and
    never when the other side still has it under a different capitalisation.
    """

    def as_files(state: dict | None, side: str) -> dict[str, str]:
        return dict.fromkeys((state or {}).get(f"{side}_dirs", []), "folder")

    current = {side: as_files(scan_state, side) for side in ("local", "onedrive")}
    upgrading = prev_state is not None and "onedrive_dirs" not in prev_state
    actions, _ = classify_changes(
        current["local"],
        current["onedrive"],
        as_files(prev_state, "local"),
        current["onedrive"] if upgrading else as_files(prev_state, "onedrive"),
        directions,
    )
    canonical = {side: {canonical_rel_path(path) for path in paths} for side, paths in current.items()}
    holding_files = {
        side: {str(parent) for path in scan_state.get(side, {}) for parent in Path(path).parents}
        for side in ("local", "onedrive")
    }
    results: dict[str, list[str]] = {field: [] for field in FOLDER_RESULT_FIELDS.values()}
    for action, rel_path in actions:
        side, other = ("onedrive", "local") if action.endswith("onedrive") else ("local", "onedrive")
        target = side_dir(pair, side) / rel_path
        if action.startswith("copy"):
            if target.exists() or rel_path in holding_files[other]:
                continue
            if not dry_run:
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    log_msg(f"  ERROR creating folder {rel_path}: {exc}", verbose)
                    continue
            log_msg(f"  {'[DRY RUN] Would create' if dry_run else 'Created'} folder: {rel_path}", verbose)
        else:
            if (
                canonical_rel_path(rel_path) in canonical[other]  # case-only rename
                or not target.is_dir()
                or tree_has_files(target)
            ):
                continue
            if not dry_run:
                try:
                    shutil.rmtree(target)
                except OSError as exc:
                    log_msg(f"  ERROR removing folder {rel_path}: {exc}", verbose)
                    continue
            log_msg(f"  {'[DRY RUN] Would remove' if dry_run else 'Removed'} empty folder: {rel_path}", verbose)
        results[FOLDER_RESULT_FIELDS[action]].append(rel_path)
    return results


def pair_result(pair: SyncPair, state: dict, **fields) -> dict:
    result = {
        "pair": pair,
        "state": state,
        "skipped": False,
        "skip_reason": None,
        "to_onedrive_files": [],
        "to_local_files": [],
        "deleted_onedrive_files": [],
        "deleted_local_files": [],
        "renamed_onedrive_files": [],
        "renamed_local_files": [],
        "created_onedrive_dirs": [],
        "created_local_dirs": [],
        "deleted_onedrive_dirs": [],
        "deleted_local_dirs": [],
        "skipped_delete_files": [],
        "skipped_delete_actions": [],
        "unreadable_files": [],
        "conflicts": [],
        "errors": 0,
    }
    unknown = set(fields) - set(result)
    if unknown:
        raise TypeError(f"Unknown pair result fields: {sorted(unknown)}")
    result.update(fields)
    return result


def skipped_pair_result(pair: SyncPair, state: dict | None, reason: str) -> dict:
    return pair_result(
        pair, state or {}, skipped=True, skip_reason=reason, errors=1
    )


def without_paths(mapping: dict[str, str], paths: set[str]) -> dict[str, str]:
    return {path: value for path, value in mapping.items() if path not in paths}


def sync_pair(
    pair: SyncPair,
    prev_pair_state: dict | None,
    timestamp: str,
    verbose: bool = False,
    dry_run: bool = False,
    no_delete: bool = True,
    interactive: bool = False,
    force_full: bool = False,
    delete_limit: int | None = None,
    held_deletions: set[tuple[str, str]] | None = None,
    overrides: dict[str, str] | None = None,
) -> dict:
    """Sync one pair.

    With delete_limit set, deletions run without a dialog as long as at most
    delete_limit of them are new. Deletions already held for review
    (held_deletions) stay held until they are reviewed. overrides maps a file
    to an action chosen in the preview ("skip" or a copy/delete action).
    """

    log_msg(f"Pair [{pair.name}] start", verbose)
    prev_pair_state = filter_excluded_state(prev_pair_state, pair)
    directions = effective_directions(pair)
    if prev_pair_state is None and pair.variant != "two-way":
        # One-way variants decide by direction, even on the first run.
        prev_pair_state = {"local": {}, "onedrive": {}, "local_dirs": [], "onedrive_dirs": []}

    if not pair.local_dir.exists():
        reason = "Local root directory is missing"
        log_msg(f"ERROR [{pair.name}]: {reason}. Skipping pair.", verbose)
        return skipped_pair_result(pair, prev_pair_state, reason)
    if not directory_readable(pair.local_dir):
        reason = "Local directory is not readable"
        log_msg(f"ERROR [{pair.name}]: {reason}. Skipping pair.", verbose)
        return skipped_pair_result(pair, prev_pair_state, reason)
    if not pair.onedrive_dir.exists():
        reason = "OneDrive root directory is missing"
        log_msg(f"ERROR [{pair.name}]: {reason}. Skipping pair.", verbose)
        return skipped_pair_result(pair, prev_pair_state, reason)
    if not directory_readable(pair.onedrive_dir):
        reason = "OneDrive directory is not readable"
        log_msg(f"ERROR [{pair.name}]: {reason}. Skipping pair.", verbose)
        return skipped_pair_result(pair, prev_pair_state, reason)

    has_signatures = bool(
        prev_pair_state
        and "local_signatures" in prev_pair_state
        and "onedrive_signatures" in prev_pair_state
    )
    full_scan = force_full or not has_signatures
    mode = "full verification" if full_scan else "safe fast mode"
    try:
        log_msg(f"Scanning [{pair.name}] ({mode})...", verbose)
        scan_state, unreadable_paths, filtered_paths = scan_pair(
            pair, prev_pair_state or {}, force_full=full_scan
        )
    except ScanIncompleteError as exc:
        reason = str(exc)
        log_msg(f"ERROR [{pair.name}]: {reason}. No actions taken.", verbose)
        return skipped_pair_result(pair, prev_pair_state, reason)

    # Files outside the size/age filter are left alone on both sides.
    scan_state = drop_state_paths(scan_state, filtered_paths)
    if prev_pair_state is not None:
        prev_pair_state = drop_state_paths(prev_pair_state, filtered_paths)
    current_local, current_onedrive = scan_state["local"], scan_state["onedrive"]
    log_msg(
        f"  Found {len(current_local)} local and {len(current_onedrive)} OneDrive files",
        verbose,
    )

    # Unreadable files are frozen: no copy, rename or deletion is derived from
    # them, and their previous baseline is kept until they can be read again.
    for rel_path in sorted(unreadable_paths)[:SUMMARY_PATH_LIMIT]:
        log_msg(f"  UNREADABLE [{pair.name}], left untouched: {rel_path}", verbose)
    active_local = without_paths(current_local, unreadable_paths)
    active_onedrive = without_paths(current_onedrive, unreadable_paths)

    if prev_pair_state is None:
        log_msg(f"First run for pair [{pair.name}] — initializing state...", verbose)
        to_onedrive_files, to_local_files, conflicts, failed_paths = initial_sync(
            pair, active_local, active_onedrive, verbose, dry_run
        )
        resolved_actions, conflicts = resolve_conflicts_for_run(
            pair, conflicts, interactive, verbose, dry_run
        )
        resolved_onedrive, resolved_local, resolved_failed = run_copy_actions(
            pair, resolved_actions, verbose, dry_run
        )
        to_onedrive_files += resolved_onedrive
        to_local_files += resolved_local
        failed_paths += sorted(resolved_failed)
        folder_results = sync_folders(pair, None, scan_state, directions, verbose, dry_run)

        next_state: dict = {}
        post_scan_error = None
        if not dry_run:
            affected = set(to_onedrive_files + to_local_files + failed_paths)
            try:
                next_state = refresh_pair_state(
                    pair, scan_state, force_paths=affected
                )
                preserve_state_for_paths(
                    next_state,
                    None,
                    paths_from_conflicts(conflicts)
                    | set(failed_paths)
                    | unreadable_paths
                    | mismatched_paths_in_state(next_state),
                )
            except ScanIncompleteError as exc:
                post_scan_error = str(exc)
                next_state = {}
                log_msg(f"ERROR [{pair.name}]: Post-sync verification failed: {exc}", verbose)

        return pair_result(
            pair,
            next_state,
            skip_reason=post_scan_error,
            to_onedrive_files=to_onedrive_files,
            to_local_files=to_local_files,
            unreadable_files=sorted(unreadable_paths),
            conflicts=conflicts,
            errors=len(failed_paths) + (1 if post_scan_error else 0),
            **folder_results,
        )

    active_prev_local = without_paths(prev_pair_state.get("local", {}), unreadable_paths)
    active_prev_onedrive = without_paths(
        prev_pair_state.get("onedrive", {}), unreadable_paths
    )
    rename_actions, _ = find_safe_renames(
        active_local,
        active_onedrive,
        active_prev_local,
        active_prev_onedrive,
    )
    # A rename is only mirrored when the variant propagates both the new and
    # the deleted name of the renamed side.
    rename_actions = [
        rename
        for rename in rename_actions
        if renames_follow_directions(rename[0], directions)
        and not ({rename[1], rename[2]} & set(overrides or {}))
    ]
    rename_paths = {path for _, old, new in rename_actions for path in (old, new)}
    name_conflicts, name_conflict_paths = find_name_mismatch_conflicts(
        without_paths(active_local, rename_paths),
        without_paths(active_onedrive, rename_paths),
    )
    ignored_paths = rename_paths | name_conflict_paths
    comparable_local = without_paths(active_local, ignored_paths)
    comparable_onedrive = without_paths(active_onedrive, ignored_paths)
    comparable_prev_local = without_paths(active_prev_local, ignored_paths)
    comparable_prev_onedrive = without_paths(active_prev_onedrive, ignored_paths)
    actions, conflicts = classify_changes(
        comparable_local,
        comparable_onedrive,
        comparable_prev_local,
        comparable_prev_onedrive,
        directions,
    )
    actions, conflicts, override_deletes, skipped_by_override = apply_overrides(
        pair, actions, conflicts, overrides or {}
    )
    actions = protect_deletions(actions)
    conflicts = name_conflicts + conflicts
    conflicts = [{"pair": pair.key, **conflict} for conflict in conflicts]
    copy_actions = [item for item in actions if "delete" not in item[0]]
    delete_actions = [item for item in actions if "delete" in item[0]]

    labels = {
        "copy_local_to_onedrive": "Local -> OneDrive",
        "copy_onedrive_to_local": "OneDrive -> Local",
        "delete_onedrive": "Delete from OneDrive",
        "delete_local": "Delete from Local",
    }
    rename_labels = {
        "rename_onedrive": "Rename on OneDrive",
        "rename_local": "Rename on Local",
    }
    for action, source_path, destination_path in rename_actions:
        log_msg(
            f"  [{pair.name}] {rename_labels[action]}: "
            f"{source_path} -> {destination_path}",
            verbose,
        )
    for action, rel_path in copy_actions:
        log_msg(f"  [{pair.name}] {labels[action]}: {rel_path}", verbose)
    for action, rel_path in delete_actions[:SUMMARY_PATH_LIMIT]:
        log_msg(f"  [{pair.name}] {labels[action]}: {rel_path}", verbose)
    if len(delete_actions) > SUMMARY_PATH_LIMIT:
        log_msg(
            f"  ... and {len(delete_actions) - SUMMARY_PATH_LIMIT} more pending delete actions "
            f"for [{pair.name}]",
            verbose,
        )
    for conflict in conflicts:
        log_msg(f"  CONFLICT [{pair.name}]: {conflict['file']} ({conflict['reason']})", verbose)

    resolved_actions, conflicts = resolve_conflicts_for_run(
        pair, conflicts, interactive, verbose, dry_run
    )
    copy_actions.extend(resolved_actions)

    failed_paths: set[str] = set()
    renamed_onedrive_files: list[tuple[str, str]] = []
    renamed_local_files: list[tuple[str, str]] = []
    for action, source_path, destination_path in rename_actions:
        base_dir = pair.onedrive_dir if action == "rename_onedrive" else pair.local_dir
        if rename_file(base_dir, source_path, destination_path, verbose, dry_run):
            renamed = (source_path, destination_path)
            if action == "rename_onedrive":
                renamed_onedrive_files.append(renamed)
            else:
                renamed_local_files.append(renamed)
        else:
            failed_paths.update({source_path, destination_path})

    to_onedrive_files, to_local_files, copy_failed = run_copy_actions(
        pair, copy_actions, verbose, dry_run
    )
    failed_paths |= copy_failed

    deleted_onedrive_files: list[str] = []
    deleted_local_files: list[str] = []
    held_deletions = held_deletions or set()
    to_hold: list[tuple[str, str]] = []
    hold_reason = ""
    if delete_actions and no_delete:
        to_hold, hold_reason = list(delete_actions), "--no-delete"
    elif delete_actions and delete_limit is not None:
        new_actions = [item for item in delete_actions if item not in held_deletions]
        if len(new_actions) > delete_limit:
            to_hold = list(delete_actions)
            hold_reason = f"{len(new_actions)} new deletions exceed limit {delete_limit}"
        else:
            to_hold = [item for item in delete_actions if item in held_deletions]
            hold_reason = "awaiting review"
    elif delete_actions and interactive and not dry_run:
        printable_items = [
            f"[{pair.name}] {'OneDrive' if action == 'delete_onedrive' else 'Local'}: {path}"
            for action, path in delete_actions
        ]
        if not show_deletion_dialog(printable_items):
            to_hold, hold_reason = list(delete_actions), "cancelled by user"

    held_set = set(to_hold)
    delete_actions = [item for item in delete_actions if item not in held_set]
    delete_actions += override_deletes  # chosen explicitly in the preview
    skipped_delete_actions = to_hold
    skipped_delete_files = [path for _, path in to_hold]
    for rel_path in skipped_delete_files[:SUMMARY_PATH_LIMIT]:
        log_msg(f"  HELD delete [{pair.name}] ({hold_reason}): {rel_path}", verbose)
    if len(skipped_delete_files) > SUMMARY_PATH_LIMIT:
        log_msg(
            f"  ... and {len(skipped_delete_files) - SUMMARY_PATH_LIMIT} more held deletes "
            f"for [{pair.name}]",
            verbose,
        )

    for action, rel_path in delete_actions:
        if delete_from_side(pair, action, rel_path, verbose, dry_run):
            if action == "delete_onedrive":
                deleted_onedrive_files.append(rel_path)
            else:
                deleted_local_files.append(rel_path)
        else:
            failed_paths.add(rel_path)

    folder_results = sync_folders(pair, prev_pair_state, scan_state, directions, verbose, dry_run)

    next_state = prev_pair_state
    post_scan_error = None
    if not dry_run:
        affected_paths = {
            path for _, path in copy_actions + delete_actions
        } | {
            path
            for _, source_path, destination_path in rename_actions
            for path in (source_path, destination_path)
        } | failed_paths
        try:
            next_state = refresh_pair_state(
                pair, scan_state, force_paths=affected_paths
            )
            blocked_paths = (
                paths_from_conflicts(conflicts)
                | skipped_by_override
                | set(skipped_delete_files)
                | failed_paths
                | unreadable_paths
                | mismatched_paths_in_state(next_state)
            )
            preserve_state_for_paths(next_state, prev_pair_state, blocked_paths)
        except ScanIncompleteError as exc:
            post_scan_error = str(exc)
            next_state = prev_pair_state
            log_msg(f"ERROR [{pair.name}]: Post-sync verification failed: {exc}", verbose)

    return pair_result(
        pair,
        next_state,
        skip_reason=post_scan_error,
        to_onedrive_files=to_onedrive_files,
        to_local_files=to_local_files,
        deleted_onedrive_files=deleted_onedrive_files,
        deleted_local_files=deleted_local_files,
        renamed_onedrive_files=renamed_onedrive_files,
        renamed_local_files=renamed_local_files,
        skipped_delete_files=skipped_delete_files,
        skipped_delete_actions=skipped_delete_actions,
        unreadable_files=sorted(unreadable_paths),
        conflicts=conflicts,
        errors=len(failed_paths) + (1 if post_scan_error else 0),
        **folder_results,
    )


CHANGE_FIELDS = (
    "to_onedrive_files",
    "to_local_files",
    "deleted_onedrive_files",
    "deleted_local_files",
    "renamed_onedrive_files",
    "renamed_local_files",
    *FOLDER_RESULT_FIELDS.values(),
)


def change_count(result: dict) -> int:
    return sum(len(result.get(field, [])) for field in CHANGE_FIELDS)


def write_summary(timestamp: str, results: list[dict], dry_run: bool = False) -> None:
    config = require_config()
    prefix = "[DRY RUN] " if dry_run else ""
    lines = [f"{prefix}Sync Summary — {timestamp}", "=" * 60]

    total_changes = 0
    total_conflicts = 0

    def append_paths(label: str, paths: list[str], unit: str = "files") -> None:
        lines.append(f"{label} ({len(paths)} {unit}):")
        lines.extend(f"  {path}" for path in paths[:SUMMARY_PATH_LIMIT])
        if len(paths) > SUMMARY_PATH_LIMIT:
            lines.append(f"  ... and {len(paths) - SUMMARY_PATH_LIMIT} more")

    def append_renames(label: str, renames: list[tuple[str, str]]) -> None:
        lines.append(f"{label} ({len(renames)} files):")
        lines.extend(
            f"  {source} -> {destination}"
            for source, destination in renames[:SUMMARY_PATH_LIMIT]
        )
        if len(renames) > SUMMARY_PATH_LIMIT:
            lines.append(f"  ... and {len(renames) - SUMMARY_PATH_LIMIT} more")

    for result in results:
        pair = result["pair"]
        lines.append("")
        lines.append(f"[{pair.name}]")
        lines.append(f"Local:    {pair.local_dir}")
        lines.append(f"OneDrive: {pair.onedrive_dir}")

        if result["skipped"]:
            lines.append(f"Skipped: {result['skip_reason']}")
            continue

        to_local_files = result["to_local_files"]
        to_onedrive_files = result["to_onedrive_files"]
        deleted_onedrive_files = result["deleted_onedrive_files"]
        deleted_local_files = result["deleted_local_files"]
        renamed_onedrive_files = result.get("renamed_onedrive_files", [])
        renamed_local_files = result.get("renamed_local_files", [])
        skipped_delete_files = result["skipped_delete_files"]
        conflicts = result["conflicts"]

        if to_local_files:
            append_paths("OneDrive -> Local", to_local_files)

        if to_onedrive_files:
            append_paths("Local -> OneDrive", to_onedrive_files)

        if deleted_onedrive_files:
            append_paths("Deleted from OneDrive", deleted_onedrive_files)

        if deleted_local_files:
            append_paths("Deleted from Local", deleted_local_files)

        if renamed_onedrive_files:
            append_renames("Renamed on OneDrive", renamed_onedrive_files)

        if renamed_local_files:
            append_renames("Renamed on Local", renamed_local_files)

        for field, label in (
            ("created_onedrive_dirs", "Folders created on OneDrive"),
            ("created_local_dirs", "Folders created on Local"),
            ("deleted_onedrive_dirs", "Empty folders removed from OneDrive"),
            ("deleted_local_dirs", "Empty folders removed from Local"),
        ):
            if result.get(field):
                append_paths(label, result[field], "folders")

        if skipped_delete_files:
            append_paths("Skipped deletes", skipped_delete_files)

        if result.get("unreadable_files"):
            append_paths("Unreadable, left untouched", result["unreadable_files"])

        if conflicts:
            lines.append(f"Unresolved conflicts ({len(conflicts)}):")
            for conflict in conflicts:
                lines.append(f"  {conflict['file']}: {conflict['reason']}")

        pair_changes = change_count(result)
        total_changes += pair_changes
        total_conflicts += len(conflicts)
        if pair_changes == 0 and not conflicts and not result["skipped"]:
            lines.append("No changes detected.")

    lines.append("")
    if total_changes == 0 and total_conflicts == 0 and not any(result["skipped"] for result in results):
        lines.append("Everything is in sync.")

    atomic_write_text(config.summary_file, "\n".join(lines))


def print_results_summary(
    results: list[dict], summary_written: bool = True, quiet: bool = False
) -> str:
    config = require_config()
    total_synced = 0
    total_errors = 0
    total_conflicts = 0
    total_skipped_deletes = 0
    total_skipped_pairs = 0
    total_unreadable = 0

    for result in results:
        total_errors += result["errors"]
        total_unreadable += len(result.get("unreadable_files", []))
        if result["skipped"]:
            total_skipped_pairs += 1
            continue
        total_synced += change_count(result)
        total_conflicts += len(result["conflicts"])
        total_skipped_deletes += len(result["skipped_delete_files"])

    parts = []
    if total_synced > 0:
        parts.append(f"{total_synced} synced")
    if total_errors > 0:
        parts.append(f"{total_errors} error(s)")
    if total_conflicts > 0:
        parts.append(f"{total_conflicts} conflict(s)")
    if total_skipped_deletes > 0:
        parts.append(f"{total_skipped_deletes} deletion(s) pending")
    if total_skipped_pairs > 0:
        parts.append(f"{total_skipped_pairs} pair(s) not verified")
    if total_unreadable > 0:
        parts.append(f"{total_unreadable} file(s) unreadable")

    if parts:
        summary = ", ".join(parts)
        log_msg(summary, verbose=not quiet)
        if summary_written and not quiet:
            print(f"\nSummary written to: {config.summary_file}")
        if (
            summary_written
            and total_synced > 0
            and total_errors == 0
            and total_conflicts == 0
            and total_skipped_deletes == 0
            and total_skipped_pairs == 0
        ):
            notify("Sync Complete", summary)
        return summary
    else:
        log_msg("No changes detected.", verbose=not quiet)
        if not quiet:
            print("No changes detected. Everything is in sync.")
    return "No changes detected."


def sync_all(
    verbose: bool = False,
    dry_run: bool = False,
    no_delete: bool = True,
    interactive: bool = False,
    force_full: bool = False,
    delete_limit: int | None = None,
    pair_names: list[str] | None = None,
    overrides: list[dict] | None = None,
    quiet: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Sync all pairs, or only pair_names (other pairs keep their state).

    overrides are per-file actions chosen in the preview: {pair, file, action}.
    quiet prints nothing to stdout (used for JSON output).
    """

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg("=" * 60, verbose)
    log_msg("Starting sync...", verbose)

    config = require_config()
    if not dry_run:
        prune_trash()
    held_by_pair: dict[str, set[tuple[str, str]]] = {}
    for item in load_pending_deletion_snapshot() or []:
        held_by_pair.setdefault(item.pair, set()).add((item.action, item.file))
    prev_state = load_state()
    pair_states = dict(prev_state.get("pairs", {})) if prev_state else {}

    results = []
    next_pair_states = dict(pair_states)
    all_conflicts = []
    existing_conflicts = load_conflicts()

    overrides_by_pair: dict[str, dict[str, str]] = {}
    for item in overrides or []:
        overrides_by_pair.setdefault(item["pair"], {})[item["file"]] = item["action"]
    selected = [pair for pair in config.pairs if pair_names is None or pair.key in pair_names]
    selected_keys = {pair.key for pair in selected}
    all_conflicts.extend(
        conflict for conflict in existing_conflicts if conflict.get("pair") not in selected_keys
    )

    for pair in selected:
        result = sync_pair(
            pair=pair,
            prev_pair_state=pair_states.get(pair.key),
            timestamp=timestamp,
            verbose=verbose,
            dry_run=dry_run,
            no_delete=no_delete,
            interactive=interactive,
            force_full=force_full,
            delete_limit=delete_limit,
            held_deletions=held_by_pair.get(pair.key, set()),
            overrides=overrides_by_pair.get(pair.key),
        )
        results.append(result)
        if not result["skipped"] or pair.key in next_pair_states:
            next_pair_states[pair.key] = result["state"]
        if result["skipped"]:
            all_conflicts.extend(
                conflict
                for conflict in existing_conflicts
                if conflict.get("pair") == pair.key
            )
        else:
            all_conflicts.extend(result["conflicts"])

    if not dry_run:
        save_conflicts(all_conflicts)
        write_summary(timestamp, results, dry_run=False)
        save_state(next_pair_states)
        save_pending_deletions_from_results(results, next_pair_states)

    def conflict_identity(conflict: dict) -> tuple:
        return (
            conflict.get("pair"),
            conflict.get("file"),
            conflict.get("related_file"),
            conflict.get("local_hash"),
            conflict.get("onedrive_hash"),
            conflict.get("reason"),
        )

    existing_conflict_ids = {conflict_identity(item) for item in existing_conflicts}
    new_conflicts = [
        conflict
        for conflict in all_conflicts
        if conflict_identity(conflict) not in existing_conflict_ids
    ]
    if new_conflicts and not dry_run:
        notify(
            "Sync: New conflicts!",
            f"{len(new_conflicts)} new conflict(s) need manual resolution. Click to review.",
            open="conflicts",
        )

    summary = print_results_summary(results, summary_written=not dry_run, quiet=quiet)
    if not dry_run:
        atomic_write_json(
            status_file(),
            {"timestamp": datetime.now().isoformat(timespec="seconds"), "summary": summary},
        )
        run_after_sync_command()
    return results, all_conflicts


def run_after_sync_command() -> None:
    """Run the configured command after a sync; it gets the status file path."""

    command = require_config().run_after_sync
    if not command:
        return
    try:
        subprocess.run(
            command,
            shell=True,
            env={**os.environ, "SYNCBRIDGE_STATUS": str(status_file())},
            timeout=300,
            check=False,
        )
    except Exception as exc:
        log_msg(f"ERROR running command after sync: {exc}")


PREVIEW_FIELDS = (
    ("to_onedrive_files", "copy_local_to_onedrive"),
    ("to_local_files", "copy_onedrive_to_local"),
    ("deleted_onedrive_files", "delete_onedrive"),
    ("deleted_local_files", "delete_local"),
    ("created_onedrive_dirs", "create_folder_onedrive"),
    ("created_local_dirs", "create_folder_local"),
    ("deleted_onedrive_dirs", "delete_folder_onedrive"),
    ("deleted_local_dirs", "delete_folder_local"),
)


def preview_payload(pair_names: list[str] | None = None) -> dict:
    """Plan a sync without changing anything, as FreeFileSync's Compare does."""

    config = require_config()
    results, conflicts = sync_all(
        dry_run=True,
        no_delete=False,
        delete_limit=config.auto_delete_limit,
        pair_names=pair_names,
        quiet=True,
    )
    actions = []
    for result in results:
        pair = result["pair"].key
        for result_field, action in PREVIEW_FIELDS:
            actions.extend({"pair": pair, "file": path, "action": action} for path in result[result_field])
        for action, path in result["skipped_delete_actions"]:
            actions.append({"pair": pair, "file": path, "action": action, "held": True})
        for kind, renamed in (("rename_onedrive", "renamed_onedrive_files"), ("rename_local", "renamed_local_files")):
            actions.extend(
                {"pair": pair, "file": new, "from": old, "action": kind} for old, new in result[renamed]
            )
    actions.sort(key=lambda item: (item["pair"], item["file"]))
    return {
        "actions": actions,
        "conflicts": [
            {key: conflict.get(key) for key in ("pair", "file", "reason")} for conflict in conflicts
        ],
        "skipped_pairs": [
            {"pair": result["pair"].key, "reason": result["skip_reason"]}
            for result in results
            if result["skipped"]
        ],
    }


def save_pending_deletion_snapshot(pending: list[PendingDeletion]) -> None:
    config = require_config()
    atomic_write_json(
        config.pending_deletions_file,
        {
            "timestamp": datetime.now().isoformat(),
            "pending": [
                {
                    "pair": item.pair,
                    "action": item.action,
                    "file": item.file,
                    "expected_hash": item.expected_hash,
                    "expected_signature": item.expected_signature,
                }
                for item in sorted(pending, key=lambda item: item.identity)
            ],
        },
    )


def load_pending_deletion_snapshot() -> list[PendingDeletion] | None:
    path = require_config().pending_deletions_file
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    entries = raw.get("pending", []) if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"Invalid pending deletion snapshot: {path}")
    pending: list[PendingDeletion] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid pending deletion snapshot: {path}")
        action = entry.get("action")
        if action not in {"delete_onedrive", "delete_local"}:
            raise ValueError(f"Invalid deletion action in snapshot: {action}")
        pending.append(
            PendingDeletion(
                pair=entry["pair"],
                action=action,
                file=entry["file"],
                expected_hash=entry.get("expected_hash"),
                expected_signature=entry.get("expected_signature"),
            )
        )
    pending.sort(key=lambda item: item.identity)
    return pending


def save_pending_deletions_from_results(
    results: list[dict],
    pair_states: dict[str, dict],
) -> None:
    existing = load_pending_deletion_snapshot() or []
    synced_pairs = {
        result["pair"].key for result in results if not result.get("skipped")
    }
    pending = [item for item in existing if item.pair not in synced_pairs]
    for result in results:
        if result.get("skipped"):
            continue
        pair_name = result["pair"].key
        pair_state = pair_states.get(pair_name, {})
        for action, rel_path in result.get("skipped_delete_actions", []):
            state_side = "onedrive" if action == "delete_onedrive" else "local"
            pending.append(
                PendingDeletion(
                    pair=pair_name,
                    action=action,
                    file=rel_path,
                    expected_hash=pair_state.get(state_side, {}).get(rel_path),
                    expected_signature=pair_state.get(
                        f"{state_side}_signatures", {}
                    ).get(rel_path),
                )
            )
    save_pending_deletion_snapshot(pending)


def plan_pending_deletions(
    verbose: bool = False,
) -> tuple[list[PendingDeletion], list[str]]:
    """Return protected delete actions without changing files or saved state."""

    state = load_state()
    if state is None:
        return [], ["No previous sync state is available."]

    pair_states = state.get("pairs", {})
    pending: list[PendingDeletion] = []
    issues: list[str] = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for pair in require_config().pairs:
        previous = filter_excluded_state(pair_states.get(pair.key))
        result = sync_pair(
            pair=pair,
            prev_pair_state=previous,
            timestamp=timestamp,
            verbose=verbose,
            dry_run=True,
            no_delete=True,
            interactive=False,
        )
        if result["skipped"] or result["errors"]:
            issues.append(
                f"[{pair.name}] could not be verified: "
                f"{result.get('skip_reason') or 'scan error'}"
            )
            continue
        previous = previous or {}
        for action, rel_path in result.get("skipped_delete_actions", []):
            state_side = "onedrive" if action == "delete_onedrive" else "local"
            signature_side = f"{state_side}_signatures"
            pending.append(
                PendingDeletion(
                    pair=pair.key,
                    action=action,
                    file=rel_path,
                    expected_hash=previous.get(state_side, {}).get(rel_path),
                    expected_signature=previous.get(signature_side, {}).get(rel_path),
                )
            )

    pending.sort(key=lambda item: item.identity)
    return pending, issues


def group_pending_deletions(
    pending: list[PendingDeletion],
) -> list[DeletionGroup]:
    grouped: dict[tuple[str, str, str], DeletionGroup] = {}
    for item in pending:
        staged = staged_exclusion_for_path(item.pair, item.file)
        if staged:
            group_path = staged.root
            label = staged.label
            recommended = True
        else:
            group_path = item.file.rsplit("/", 1)[0] if "/" in item.file else "(pair root)"
            label = group_path
            recommended = False
        key = (item.pair, item.action, group_path)
        group = grouped.get(key)
        if group is None:
            group = DeletionGroup(
                pair=item.pair,
                action=item.action,
                path=group_path,
                label=label,
                recommended=recommended,
                items=[],
            )
            grouped[key] = group
        group.items.append(item)

    groups = list(grouped.values())
    for group in groups:
        group.items.sort(key=lambda item: item.file)
    groups.sort(
        key=lambda group: (
            not group.recommended,
            -len(group.items),
            group.pair,
            group.action,
            group.path,
        )
    )
    return groups


def deletion_group_display_label(group: DeletionGroup) -> str:
    destination = "OneDrive" if group.action == "delete_onedrive" else "Local"
    recommendation = "RECOMMENDED — " if group.recommended else ""
    count_label = "file" if len(group.items) == 1 else "files"
    return (
        f"{recommendation}{len(group.items):,} {count_label} from {destination} — "
        f"{group.label}"
    )


def choose_deletion_groups_dialog(groups: list[DeletionGroup]) -> list[DeletionGroup]:
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            encoding="utf-8",
        ) as handle:
            for index, group in enumerate(groups, start=1):
                handle.write(f"{index}. {deletion_group_display_label(group)}\n")
            temp_path = handle.name
        script = f'''set choiceLines to paragraphs of (read POSIX file "{temp_path}" as «class utf8»)
set selectedItems to choose from list choiceLines with title "Review Pending Deletions" with prompt "Select the folders to restore or delete. Nothing changes on this screen." OK button name "Continue" cancel button name "Cancel" with multiple selections allowed
if selectedItems is false then return ""
set AppleScript's text item delimiters to linefeed
return selectedItems as text'''
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=600,
        )
        selected: list[DeletionGroup] = []
        for line in result.stdout.splitlines():
            try:
                index = int(line.split(".", 1)[0]) - 1
            except (ValueError, IndexError):
                continue
            if 0 <= index < len(groups) and groups[index] not in selected:
                selected.append(groups[index])
        return selected
    except Exception:
        return []
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def write_deletion_review_file(groups: list[DeletionGroup]) -> Path:
    review_file = require_config().state_dir / "pending-deletions-review.txt"
    total = sum(len(group.items) for group in groups)
    lines = [
        f"Pending Deletion Review — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 70,
        f"Selected groups: {len(groups)}",
        f"Selected files: {total}",
        "",
    ]
    for group in groups:
        lines.append(deletion_group_display_label(group))
        lines.append(f"Pair: {group.pair}")
        lines.append(f"Folder: {group.path}")
        for item in group.items:
            lines.append(f"  {item.file}")
        lines.append("")
    atomic_write_text(review_file, "\n".join(lines))
    return review_file


def confirm_selected_deletions(
    groups: list[DeletionGroup],
    review_file: Path,
) -> bool:
    total = sum(len(group.items) for group in groups)
    lines = [
        f"You selected {len(groups)} folder group(s) containing {total:,} file(s).",
        "",
        "These exact remaining copies will be deleted. Their counterparts are already absent.",
        f"Local files are kept in the sync trash for {require_config().trash_retention_days} days; "
        "OneDrive files go to the OneDrive recycle bin.",
        "No unselected file will be deleted.",
        "",
    ]
    for group in groups[:12]:
        lines.append(f"• {deletion_group_display_label(group)}")
    if len(groups) > 12:
        lines.append(f"• ... and {len(groups) - 12} more selected groups")
    lines.extend(["", f"Full exact list: {review_file}"])
    message = "\n".join(lines)

    while True:
        answer = _osascript_with_tempfile(
            message,
            title="Confirm Selected Deletions",
            buttons=["Cancel", "Open Full List", "Delete Selected"],
            default_button="Cancel",
            timeout=600,
        )
        if answer == "Open Full List":
            subprocess.run(["/usr/bin/open", str(review_file)], check=False)
            continue
        return answer == "Delete Selected"


def save_activated_exclusion_patterns(patterns: set[str]) -> None:
    config = require_config()
    allowed = {exclusion.pattern for exclusion in config.staged_exclusions}
    safe_patterns = sorted(pattern for pattern in patterns if pattern in allowed)
    atomic_write_json(
        config.exclusions_file,
        {
            "timestamp": datetime.now().isoformat(),
            "patterns": safe_patterns,
        },
    )
    config.exclude_path_patterns = tuple(
        dict.fromkeys((*config.exclude_path_patterns, *safe_patterns))
    )


def activate_ready_staged_exclusions(
    deleted: list[PendingDeletion],
    remaining: list[PendingDeletion],
) -> list[StagedExclusion]:
    config = require_config()
    staged_patterns = {exclusion.pattern for exclusion in config.staged_exclusions}
    active_patterns = {
        pattern for pattern in config.exclude_path_patterns if pattern in staged_patterns
    }
    activated: list[StagedExclusion] = []
    for exclusion in config.staged_exclusions:
        deleted_match = any(
            item.pair == exclusion.pair
            and rel_path_matches_pattern(item.file, exclusion.pattern)
            for item in deleted
        )
        remaining_match = any(
            item.pair == exclusion.pair
            and rel_path_matches_pattern(item.file, exclusion.pattern)
            for item in remaining
        )
        if deleted_match and not remaining_match and exclusion.pattern not in active_patterns:
            active_patterns.add(exclusion.pattern)
            activated.append(exclusion)
    if activated:
        save_activated_exclusion_patterns(active_patterns)
    return activated


def pending_deletion_bases(item: PendingDeletion) -> tuple[Path, Path]:
    """Return (base holding the remaining copy, base where it was deleted)."""

    pair = get_pair_by_name(item.pair)
    if item.action == "delete_onedrive":
        return pair.onedrive_dir, pair.local_dir
    if item.action == "delete_local":
        return pair.local_dir, pair.onedrive_dir
    raise RuntimeError(f"Unknown deletion action: {item.action}")


def pending_deletion_paths(item: PendingDeletion) -> tuple[Path, Path]:
    """Return (remaining copy, path where it was deleted) for a pending item."""

    remaining_base, deleted_base = pending_deletion_bases(item)
    return remaining_base / item.file, deleted_base / item.file


def update_state_for_paths(pair: SyncPair, pair_state: dict, paths: set[str]) -> None:
    for rel_path in paths:
        for side, base in (("local", pair.local_dir), ("onedrive", pair.onedrive_dir)):
            fields = (side, f"{side}_signatures", f"{side}_mtimes")
            path = base / rel_path
            try:
                stat = path.stat()
                checksum = md5_file(path)
            except FileNotFoundError:
                for field in fields:
                    pair_state.setdefault(field, {}).pop(rel_path, None)
                continue
            pair_state.setdefault(side, {})[rel_path] = checksum
            pair_state.setdefault(f"{side}_signatures", {})[rel_path] = stat_signature(stat)
            pair_state.setdefault(f"{side}_mtimes", {})[rel_path] = stat.st_mtime


def refresh_and_save_pairs(
    pair_states: dict[str, dict],
    affected_by_pair: dict[str, set[str]],
) -> None:
    """Update the saved baseline for paths changed outside a normal sync run.

    Only the affected paths are re-read, so applying a review never rescans
    the whole tree. Paths that still differ or are in conflict keep their
    previous baseline.
    """

    if not affected_by_pair:
        return
    conflicts = load_conflicts()
    for pair_name, paths in affected_by_pair.items():
        pair = get_pair_by_name(pair_name)
        previous = filter_excluded_state(pair_states.get(pair_name, {})) or {}
        refreshed = {**previous, **{field: dict(previous.get(field, {})) for field in STATE_PATH_FIELDS}}
        update_state_for_paths(pair, refreshed, paths)
        protected = mismatched_paths_in_state(refreshed) | paths_from_conflicts(
            [conflict for conflict in conflicts if conflict.get("pair") == pair_name]
        )
        preserve_state_for_paths(refreshed, previous, protected)
        pair_states[pair_name] = refreshed
    save_state(pair_states)


def load_selected_pending(selected: list[PendingDeletion]) -> tuple[
    list[PendingDeletion], list[PendingDeletion]
]:
    """Return (approved items from the saved plan, full saved plan)."""

    snapshot = load_pending_deletion_snapshot()
    if snapshot is None:
        raise RuntimeError("The pending deletion plan is missing. Review it again.")
    snapshot_by_id = {item.identity: item for item in snapshot}
    selected_ids = {item.identity for item in selected}
    if not selected_ids.issubset(snapshot_by_id):
        raise RuntimeError(
            "The pending deletion plan changed. Nothing was changed; review it again."
        )
    return [snapshot_by_id[identity] for identity in sorted(selected_ids)], snapshot


def restore_pending_deletions(
    selected: list[PendingDeletion],
    verbose: bool = False,
) -> tuple[list[PendingDeletion], list[PendingDeletion]]:
    """Undo a deletion by copying the remaining copy back to where it was deleted."""

    if not selected:
        return [], []
    approved, snapshot = load_selected_pending(selected)
    pair_states = (load_state() or {"pairs": {}}).get("pairs", {})
    restored: list[PendingDeletion] = []
    failed: list[PendingDeletion] = []
    affected_by_pair: dict[str, set[str]] = {}
    for item in approved:
        source_base, destination_base = pending_deletion_bases(item)
        if (destination_base / item.file).exists() or not (source_base / item.file).exists():
            log_msg(f"  Restore skipped, plan changed [{item.pair}] {item.file}", verbose)
            failed.append(item)
            continue
        if copy_file(source_base, destination_base, item.file, verbose):
            restored.append(item)
            affected_by_pair.setdefault(item.pair, set()).add(item.file)
        else:
            failed.append(item)

    refresh_and_save_pairs(pair_states, affected_by_pair)
    restored_ids = {item.identity for item in restored}
    save_pending_deletion_snapshot(
        [item for item in snapshot if item.identity not in restored_ids]
    )
    return restored, failed


def apply_approved_deletions(
    selected: list[PendingDeletion],
    *,
    confirmed: bool = False,
    verbose: bool = False,
) -> tuple[list[PendingDeletion], list[PendingDeletion], list[StagedExclusion]]:
    """Delete only an unchanged, freshly revalidated, explicitly approved set."""

    if not confirmed:
        raise PermissionError("Explicit confirmation is required before deletion.")
    if not selected:
        return [], [], []
    approved, snapshot = load_selected_pending(selected)

    pair_states = (load_state() or {"pairs": {}}).get("pairs", {})
    validated: list[tuple[PendingDeletion, Path]] = []
    for item in approved:
        target, counterpart = pending_deletion_paths(item)
        if counterpart.exists() or not target.exists():
            raise RuntimeError(
                f"The deletion plan changed for [{item.pair}] {item.file}. Nothing was deleted."
            )
        signature_matches = bool(
            item.expected_signature
            and file_signature(target) == item.expected_signature
        )
        if not signature_matches:
            if not item.expected_hash or md5_file(target) != item.expected_hash:
                raise RuntimeError(
                    f"The remaining copy changed for [{item.pair}] {item.file}. "
                    "Nothing was deleted."
                )
        validated.append((item, target))

    deleted: list[PendingDeletion] = []
    failed: list[PendingDeletion] = []
    affected_by_pair: dict[str, set[str]] = {}
    for item, _ in validated:
        if delete_from_side(get_pair_by_name(item.pair), item.action, item.file, verbose):
            deleted.append(item)
            affected_by_pair.setdefault(item.pair, set()).add(item.file)
        else:
            failed.append(item)

    refresh_and_save_pairs(pair_states, affected_by_pair)

    deleted_ids = {item.identity for item in deleted}
    remaining = [item for item in snapshot if item.identity not in deleted_ids]
    save_pending_deletion_snapshot(remaining)
    activated = activate_ready_staged_exclusions(deleted, remaining)
    return deleted, failed, activated


def choose_review_action(groups: list[DeletionGroup]) -> str | None:
    total = sum(len(group.items) for group in groups)
    answer = _osascript_with_tempfile(
        f"{len(groups)} folder group(s) with {total:,} file(s) selected.\n\n"
        "Restore: copy the remaining copies back to where they were deleted.\n"
        "Delete: remove the remaining copies (a second confirmation follows).",
        title="Review Pending Deletions",
        buttons=["Cancel", "Restore Selected", "Delete Selected"],
        default_button="Cancel",
        timeout=600,
    )
    return {"Restore Selected": "restore", "Delete Selected": "delete"}.get(answer)


def show_deletion_review_result(
    deleted: list[PendingDeletion],
    failed: list[PendingDeletion],
    activated: list[StagedExclusion],
    review_file: Path,
    verb: str = "Deleted",
) -> None:
    lines = [f"{verb} {len(deleted):,} explicitly selected file(s)."]
    if failed:
        lines.append(f"{len(failed):,} file(s) could not be processed and remain pending.")
    if activated:
        lines.append("")
        lines.append("Future temporary files are now excluded for:")
        lines.extend(f"• {exclusion.label}" for exclusion in activated)
    try:
        existing_report = review_file.read_text(encoding="utf-8")
        atomic_write_text(
            review_file,
            existing_report
            + "\n\nExecution Result\n"
            + "-" * 70
            + "\n"
            + "\n".join(lines)
            + "\n",
        )
    except OSError:
        pass
    answer = _osascript_with_tempfile(
        "\n".join(lines),
        title="Deletion Review Complete",
        buttons=["Open Report", "OK"],
        default_button="OK",
        timeout=300,
    )
    if answer == "Open Report":
        subprocess.run(["/usr/bin/open", str(review_file)], check=False)


def acquire_lock(wait_seconds: float = 0):
    """Return the held lock file, or None when another sync is still running
    after wait_seconds."""

    config = require_config()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = open(config.lock_file, "a+", encoding="utf-8")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                lock_fd.close()
                return None
            time.sleep(0.2)
    lock_fd.seek(0)
    lock_fd.truncate()
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd


def release_lock(lock_fd) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
    except OSError:
        pass


def sync_running() -> bool:
    lock_fd = acquire_lock()
    if lock_fd is None:
        return True
    release_lock(lock_fd)
    return False


def review_pending_deletions_interactive(verbose: bool = False) -> int:
    lock_fd = acquire_lock()
    if lock_fd is None:
        _osascript_with_tempfile(
            "Another sync is already running. Try the deletion review again afterwards.",
            title="Deletion Review Unavailable",
            buttons=["OK"],
            default_button="OK",
            timeout=120,
        )
        return 2

    try:
        pending = load_pending_deletion_snapshot()
        issues: list[str] = []
        if pending is None:
            pending, issues = plan_pending_deletions(verbose=verbose)
            if not issues:
                save_pending_deletion_snapshot(pending)
        if issues:
            _osascript_with_tempfile(
                "The deletion plan could not be verified, so nothing can be deleted.\n\n"
                + "\n".join(issues[:10]),
                title="Deletion Review Stopped",
                buttons=["OK"],
                default_button="OK",
                timeout=300,
            )
            return 2
        if not pending:
            _osascript_with_tempfile(
                "There are no pending deletions.",
                title="Deletion Review",
                buttons=["OK"],
                default_button="OK",
                timeout=120,
            )
            return 0

        groups = group_pending_deletions(pending)
        selected_groups = choose_deletion_groups_dialog(groups)
        if not selected_groups:
            print("Deletion review cancelled. Nothing was deleted.")
            return 0
        review_file = write_deletion_review_file(selected_groups)
        selected_items = [
            item for group in selected_groups for item in group.items
        ]
        action = choose_review_action(selected_groups)
        if action == "restore":
            restored, failed = restore_pending_deletions(selected_items, verbose=verbose)
            show_deletion_review_result(restored, failed, [], review_file, verb="Restored")
            print(f"Restore complete: {len(restored)} restored, {len(failed)} failed.")
            return 0 if not failed else 2
        if action != "delete" or not confirm_selected_deletions(
            selected_groups, review_file
        ):
            print("Deletion review cancelled. Nothing was changed.")
            return 0

        deleted, failed, activated = apply_approved_deletions(
            selected_items,
            confirmed=True,
            verbose=verbose,
        )
        show_deletion_review_result(deleted, failed, activated, review_file)
        print(
            f"Deletion review complete: {len(deleted)} deleted, "
            f"{len(failed)} failed, {len(activated)} exclusion(s) activated."
        )
        return 0 if not failed else 2
    except Exception as exc:
        log_msg(f"Deletion review error: {exc}", verbose=True)
        _osascript_with_tempfile(
            "The review stopped safely. Previously confirmed deletions may already "
            f"have completed; the next sync will reconcile the state.\n\n{exc}",
            title="Deletion Review Stopped",
            buttons=["OK"],
            default_button="OK",
            timeout=300,
        )
        return 2
    finally:
        release_lock(lock_fd)


def show_status() -> bool:
    config = require_config()
    state = load_state()
    if state is None:
        print("No previous sync state found. Run sync first.")
        return False

    print(f"Config: {config.config_path}")
    print(f"Last sync: {state.get('timestamp', 'unknown')}")
    print("")

    pair_states = state.get("pairs", {})
    recorded_differences = 0
    for pair in config.pairs:
        pair_state = filter_excluded_state(pair_states.get(pair.key, {})) or {}
        local = pair_state.get("local", {})
        onedrive = pair_state.get("onedrive", {})
        differences = sum(
            local.get(path) != onedrive.get(path)
            for path in set(local) | set(onedrive)
        )
        recorded_differences += differences
        print(f"[{pair.name}]")
        print(f"  Local:    {pair.local_dir}")
        print(f"  OneDrive: {pair.onedrive_dir}")
        print(f"  Local files tracked: {len(local)}")
        print(f"  OneDrive files tracked: {len(onedrive)}")
        print(f"  Recorded differences: {differences}")
        print("")

    conflicts = load_conflicts()
    if conflicts:
        print(f"Unresolved conflicts ({len(conflicts)}):")
        for conflict in conflicts:
            print(f"  - [{conflict.get('pair', '?')}] {conflict['file']}: {conflict['reason']}")
    else:
        print("No conflicts.")

    healthy = not conflicts and recorded_differences == 0
    if not healthy:
        print("\nStatus: NOT fully synchronized or not yet reconciled.")
    else:
        print("\nStatus: Last recorded state is synchronized.")

    if config.summary_file.exists():
        print(f"\nLast sync summary ({config.summary_file}):")
        print("-" * 40)
        with open(config.summary_file, encoding="utf-8") as handle:
            print(handle.read())
    return healthy


def list_pairs() -> None:
    config = require_config()
    print(f"Config: {config.config_path}")
    for pair in config.pairs:
        print(f"- {pair.label}")


def parse_conflict_target(target: str, conflicts: list[dict]) -> tuple[str, str]:
    if ":" in target:
        pair_name, rel_path = target.split(":", 1)
        return pair_name, rel_path

    matches = [conflict for conflict in conflicts if conflict["file"] == target]
    if len(matches) == 1:
        return matches[0]["pair"], target
    if len(matches) > 1:
        raise ValueError(
            "Conflict file exists in multiple pairs. Use PAIR:relative/path syntax."
        )
    raise ValueError("Conflict not found.")


def get_pair_by_name(name: str) -> SyncPair:
    config = require_config()
    for pair in config.pairs:
        if pair.key == name:
            return pair
    raise ValueError(f"Unknown pair: {name}")


def apply_resolution(pair: SyncPair, rel_path: str, keep: str) -> None:
    """Make both sides equal to the kept version and verify the result."""

    if keep not in {"local", "onedrive"}:
        raise ValueError(f"Invalid direction: {keep}. Use 'local' or 'onedrive'.")
    if (side_dir(pair, keep) / rel_path).exists():
        success = copy_for_action(pair, copy_action_for(keep), rel_path, verbose=True)
    else:
        delete_action = "delete_onedrive" if keep == "local" else "delete_local"
        success = delete_from_side(pair, delete_action, rel_path, verbose=True)
    if not success:
        raise RuntimeError(f"Resolution failed for [{pair.name}] {rel_path}")

    local_path = pair.local_dir / rel_path
    onedrive_path = pair.onedrive_dir / rel_path
    if local_path.exists() != onedrive_path.exists():
        raise RuntimeError(f"Resolution verification failed for [{pair.name}] {rel_path}")
    if local_path.exists() and md5_file(local_path) != md5_file(onedrive_path):
        raise RuntimeError(f"Resolved files still differ for [{pair.name}] {rel_path}")


def apply_name_resolution(pair: SyncPair, conflict: dict, keep: str) -> list[str]:
    """Settle a name conflict: the kept side's content and spelling go to both sides.

    Returns the paths whose baseline must be refreshed.
    """

    if keep not in {"local", "onedrive"}:
        raise ValueError(f"Invalid direction: {keep}. Use 'local' or 'onedrive'.")
    names = {"local": conflict["file"], "onedrive": conflict["related_file"]}
    other = other_side(keep)
    kept_rel, other_rel = names[keep], names[other]
    source = side_dir(pair, keep) / kept_rel
    target = side_dir(pair, other) / other_rel
    if not source.is_file() or not target.is_file():
        raise RuntimeError(f"File missing for [{pair.name}] {kept_rel}")
    keep_version(pair, other, other_rel, move=False)
    shutil.copy2(source, target)
    match_name_case(side_dir(pair, other), kept_rel, exact=True)
    final = side_dir(pair, other) / kept_rel
    if kept_rel not in os.listdir(final.parent) or md5_file(final) != md5_file(source):
        raise RuntimeError(f"Resolution verification failed for [{pair.name}] {kept_rel}")
    return [kept_rel, other_rel]


def resolve_conflicts_batch(decisions: list[dict]) -> dict:
    """Apply conflict decisions ({pair, file, keep}) and update only those paths."""

    conflicts = load_conflicts()
    open_ids = {(conflict.get("pair"), conflict["file"]): conflict for conflict in conflicts}
    done: list[str] = []
    failed: list[dict] = []
    resolved_ids: set[tuple[str, str]] = set()
    affected_by_pair: dict[str, set[str]] = {}
    for decision in decisions:
        identity = (decision["pair"], decision["file"])
        conflict = open_ids.get(identity)
        try:
            if conflict is None:
                raise ValueError("Conflict is no longer open")
            pair = get_pair_by_name(decision["pair"])
            if decision["keep"] == "merged":
                if conflict.get("related_file"):
                    raise ValueError("Name conflicts can't be merged; keep one version")
                apply_merge(pair, decision["file"], decision.get("merged_text") or "")
                affected = [decision["file"]]
            elif conflict.get("related_file"):
                affected = apply_name_resolution(pair, conflict, decision["keep"])
            else:
                apply_resolution(pair, decision["file"], decision["keep"])
                affected = [decision["file"]]
        except Exception as exc:
            failed.append({"file": decision["file"], "error": str(exc)})
            log_msg(f"  ERROR resolving [{decision['pair']}] {decision['file']}: {exc}")
            continue
        done.append(decision["file"])
        resolved_ids.add(identity)
        affected_by_pair.setdefault(decision["pair"], set()).update(affected)
        log_msg(f"  RESOLVED [{decision['pair']}] kept {decision['keep']}: {decision['file']}")

    save_conflicts(
        [c for c in conflicts if (c.get("pair"), c["file"]) not in resolved_ids]
    )
    pair_states = (load_state() or {"pairs": {}}).get("pairs", {})
    refresh_and_save_pairs(pair_states, affected_by_pair)
    return {"done": done, "failed": failed}


def resolve_conflict(target: str, direction: str) -> None:
    pair_name, rel_path = parse_conflict_target(target, load_conflicts())
    result = resolve_conflicts_batch([{"pair": pair_name, "file": rel_path, "keep": direction}])
    if result["failed"]:
        raise RuntimeError(result["failed"][0]["error"])
    print(f"Resolved: [{pair_name}] {rel_path} (kept {direction} version)")


def resolve_all(direction: str) -> None:
    conflicts = load_conflicts()
    if not conflicts:
        print("No conflicts to resolve.")
        return
    result = resolve_conflicts_batch(
        [
            {"pair": conflict["pair"], "file": conflict["file"], "keep": direction}
            for conflict in conflicts
            if not conflict.get("related_file")
        ]
    )
    print(
        f"Resolved {len(result['done'])} conflict(s) using {direction} version; "
        f"{len(result['failed'])} failed."
    )


def reconcile_missing_conflicts() -> tuple[list[dict], list[dict]]:
    """Copy one-sided conflict files without overwriting or deleting anything."""

    conflicts = load_conflicts()
    if not conflicts:
        print("No conflicts to reconcile.")
        return [], []

    state = load_state() or {"pairs": {}}
    pair_states = state.get("pairs", {})
    remaining: list[dict] = []
    resolved: list[dict] = []
    affected_by_pair: dict[str, set[str]] = {}

    for conflict in conflicts:
        if conflict.get("related_file"):
            remaining.append(conflict)
            continue
        local_hash = conflict.get("local_hash")
        onedrive_hash = conflict.get("onedrive_hash")
        if (local_hash is None) == (onedrive_hash is None):
            remaining.append(conflict)
            continue

        pair = get_pair_by_name(conflict["pair"])
        rel_path = conflict["file"]
        if local_hash is not None:
            source_base, destination_base = pair.local_dir, pair.onedrive_dir
            direction = "local to OneDrive"
        else:
            source_base, destination_base = pair.onedrive_dir, pair.local_dir
            direction = "OneDrive to local"

        source = source_base / rel_path
        destination = destination_base / rel_path
        if not source.exists() or destination.exists():
            failed = dict(conflict)
            failed["reason"] = (
                "Non-destructive reconciliation stopped because source/destination changed"
            )
            remaining.append(failed)
            continue
        if not copy_file(source_base, destination_base, rel_path, verbose=True):
            failed = dict(conflict)
            failed["reason"] = "Non-destructive reconciliation copy failed"
            remaining.append(failed)
            continue
        try:
            verified = md5_file(source) == md5_file(destination)
        except OSError:
            verified = False
        if not verified:
            failed = dict(conflict)
            failed["reason"] = "Non-destructive reconciliation verification failed"
            remaining.append(failed)
            continue

        resolved.append(conflict)
        affected_by_pair.setdefault(pair.key, set()).add(rel_path)
        log_msg(f"  RECONCILED [{pair.name}] {direction}: {rel_path}", verbose=True)

    for pair_name, paths in affected_by_pair.items():
        pair = get_pair_by_name(pair_name)
        previous_pair_state = pair_states.get(pair_name, {})
        refreshed = refresh_pair_state(
            pair, previous_pair_state, force_paths=paths
        )
        preserve_state_for_paths(
            refreshed,
            previous_pair_state,
            mismatched_paths_in_state(refreshed),
        )
        pair_states[pair_name] = refreshed

    save_conflicts(remaining)
    save_state(pair_states)
    print(
        f"Reconciled {len(resolved)} one-sided file(s); "
        f"{len(remaining)} conflict(s) remain."
    )
    return resolved, remaining


# ---------------------------------------------------------------------------
# JSON interface for the SyncBridge menu bar app
# ---------------------------------------------------------------------------

CONFIG_INT_FIELDS = (
    "auto_delete_limit",
    "trash_retention_days",
    "sync_interval_minutes",
    "versioning_max_count",
    "versioning_min_count",
)
CONFIG_LIST_FIELDS = (
    "exclude_names",
    "exclude_prefixes",
    "exclude_suffixes",
    "exclude_dirs",
    "exclude_path_patterns",
)


def status_file() -> Path:
    return require_config().state_dir / "status.json"


def status_payload() -> dict:
    config = require_config()
    try:
        last = json.loads(status_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    conflicts = load_conflicts()
    pending = load_pending_deletion_snapshot() or []
    return {
        "running": sync_running(),
        "last_sync": last.get("timestamp"),
        "last_result": last.get("summary", ""),
        "conflicts": len(conflicts),
        "pending_deletions": len(pending),
        "pairs": [
            {
                "name": pair.name,
                "local": str(pair.local_dir),
                "onedrive": str(pair.onedrive_dir),
                "variant": pair.variant,
                "direction": pair.direction,
                "conflicts": sum(1 for item in conflicts if item.get("pair") == pair.key),
                "pending_deletions": sum(1 for item in pending if item.pair == pair.key),
            }
            for pair in config.pairs
        ],
        "config_path": str(config.config_path),
        "log_file": str(config.log_file),
        "summary_file": str(config.summary_file),
        "sync_interval_minutes": config.sync_interval_minutes,
    }


def conflicts_payload() -> list[dict]:
    pairs = {pair.key: pair for pair in require_config().pairs}
    items = []
    for conflict in load_conflicts():
        pair = pairs.get(conflict.get("pair"))
        if pair is None:
            continue
        rel_path = conflict["file"]
        local, onedrive, newer = conflict_versions(pair, rel_path)
        is_text = is_text_file(rel_path) and local is not None and onedrive is not None
        items.append(
            {
                **conflict,
                "local": local,
                "onedrive": onedrive,
                "newer": newer,
                "is_text": is_text,
                "diff_preview": conflict_diff_preview(pair, rel_path) if is_text else "",
                "analysis": analyze_conflict(pair, rel_path),
            }
        )
    return items


def deletions_payload() -> list[dict]:
    return [
        {
            "id": f"{group.pair}|{group.action}|{group.path}",
            "pair": group.pair,
            "action": group.action,
            "remaining_on": "OneDrive" if group.action == "delete_onedrive" else "Local",
            "path": group.path,
            "label": group.label,
            "recommended": group.recommended,
            "count": len(group.items),
            "items": [
                {"pair": item.pair, "action": item.action, "file": item.file}
                for item in group.items
            ],
        }
        for group in group_pending_deletions(load_pending_deletion_snapshot() or [])
    ]


def apply_deletion_selection(items: list[dict], action: str, *, confirmed: bool) -> dict:
    """Delete or restore pending deletions selected in the app."""

    if not confirmed:
        raise PermissionError("Explicit confirmation is required.")
    selected = [
        PendingDeletion(pair=item["pair"], action=item["action"], file=item["file"])
        for item in items
    ]
    activated: list[StagedExclusion] = []
    if action == "delete":
        done, failed, activated = apply_approved_deletions(selected, confirmed=True)
    elif action == "restore":
        done, failed = restore_pending_deletions(selected)
    else:
        raise ValueError(f"Unknown action: {action}")
    return {
        "done": [item.file for item in done],
        "failed": [item.file for item in failed],
        "activated": [exclusion.label for exclusion in activated],
    }


def read_config_payload(path: Path | None = None) -> dict:
    path = path or default_config_path()
    if not path.exists():
        return {"pairs": []}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def validate_config_payload(payload: dict) -> list[str]:
    errors: list[str] = []
    pairs = payload.get("pairs")
    if not isinstance(pairs, list):
        return ["pairs must be a list"]
    names: set[str] = set()
    folders: list[tuple[str, Path]] = []
    for index, pair in enumerate(pairs, start=1):
        name = str(pair.get("name", "")).strip()
        local = str(pair.get("local", "")).strip()
        onedrive = str(pair.get("onedrive", "")).strip()
        if not (name and local and onedrive):
            errors.append(f"Pair {index}: name, local and onedrive folder are required")
            continue
        if name in names:
            errors.append(f"Duplicate pair name: {name}")
        try:
            pair_options_from_entry(pair)
        except ValueError as exc:
            errors.append(f"Pair {name}: {exc}")
        names.add(name)
        for side, value in (("local", local), ("onedrive", onedrive)):
            folders.append((f"{name} ({side})", expand_path(value).resolve()))
    for index, (label, folder) in enumerate(folders):
        for other_label, other in folders[index + 1:]:
            if paths_overlap(folder, other):
                errors.append(f"Folders overlap: {label} and {other_label}")
    for field in CONFIG_INT_FIELDS:
        value = payload.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            errors.append(f"{field} must be a whole number of at least 0")
    errors.extend(config_choice_errors(payload))
    errors.extend(rule_config_errors(payload))
    for field in CONFIG_LIST_FIELDS:
        value = payload.get(field)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(item, str) for item in value)
        ):
            errors.append(f"{field} must be a list of text entries")
    return errors


def save_config_payload(payload: dict, path: Path | None = None) -> list[str]:
    """Validate and save settings from the app, keeping fields it does not know."""

    errors = validate_config_payload(payload)
    if errors:
        return errors
    path = path or default_config_path()
    merged = {**read_config_payload(path), **payload}
    atomic_write_json(path, merged)
    return []


def print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


APP_LOCK_WAIT_SECONDS = 15 * 60  # longer than a full sync of large folders


def run_locked(action, wait_seconds: float = APP_LOCK_WAIT_SECONDS) -> int:
    """Run an app command under the sync lock and print its JSON result.

    A running sync is waited for, so decisions made in the app are not lost.
    """

    lock_fd = acquire_lock(wait_seconds)
    if lock_fd is None:
        print_json({"error": "Another sync is still running. Your choices are kept; try again when it has finished."})
        return 1
    try:
        result = action()
    except Exception as exc:
        print_json({"error": str(exc)})
        return 2
    finally:
        release_lock(lock_fd)
    print_json(result)
    return 2 if result.get("failed") else 0


def run_app_command(args: argparse.Namespace) -> int | None:
    """Handle the JSON commands used by the app; None when none was requested."""

    if args.get_config:
        print_json(read_config_payload(Path(args.config) if args.config else None))
        return 0
    if args.set_config:
        payload = json.loads(Path(args.set_config).read_text(encoding="utf-8"))
        errors = save_config_payload(payload, Path(args.config) if args.config else None)
        print_json({"errors": errors})
        return 0 if not errors else 2

    global CONFIG
    CONFIG = load_config(args.config)
    if args.status and args.json:
        print_json(status_payload())
        return 0
    if args.list_conflicts:
        print_json(conflicts_payload())
        return 0
    if args.conflict_texts:
        print_json(conflict_texts_payload(*args.conflict_texts, max_chars=args.max_chars))
        return 0
    if args.list_deletions:
        print_json(deletions_payload())
        return 0
    if args.preview:
        return run_locked(lambda: preview_payload(args.pairs))
    if args.resolve_batch:
        decisions = json.loads(Path(args.resolve_batch).read_text(encoding="utf-8"))
        return run_locked(lambda: resolve_conflicts_batch(decisions))
    selection_file = args.apply_deletions or args.restore_deletions
    if selection_file:
        items = json.loads(Path(selection_file).read_text(encoding="utf-8"))
        action = "delete" if args.apply_deletions else "restore"
        return run_locked(
            lambda: apply_deletion_selection(items, action, confirmed=args.confirmed)
        )
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SyncBridge: safe two-way sync between local folders and OneDrive"
    )
    parser.add_argument("--config", help="Path to config JSON file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("--status", action="store_true", help="Show sync status")
    parser.add_argument("--list-pairs", action="store_true", help="List configured folder pairs")
    parser.add_argument(
        "--no-delete",
        action="store_true",
        help="Skip deletions (default for non-interactive runs)",
    )
    parser.add_argument(
        "--allow-delete",
        action="store_true",
        help="Allow deletions without a dialog; must be explicitly requested",
    )
    parser.add_argument(
        "--auto-delete",
        action="store_true",
        help=(
            "Propagate deletions without a dialog while a run has at most "
            "auto_delete_limit new ones; larger batches are held for review"
        ),
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Hash every included file instead of using the metadata cache",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Show macOS dialogs for conflicts and deletions",
    )
    parser.add_argument(
        "--resolve",
        nargs=2,
        metavar=("FILE", "DIRECTION"),
        help="Resolve a conflict (use PAIR:FILE when ambiguous; direction: local or onedrive)",
    )
    parser.add_argument(
        "--resolve-all",
        choices=["local", "onedrive"],
        help="Resolve all conflicts using the specified source",
    )
    parser.add_argument(
        "--reconcile-missing",
        action="store_true",
        help="Copy one-sided conflicts without overwriting or deleting files",
    )
    parser.add_argument(
        "--review-deletions",
        action="store_true",
        help="Review protected deletions by folder and confirm selected groups",
    )
    parser.add_argument("--json", action="store_true", help="Output conflicts as JSON")
    app = parser.add_argument_group("app interface (JSON output)")
    app.add_argument("--list-conflicts", action="store_true", help="List conflicts with versions")
    app.add_argument("--list-deletions", action="store_true", help="List pending deletion groups")
    app.add_argument("--apply-deletions", metavar="FILE", help="Delete the selection in FILE")
    app.add_argument("--restore-deletions", metavar="FILE", help="Restore the selection in FILE")
    app.add_argument("--confirmed", action="store_true", help="Confirm an apply/restore")
    app.add_argument(
        "--conflict-texts", nargs=2, metavar=("PAIR", "FILE"), help="Both versions' text for AI help"
    )
    app.add_argument("--max-chars", type=int, default=20000, help="Text limit for --conflict-texts")
    app.add_argument("--resolve-batch", metavar="FILE", help="Apply conflict decisions in FILE")
    app.add_argument("--get-config", action="store_true", help="Print the config file")
    app.add_argument("--set-config", metavar="FILE", help="Validate and save config from FILE")
    app.add_argument("--preview", action="store_true", help="Plan a sync without changes")
    app.add_argument("--pairs", nargs="+", metavar="NAME", help="Only sync these pairs")
    app.add_argument("--overrides", metavar="FILE", help="Per-file actions chosen in the preview")
    args = parser.parse_args()

    exit_code = run_app_command(args)
    if exit_code is not None:
        sys.exit(exit_code)

    global CONFIG
    CONFIG = load_config(args.config)

    if args.list_pairs:
        list_pairs()
        return

    if args.status:
        if not show_status():
            sys.exit(2)
        return

    if args.resolve:
        resolve_conflict(args.resolve[0], args.resolve[1])
        return

    if args.resolve_all:
        resolve_all(args.resolve_all)
        return

    if args.reconcile_missing:
        _, remaining = reconcile_missing_conflicts()
        if remaining:
            sys.exit(2)
        return
    if args.review_deletions:
        sys.exit(review_pending_deletions_interactive(verbose=True))

    config = require_config()
    lock_fd = acquire_lock()
    if lock_fd is None:
        print("Another sync is already running.")
        sys.exit(1)

    try:
        no_delete = args.no_delete or not (
            args.allow_delete or args.interactive or args.auto_delete
        )
        delete_limit = (
            config.auto_delete_limit if args.auto_delete and not no_delete else None
        )
        results, conflicts = sync_all(
            verbose=args.verbose or args.dry_run,
            dry_run=args.dry_run,
            no_delete=no_delete,
            interactive=args.interactive,
            force_full=args.full_scan,
            delete_limit=delete_limit,
            pair_names=args.pairs,
            overrides=(
                json.loads(Path(args.overrides).read_text(encoding="utf-8"))
                if args.overrides
                else None
            ),
        )
        if args.json and conflicts:
            print(json.dumps(conflicts, indent=2))
        has_errors = any(result["skipped"] or result["errors"] for result in results)
        if conflicts or has_errors:
            sys.exit(2)
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
