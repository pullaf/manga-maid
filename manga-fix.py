#!/usr/bin/env python3
"""manga-fix — find and fix Kavita-incompatible manga filenames."""

import argparse
import json
import os
import re
import sys
from datetime import datetime

MANGA_EXTENSIONS = {".cbz", ".cbr", ".zip", ".rar"}
LOG_FILENAME = ".manga-fix-log.json"

# Matches filenames with a trailing download-manager version suffix: name (1).cbz
DUP_VERSION_RE = re.compile(r"^(.+?)\s*\((\d+)\)(\.[^.]+)$", re.IGNORECASE)

# Matches the front-loaded [en GroupName] or [en] prefix
MOVE_GROUP_RE = re.compile(r"^\[([a-z]{2})(?:\s+(.+?))?\]\s+(.+)$")

CH_RE = re.compile(r"ch\.\s*(\d+(?:\.\d+)?)")

# --- Detection patterns and fix functions ---

ISSUES = [
    {
        "name": "empty_group",
        "description": "bracket with only whitespace after lang code: [en ] → [en]",
        "pattern": re.compile(r"\[([a-z]{2})\s+\]"),
        "fix": lambda m: f"[{m.group(1)}]",
    },
    {
        "name": "empty_volume",
        "description": "vol. with no number before ch.: vol.  ch. N → ch. N",
        "pattern": re.compile(r"vol\.\s{2,}(?=ch\.)"),
        "fix": lambda m: "",
    },
    {
        "name": "malformed_bracket",
        "description": "extra spaces or extra ] in release group: [en  alt.ver.]] → [en alt.ver.]",
        "pattern": re.compile(r"\[\s*([a-z]{2})\s{2,}(.+?)\]+"),
        "fix": lambda m: f"[{m.group(1)} {m.group(2).strip()}]",
    },
]


def human_size(n):
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.1f} MB"


def fix_name(name, issue):
    pattern = issue["pattern"]
    match = pattern.search(name)
    if not match:
        return None
    fixed = pattern.sub(issue["fix"](match), name, count=1)
    return fixed if fixed != name else None


def scan(directory):
    """Return list of (filepath, issue_name, new_name) for rename-type issues."""
    findings = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in MANGA_EXTENSIONS:
                continue
            for issue in ISSUES:
                fixed = fix_name(fname, issue)
                if fixed is not None:
                    findings.append((os.path.join(root, fname), issue["name"], fixed))
                    break
    return findings


def scan_duplicates(directory):
    """Return list of dup groups.

    Each group dict:
      keep_path    — absolute path of the file to keep
      keep_name    — target basename (version suffix stripped)
      needs_rename — True when keep_path already carries a (N) suffix
      delete_paths — list of paths to remove
      sizes        — {path: int} for all files in the group
    """
    groups = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        base_files = {}   # (stem_lower, ext_lower) -> path
        ver_files  = {}   # (stem_lower, ext_lower) -> [paths]

        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in MANGA_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            m = DUP_VERSION_RE.match(fname)
            if m:
                key = (m.group(1).lower(), m.group(3).lower())
                ver_files.setdefault(key, []).append(fpath)
            else:
                stem = os.path.splitext(fname)[0]
                key = (stem.lower(), ext)
                base_files[key] = fpath

        for key, versioned in ver_files.items():
            base = base_files.get(key)
            if not base and len(versioned) < 2:
                continue  # lone (1) file with no base — skip

            all_files = ([base] if base else []) + versioned
            sizes = {p: os.path.getsize(p) for p in all_files}
            max_size = max(sizes.values())

            # Prefer keeping the non-versioned file on a tie
            if base and sizes[base] >= max_size:
                winner = base
            else:
                winner = max(versioned, key=lambda p: (sizes[p], p))

            if base:
                keep_name = os.path.basename(base)
            else:
                wm = DUP_VERSION_RE.match(os.path.basename(winner))
                keep_name = wm.group(1) + wm.group(3)

            target_path = os.path.join(root, keep_name)
            needs_rename = winner != target_path

            groups.append({
                "keep_path":    winner,
                "keep_name":    keep_name,
                "needs_rename": needs_rename,
                "delete_paths": [p for p in all_files if p != winner],
                "sizes":        sizes,
            })

    return groups


