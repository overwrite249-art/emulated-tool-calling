#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rebuild the standalone, single-file emutools.py from the emutools/ package.

    python3 build_single_file.py             # writes ./emutools.py
    python3 build_single_file.py out.py      # writes ./out.py
    python3 build_single_file.py --check     # exit 1 if ./emutools.py is stale

On the default branch nobody has to run this by hand (PRs and other branches
must commit the rebuilt file; CI fails them when it is stale). The built file is committed at the repository
root, so `curl`-and-run works straight from a clone, and CI rebuilds it on every
push to the default branch and commits it back when it differs (`--check` is what
makes a stale file a red build). The package exists so the code is reviewable
module by module; the single file is what you deploy.

The output is verified to parse before it is written, and `emutools.py --selftest`
is the real check that it worked.
"""
import argparse
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "emutools")

HS = "# --- generated header: build_single_file.py strips these blocks ---"
HE = "# --- end generated header ---"

# Dependency order: every definition appears before anything that runs it.
ORDER = [
    "_prelude", "core", "protocol", "wire", "engine", "server",
    "selftest_a", "selftest_b", "cli",
]


def strip_generated(text):
    """Drop the generated import headers and __all__ blocks."""
    kept = []
    skipping = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped == HS:
            skipping = True
            continue
        if stripped == HE:
            skipping = False
            continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip("\n")


def render():
    """Return the full text of the single-file build."""
    init = open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    doc = ast.get_docstring(ast.parse(init))
    if not doc:
        raise SystemExit("emutools/__init__.py has no module docstring")

    parts = [
        "#!/usr/bin/env python3",
        "# -*- coding: utf-8 -*-",
        '"""' + doc + '"""',
    ]
    for mod in ORDER:
        path = os.path.join(PKG, mod + ".py")
        if not os.path.exists(path):
            raise SystemExit("missing module: %s" % path)
        parts.append(strip_generated(open(path, encoding="utf-8").read()))
    parts.append('if __name__ == "__main__":\n    sys.exit(main())')

    text = "\n\n\n".join(parts) + "\n"

    # Refuse to emit a file that will not even parse.
    ast.parse(text)
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dest", nargs="?", default=os.path.join(HERE, "emutools.py"),
                    help="output path (default: ./emutools.py)")
    ap.add_argument("--check", action="store_true", help="exit 1 if dest is stale")
    args = ap.parse_args(argv)
    dest, check = args.dest, args.check
    text = render()

    if check:
        current = None
        if os.path.exists(dest):
            with open(dest, encoding="utf-8") as fh:
                current = fh.read()
        if current == text:
            print("%s is up to date (%d lines)" % (dest, text.count("\n")))
            return 0
        print("%s is stale: run `python3 build_single_file.py` and commit the result"
              % dest, file=sys.stderr)
        return 1

    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("wrote %s (%d bytes, %d lines)" % (dest, len(text.encode("utf-8")), text.count("\n")))
    print("verify with: python3 %s --selftest" % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
