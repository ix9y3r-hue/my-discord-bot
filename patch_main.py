from pathlib import Path
import sys

path = Path("main.py")
if not path.exists():
    sys.exit("ERROR: Put this script in the same folder as main.py, then run it again.")

source = path.read_text(encoding="utf-8")
old = '    "fragment_retries": 3,\n}'
new = '    "fragment_retries": 3,\n    # Let yt-dlp use Deno to solve YouTube JavaScript player challenges.\n    "js_runtimes": {"deno": {}},\n}'

if '"js_runtimes"' in source:
    print("main.py already has js_runtimes; no change needed.")
elif old in source:
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    print("Updated main.py: enabled the Deno JavaScript runtime for yt-dlp.")
else:
    sys.exit("ERROR: Could not find the expected BASE_YTDL section. No changes were made.")
