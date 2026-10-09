"""Cross-check every DOM id player.js looks up against the templates that define them.

Cheap guard against the class of bug where the markup and the script drift apart
(a renamed button silently stops working because getElementById returns null).
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

js = (ROOT / "static" / "js" / "player.js").read_text(encoding="utf-8")
tpl = ""
for path in sorted((ROOT / "templates").rglob("*.html")):
    tpl += path.read_text(encoding="utf-8")

defined = set(re.findall(r'id="([^"]+)"', tpl))
looked_up = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))

# data-* hooks the script binds to via delegation.
# A trailing "-" is an interpolated selector prefix ("[data-follow-" + kind),
# not a real hook, so those are filtered out.
hooks = {name for name in re.findall(r"data-([a-z-]+)", js) if not name.endswith("-")}
declared = set(re.findall(r"data-([a-z-]+)", tpl))

print(f"ids defined in templates : {len(defined)}")
print(f"ids looked up by player.js: {len(looked_up)}")
missing = sorted(looked_up - defined)
print(f"MISSING IDS               : {missing or 'none'}")

print()
print(f"data-* hooks used by js   : {sorted(hooks)}")
print(f"data-* hooks in templates : {sorted(declared)}")
absent = sorted(hooks - declared)
print(f"HOOKS WITH NO MARKUP      : {absent or 'none'}")

sys.exit(1 if missing or absent else 0)
