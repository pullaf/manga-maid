#!/usr/bin/env bash
# manga-fix.sh — friendly launcher for manga-fix.py

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$SCRIPT_DIR/manga-fix.py"

echo ""
echo "  manga-fix — Kavita filename fixer"
echo "  ================================="
echo ""
echo "  1) Interactive  — walk through each issue one by one"
echo "  2) Auto         — fix everything automatically (no prompts)"
echo "  3) Add volume   — assign vol. numbers to previously stripped files"
echo "  4) Move groups  — strip language, move [Group] tag to end of filename"
echo "  q) Quit"
echo ""
read -rp "  Choice: " choice

case "$choice" in
  1) python3 "$PY" ;;
  2) python3 "$PY" --yes ;;
  3) python3 "$PY" --add-volume ;;
  4) python3 "$PY" --move-group ;;
  q|Q) echo "  Bye!"; exit 0 ;;
  *) echo "  Invalid choice, bailing out."; exit 1 ;;
esac
