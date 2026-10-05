#!/usr/bin/env python3
"""A sim-mesh firmware zip of microReticulum_Firmware's Linux daemon.

    make_zip.py DAEMON [--base microreticulum-sx1262] [--mode MODE] [--title T] [--out DIR] [--version V]

DAEMON is `.pio/build/sim-mesh/rnode_firmware_native`, built with
`pio run -e sim-mesh` once `sim` has built the radio, linked with sim-mesh's
radio library by name. `--mode` is the LoRa interface's Reticulum mode it is
started with (MR_LORA_INTERFACE_MODE; gateway when not given): a stand-in for
another firmware's rules is the same daemon under another base with another
mode or branch, and a title that says so (`--base microreticulum-jrl290-sx1262
--mode full --title "microReticulum (jrl290 rules)"`). The zip
holds the daemon, this directory's driver.py and the shared libraries the
daemon needs that the firmware contract does not promise, under lib/.
"""

import argparse
import datetime
import os
import platform
import re
import subprocess
import sys
import zipfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PROVIDED = re.compile(r"^(libc|libm|libdl|librt|libpthread|libstdc\+\+|libgcc_s|ld-linux.*"
                      r"|libsimradio-.*)\.so")


def needed_libraries(exe):
    out = subprocess.run(["ldd", exe], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        m = re.match(r"\s*(\S+) => (\S+)", line)
        if m and not PROVIDED.match(m.group(1)) and os.path.isfile(m.group(2)):
            found.append((m.group(1), m.group(2)))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("daemon")
    ap.add_argument("--base", default="microreticulum-sx1262")
    ap.add_argument("--mode", default=None)
    ap.add_argument("--title", default="microReticulum (attermann)")
    ap.add_argument("--out", default=".")
    ap.add_argument("--version",
                    default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S"))
    args = ap.parse_args(argv)
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine().lower(),
                                                       platform.machine().lower())
    libs = needed_libraries(args.daemon)
    env = {}
    if libs:
        env["LD_LIBRARY_PATH"] = "./lib"
    if args.mode:
        env["MR_LORA_INTERFACE_MODE"] = args.mode
    node = {"base": args.base, "arch": arch, "version": args.version, "category": "reticulum",
            "radio": "sx1262", "title": args.title, "hardware": "ESP32-S3",
            "exec": "rnode_firmware_native", "driver": "driver.py"}
    if env:
        node["env"] = env
    os.makedirs(args.out, exist_ok=True)
    dest = os.path.join(args.out, "%s_%s_%s.zip" % (args.base, arch, args.version))
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("node.yaml", yaml.safe_dump(node, sort_keys=False))
        info = zipfile.ZipInfo(node["exec"])
        info.external_attr = 0o100755 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        with open(args.daemon, "rb") as f:
            zf.writestr(info, f.read())
        zf.write(os.path.join(HERE, "driver.py"), "driver.py")
        for soname, path in libs:
            zf.write(path, os.path.join("lib", soname))
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
