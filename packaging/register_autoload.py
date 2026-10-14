#!/usr/bin/env python
"""Add the audio bridge to NAOqi's user autoload.ini.

Copying a package manifest does not register an ALServiceManager service.
NAOqi starts external programs listed under [program] in
/home/nao/naoqi/preferences/autoload.ini. That file is merged with the
system autoload; it must not replace /etc/naoqi/autoload.ini.
"""

from __future__ import print_function

import os
import sys

LAUNCH = "/home/nao/nao-audio-bridge/run_forever.sh"
AUTOLOAD = "/home/nao/naoqi/preferences/autoload.ini"


def _is_section(line):
    stripped = line.strip()
    return stripped.startswith("[") and stripped.endswith("]") and len(stripped) > 2


def register_program(lines, launch):
    """Return autoload lines with ``launch`` once, inside ``[program]``."""
    cleaned = []
    for line in lines:
        if line.strip() == launch:
            continue
        cleaned.append(line.rstrip("\r"))

    out = []
    inserted = False
    in_program = False
    seen_program = False
    for line in cleaned:
        if _is_section(line):
            if in_program and not inserted:
                out.append(launch)
                inserted = True
            in_program = line.strip() == "[program]"
            if in_program:
                seen_program = True
        out.append(line)

    if in_program and not inserted:
        out.append(launch)
        inserted = True

    if not seen_program:
        if out and out[-1].strip() != "":
            out.append("")
        out.append("[program]")
        out.append(launch)
    return out


def write_autoload(path, launch):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    lines = []
    if os.path.isfile(path):
        with open(path, "r") as handle:
            lines = handle.read().splitlines()
    updated = register_program(lines, launch)
    with open(path, "w") as handle:
        handle.write("\n".join(updated) + "\n")


def main(argv):
    path = AUTOLOAD
    launch = LAUNCH
    if len(argv) >= 2:
        path = argv[1]
    if len(argv) >= 3:
        launch = argv[2]
    write_autoload(path, launch)
    print("Registered %s in %s" % (launch, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
