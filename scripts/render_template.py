"""Render an infra/*.json template with a small set of substitutions.

Used by bootstrap_aws.sh in place of jq, so the only system deps are aws CLI,
bash, and python3 (which is already required by the seeders). Output goes to
stdout, original file is never modified.

Operations:
  --strip-comment            Remove the top-level "_comment" key, if present.
  --replace FIND=REPLACE     Replace FIND with REPLACE in every string value
                             anywhere in the doc. May be passed multiple times.
  --set-key DOTTED.KEY=VALUE Set a single string value at a dotted path.
                             May be passed multiple times.

Example:
  python3 render_template.py infra/bucket-policy.json \\
      --strip-comment \\
      --replace BUCKET_NAME=csd-benchmark-flat-150k

Pretty-printed JSON is emitted so the rendered file is readable when piped
to a tmpfile during a bootstrap debug session.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def replace_strings(node: Any, find: str, replace: str) -> Any:
    if isinstance(node, dict):
        return {k: replace_strings(v, find, replace) for k, v in node.items()}
    if isinstance(node, list):
        return [replace_strings(x, find, replace) for x in node]
    if isinstance(node, str):
        return node.replace(find, replace)
    return node


def set_dotted(doc: Any, dotted_key: str, value: str) -> None:
    parts = dotted_key.split(".")
    cur = doc
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(f"path {dotted_key!r} does not resolve in document")
        cur = cur[p]
    if not isinstance(cur, dict):
        raise KeyError(f"path {dotted_key!r} parent is not an object")
    cur[parts[-1]] = value


def parse_kv(value: str, flag: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            f"{flag} expects FIND=REPLACE (or KEY=VALUE), got {value!r}"
        )
    k, _, v = value.partition("=")
    return k, v


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("template", help="path to the infra/*.json template")
    p.add_argument("--strip-comment", action="store_true")
    p.add_argument(
        "--replace",
        action="append",
        default=[],
        metavar="FIND=REPLACE",
        help="replace FIND with REPLACE in every string in the doc",
    )
    p.add_argument(
        "--set-key",
        action="append",
        default=[],
        metavar="DOTTED.KEY=VALUE",
        help="set a single string value at a dotted path",
    )
    args = p.parse_args()

    with open(args.template, "r", encoding="utf-8") as fh:
        doc = json.load(fh)

    if args.strip_comment and isinstance(doc, dict):
        doc.pop("_comment", None)

    for v in args.replace:
        find, replace = parse_kv(v, "--replace")
        doc = replace_strings(doc, find, replace)

    for v in args.set_key:
        key, val = parse_kv(v, "--set-key")
        set_dotted(doc, key, val)

    json.dump(doc, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
