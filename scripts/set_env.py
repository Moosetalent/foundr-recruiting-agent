"""Set one value in .env without an editor or shell quoting.

Usage:  python scripts/set_env.py SLACK_APP_TOKEN
It prompts for the value (typing is hidden), strips whitespace and quotes,
replaces the whole line in .env (dropping any trailing comment), and confirms.
"""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

ENV = Path(".env")

if len(sys.argv) != 2 or not re.fullmatch(r"[A-Z0-9_]+", sys.argv[1]):
    sys.exit("usage: python scripts/set_env.py VARIABLE_NAME")
name = sys.argv[1]

value = getpass.getpass(f"Paste the value for {name} and press Enter (nothing will show): ")
value = value.strip().strip('"').strip("'")
if not value:
    sys.exit("Nothing pasted; .env unchanged.")
if any(ord(c) > 127 for c in value):
    sys.exit("The value contains a non-ASCII character (probably a '…'); copy it again from the source.")

text = ENV.read_text() if ENV.exists() else ""
line = f"{name}={value}"
if re.search(rf"^{name}=.*$", text, flags=re.M):
    text = re.sub(rf"^{name}=.*$", line, text, flags=re.M)
else:
    text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
ENV.write_text(text)
print(f"{name} set ({len(value)} characters, starts with {value[:8]}...). Saved to .env")
