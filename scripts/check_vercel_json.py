"""Validate vercel.json against Vercel's published schema before pushing.

Written after every build for ten hours failed on one line:

    The `vercel.json` schema validation failed with the following message:
    should NOT have additional property `_comment_ignoreCommand`

JSON has no comment syntax, so an explanation of the ignoreCommand was put in
a `_comment_ignoreCommand` key. The schema sets additionalProperties: false,
so Vercel rejected the file outright - and a rejected build leaves the previous
deployment serving, which made it silent. Ten code pushes went nowhere while
the proxy kept reporting a $202,287 account, and two wrong diagnoses were
chased before the build log was read.

Run it before pushing a vercel.json change:

    python scripts/check_vercel_json.py
"""
import json
import os
import sys
import urllib.request

SCHEMA = "https://openapi.vercel.sh/vercel.json"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    path = os.path.join(HERE, "vercel.json")
    with open(path, encoding="utf-8") as fh:
        try:
            conf = json.load(fh)
        except json.JSONDecodeError as exc:
            print("vercel.json is not valid JSON: %s" % exc)
            return 1

    try:
        with urllib.request.urlopen(SCHEMA, timeout=30) as fh:
            schema = json.loads(fh.read().decode())
    except Exception as exc:                              # noqa: BLE001
        # Offline is not a failure: the local checks below still run.
        print("could not fetch the schema (%s) - checked JSON only" % exc)
        return 0

    allowed = set(schema.get("properties", {}))
    strict = schema.get("additionalProperties") is False
    unknown = [k for k in conf if k not in allowed and k != "$schema"]

    if unknown and strict:
        print("vercel.json has %d key(s) the schema forbids: %s"
              % (len(unknown), ", ".join(sorted(unknown))))
        print("additionalProperties is false, so the build WILL fail.")
        print("There is no comment syntax in JSON - document it elsewhere.")
        return 1

    print("vercel.json valid - %d key(s): %s"
          % (len(conf), ", ".join(sorted(k for k in conf))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