def print_summary(findings, dup_groups):
    counts = {}
    dirs = {}
    for fpath, issue_name, _ in findings:
        counts[issue_name] = counts.get(issue_name, 0) + 1
        d = os.path.basename(os.path.dirname(fpath))
        dirs.setdefault(issue_name, set()).add(d)

    if dup_groups:
        counts["duplicate_version"] = len(dup_groups)
        for g in dup_groups:
            d = os.path.basename(os.path.dirname(g["keep_path"]))
            dirs.setdefault("duplicate_version", set()).add(d)

    total = len(findings) + len(dup_groups)
    print(f"\nFound {total} issue{'s' if total != 1 else ''}:")

    all_names = [i["name"] for i in ISSUES] + ["duplicate_version"]
    for name in all_names:
        if name in counts:
            dir_list = ", ".join(sorted(dirs[name]))
            print(f"  {counts[name]:3d} x {name:<22s}  {dir_list}")
    print()


def load_log(log_path):
    if os.path.exists(log_path):
        with open(log_path) as f:
            data = json.load(f)
        data.setdefault("deletes", [])
        return data
    return {"renames": [], "deletes": []}


def save_log(log_path, log_data):
    with open(log_path, "w") as f:
        json.dump(log_data, f, indent=2)


def do_rename(old_path, new_name, reason, log_data, log_path):
    new_path = os.path.join(os.path.dirname(old_path), new_name)
    os.rename(old_path, new_path)
    log_data["renames"].append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "reason":    reason,
        "old":       old_path,
        "new":       new_path,
    })
    save_log(log_path, log_data)
    return new_path


def do_delete(path, reason, log_data, log_path):
    size = os.path.getsize(path)
    os.remove(path)
    log_data["deletes"].append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "reason":    reason,
        "path":      path,
        "size":      size,
    })
    save_log(log_path, log_data)


def apply_dup_group(group, log_data, log_path):
    for path in group["delete_paths"]:
        do_delete(path, "duplicate_version", log_data, log_path)
    if group["needs_rename"]:
        do_rename(group["keep_path"], group["keep_name"], "duplicate_version", log_data, log_path)


def interactive_fix(findings, log_data, log_path):
    total = len(findings)
    apply_all = False

    for i, (fpath, issue_name, new_name) in enumerate(findings, 1):
        old_name = os.path.basename(fpath)
        print(f"[{i}/{total}] {issue_name}")
        print(f"  Before: {old_name}")
        print(f"  After:  {new_name}")

        if apply_all:
            do_rename(fpath, new_name, issue_name, log_data, log_path)
            print("  → renamed (auto)\n")
            continue

        while True:
            choice = input("  [y]es / [n]o / [A]ll / [q]uit: ").strip()
            if choice == "y":
                do_rename(fpath, new_name, issue_name, log_data, log_path)
                print("  → renamed\n")
                break
            elif choice == "n":
                print("  → skipped\n")
                break
            elif choice == "A":
                do_rename(fpath, new_name, issue_name, log_data, log_path)
                print("  → renamed (auto-all from here)\n")
                apply_all = True
                break
            elif choice == "q":
                print("Quit.")
                return
            else:
                print("  Invalid input. Use y / n / A / q.")


def interactive_fix_dups(dup_groups, log_data, log_path):
    total = len(dup_groups)
    apply_all = False

    print("  NOTE: deletions are permanent and cannot be undone.\n")

    for i, group in enumerate(dup_groups, 1):
        sizes  = group["sizes"]
        keep   = group["keep_path"]
        kname  = group["keep_name"]

        print(f"[{i}/{total}] duplicate_version")

        if group["needs_rename"]:
            print(f"  Keep → rename: {kname}  ({human_size(sizes[keep])})")
            print(f"    from: {os.path.basename(keep)}")
        else:
            print(f"  Keep:   {os.path.basename(keep)}  ({human_size(sizes[keep])})")

        for path in group["delete_paths"]:
            print(f"  Delete: {os.path.basename(path)}  ({human_size(sizes[path])})")

        if apply_all:
            apply_dup_group(group, log_data, log_path)
            print("  → done (auto)\n")
            continue

        while True:
            choice = input("  [y]es / [n]o / [A]ll / [q]uit: ").strip()
            if choice == "y":
                apply_dup_group(group, log_data, log_path)
                print("  → done\n")
                break
            elif choice == "n":
                print("  → skipped\n")
                break
            elif choice == "A":
                apply_dup_group(group, log_data, log_path)
                print("  → done (auto-all from here)\n")
                apply_all = True
                break
            elif choice == "q":
                print("Quit.")
                return
            else:
                print("  Invalid input. Use y / n / A / q.")


