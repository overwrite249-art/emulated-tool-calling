#!/usr/bin/env python3
"""Build frontend: run bun build on web/app.ts, copy static assets to dist/."""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
DIST = os.path.join(ROOT, "dist")


def main():
    os.makedirs(DIST, exist_ok=True)
    entry = os.path.join(WEB, "app.ts")
    outfile = os.path.join(DIST, "app.js")
    r = subprocess.run(
        ["bun", "build", entry, "--outfile", outfile],
        capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        sys.exit(1)
    for name in ("index.html", "style.css"):
        src = os.path.join(WEB, name)
        dst = os.path.join(DIST, name)
        if os.path.isfile(src):
            shutil.copyfile(src, dst)
    print(f"built {outfile}")


if __name__ == "__main__":
    main()