def move_group_name(fname):
    """Return new basename with group moved to end, language code dropped.

    [en Facepalm Scans] Series vol. 5 ch. 55.cbz → Series vol. 5 ch. 55 [Facepalm Scans].cbz
    [en] Series sp01.cbz                          → Series sp01.cbz
    """
    m = MOVE_GROUP_RE.match(fname)
    if not m:
        return None
    group_name = m.group(2)  # None when bracket had no group (just lang code)
    rest = m.group(3)        # everything after the bracket+space
    stem, ext = os.path.splitext(rest)
    if group_name:
        return f"{stem} [{group_name}]{ext}"
    return rest


def scan_move_group(directory):
    """Return (filepath, 'move_group', new_name) for every file with a front-loaded group tag."""
    findings = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for fname in sorted(files):
            if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
                continue
            new_name = move_group_name(fname)
            if new_name is not None:
                findings.append((os.path.join(root, fname), "move_group", new_name))
    return findings


def move_group_mode(manga_dir, log_data, log_path, auto):
    findings = scan_move_group(manga_dir)
    if not findings:
        print("No files with front-loaded group tags found.")
        return

    n_moved   = sum(1 for _, _, n in findings if "[" in os.path.splitext(n)[0])
    n_dropped = len(findings) - n_moved

    print(f"\nFiles to migrate: {len(findings)}")
    if n_moved:
        print(f"  {n_moved:4d}  will have group tag moved to end")
    if n_dropped:
        print(f"  {n_dropped:4d}  have no group name — brackets dropped entirely")

    # Show one example of each transformation type
    print("\nExamples:")
    seen_kinds = set()
    shown = 0
    for fpath, _, new_name in findings:
        kind = "moved" if "[" in os.path.splitext(new_name)[0] else "dropped"
        if kind not in seen_kinds:
            print(f"  Before: {os.path.basename(fpath)}")
            print(f"  After:  {new_name}\n")
            seen_kinds.add(kind)
            shown += 1
        if len(seen_kinds) == 2:
            break

    remaining = len(findings) - shown
    if remaining > 0:
        print(f"  ... and {remaining} more\n")

    if auto:
        for fpath, issue_name, new_name in findings:
            do_rename(fpath, new_name, issue_name, log_data, log_path)
        print(f"Done. {len(findings)} file(s) renamed.")
        return

    try:
        confirm = input("Apply all? [y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return

    if confirm == "y":
        for fpath, issue_name, new_name in findings:
            do_rename(fpath, new_name, issue_name, log_data, log_path)
        print(f"Done. {len(findings)} file(s) renamed.")
    else:
        print("Cancelled.")


def find_current_path(log_new, manga_dir):
    """Locate a file that has moved since it was logged.

    Matches by series directory name and chapter number, so it survives
    mount-point changes, language-subdir reorganisation, and move_group renames.
    """
    series = os.path.basename(os.path.dirname(log_new))
    m = CH_RE.search(os.path.basename(log_new))
    if not m:
        return None
    ch_num = m.group(1)
    for root, dirs, files in os.walk(manga_dir):
        if os.path.basename(root) != series:
            continue
        for fname in sorted(files):
            if os.path.splitext(fname)[1].lower() not in MANGA_EXTENSIONS:
                continue
            fm = CH_RE.search(fname)
            if fm and fm.group(1) == ch_num:
                return os.path.join(root, fname)
    return None


def add_volume_mode(log_data, log_path):
    stripped = [r for r in log_data["renames"] if r["reason"] == "empty_volume"]
    if not stripped:
        print("No empty_volume renames in log.")
        return

    manga_dir = os.path.dirname(log_path)
    live = []
    relocated = 0
    for r in stripped:
        if os.path.exists(r["new"]):
            live.append(r)
        else:
            found = find_current_path(r["new"], manga_dir)
            if found:
                live.append(dict(r, new=found))
                relocated += 1

    if not live:
        print("No logged empty_volume files found on disk.")
        return
    if relocated:
        print(f"  (resolved {relocated} stale log path(s) to current location)")

    def ch_key(r):
        m = CH_RE.search(r["new"])
        return float(m.group(1)) if m else 0

    live.sort(key=ch_key)

    print(f"\nFiles with stripped volumes ({len(live)} from log):\n")
    for r in live:
        fname = os.path.basename(r["new"])
        m = CH_RE.search(fname)
        ch_label = f"ch. {m.group(1)}" if m else "ch. ?"
        print(f"  {ch_label:<10}  {fname}")

    print("\nAssign volume (e.g. 208=17  or  208-221=17  or  'skip'  or  'quit'):")

    records_by_ch = {ch_key(r): r for r in live}

    def parse_assignment(s):
        s = s.strip()
        m = re.fullmatch(r"([\d.]+)-([\d.]+)=(\d+)", s)
        if m:
            start, end, vol = float(m.group(1)), float(m.group(2)), int(m.group(3))
            return {ch for ch in records_by_ch if start <= ch <= end}, vol
        m = re.fullmatch(r"([\d.]+)=(\d+)", s)
        if m:
            return {float(m.group(1))}, int(m.group(2))
        return None

    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return

        if raw.lower() in ("skip", ""):
            print("Skipped.")
            return
        if raw.lower() == "quit":
            print("Quit.")
            return

        parsed = parse_assignment(raw)
        if parsed is None:
            print("  Bad format. Try: 208=17  or  208-221=17")
            continue

        ch_set, vol = parsed
        targets = [records_by_ch[ch] for ch in sorted(ch_set) if ch in records_by_ch]
        missing = ch_set - set(records_by_ch.keys())

        if not targets:
            print("  No matching chapters found.")
            continue
        if missing:
            print(f"  Warning: chapters not found in log: {sorted(missing)}")

        print(f"\nRenames {len(targets)} files → inserting vol. {vol} before ch.")
        for r in targets:
            fname = os.path.basename(r["new"])
            new_fname = re.sub(r"(ch\. )", f"vol. {vol} \\1", fname, count=1)
            print(f"  {fname}")
            print(f"  → {new_fname}")

        confirm = input("\nApply? [y/n]: ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            continue

        for r in targets:
            old_path = r["new"]
            fname = os.path.basename(old_path)
            new_fname = re.sub(r"(ch\. )", f"vol. {vol} \\1", fname, count=1)
            do_rename(old_path, new_fname, "add_volume", log_data, log_path)
            r["new"] = os.path.join(os.path.dirname(old_path), new_fname)

        print(f"  Done. {len(targets)} file(s) renamed.\n")

        records_by_ch = {}
        for r in live:
            if os.path.exists(r["new"]):
                m = CH_RE.search(os.path.basename(r["new"]))
                if m:
                    records_by_ch[float(m.group(1))] = r

        if not records_by_ch:
            print("All files assigned. Done.")
            return

        print("Remaining unassigned chapters:")
        for ch in sorted(records_by_ch):
            fname = os.path.basename(records_by_ch[ch]["new"])
            print(f"  ch. {ch:<8}  {fname}")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Find and fix Kavita-incompatible manga filenames."
    )
    parser.add_argument(
        "--dir",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="Manga directory to scan (default: script's own directory)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Auto-apply all fixes without prompting",
    )
    parser.add_argument(
        "--add-volume", action="store_true",
        help="Assign volume numbers to previously stripped files",
    )
    parser.add_argument(
        "--move-group", action="store_true",
        help="Strip language prefix, move release group tag to end of filename",
    )
    args = parser.parse_args()

    manga_dir = os.path.abspath(args.dir)
    log_path  = os.path.join(manga_dir, LOG_FILENAME)

    if not os.path.isdir(manga_dir):
        print(f"Error: directory not found: {manga_dir}", file=sys.stderr)
        sys.exit(1)

    log_data = load_log(log_path)

    if args.add_volume:
        add_volume_mode(log_data, log_path)
        return

    if args.move_group:
        move_group_mode(manga_dir, log_data, log_path, auto=args.yes)
        return

    findings   = scan(manga_dir)
    dup_groups = scan_duplicates(manga_dir)

    if not findings and not dup_groups:
        print("No issues found. Library looks clean!")
        return

    print_summary(findings, dup_groups)

    if args.yes:
        for fpath, issue_name, new_name in findings:
            do_rename(fpath, new_name, issue_name, log_data, log_path)
            print(f"  renamed: {os.path.basename(fpath)}")
            print(f"       to: {new_name}")
        for group in dup_groups:
            apply_dup_group(group, log_data, log_path)
            print(f"  kept:    {group['keep_name']}")
            for p in group["delete_paths"]:
                print(f"  deleted: {os.path.basename(p)}")
        total = len(findings) + len(dup_groups)
        print(f"\nDone. {total} issue(s) resolved.")
    else:
        if findings:
            interactive_fix(findings, log_data, log_path)
        if dup_groups:
            if findings:
                print()
            print(f"--- Duplicate files ({len(dup_groups)} group(s)) ---\n")
            interactive_fix_dups(dup_groups, log_data, log_path)


if __name__ == "__main__":
    main()
