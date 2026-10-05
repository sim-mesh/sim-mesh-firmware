#!/usr/bin/env python3
"""A sim-mesh firmware zip of a standard Reticulum node.

    make_zip.py RNODE [--rns 1.5.2] [--lxmf 1.1.1] [--out DIR] [--version V]

RNODE is microReticulum_Firmware's Linux daemon built with
`[env:sim-mesh-rnode]` (`pio run -e sim-mesh-rnode`, once `sim` has built
the radio), linked with sim-mesh's radio library by name. The zip holds it as
`rnode`, this directory's station.py and driver.py, Reticulum and LXMF from
PyPI under python/ (pip --target, for the python3 sim-mesh runs drivers
with), and the shared libraries the RNode needs that the firmware contract
does not promise, under lib/. It is named
`standard-reticulum-sx1262_<arch>_<version>.zip`, the version this moment's
UTC stamp unless given.
"""

import argparse
import datetime
import os
import platform
import re
import subprocess
import sys
import tempfile
import zipfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "standard-reticulum-sx1262"
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


def add_tree(zf, root, prefix):
    for folder, _, files in os.walk(root):
        for each in files:
            path = os.path.join(folder, each)
            zf.write(path, os.path.join(prefix, os.path.relpath(path, root)))


def executable(zf, path, name):
    info = zipfile.ZipInfo(name)
    info.external_attr = 0o100755 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    with open(path, "rb") as f:
        zf.writestr(info, f.read())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("rnode")
    ap.add_argument("--rns", default="1.5.2")
    ap.add_argument("--lxmf", default="1.1.1")
    ap.add_argument("--out", default=".")
    ap.add_argument("--version",
                    default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S"))
    args = ap.parse_args(argv)
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine().lower(),
                                                       platform.machine().lower())
    name = "%s_%s_%s" % (BASE, arch, args.version)
    libs = needed_libraries(args.rnode)
    node = {"base": BASE, "arch": arch, "version": args.version, "category": "reticulum",
            "radio": "sx1262",
            "title": "Standard Reticulum (rns %s, lxmf %s) on an RNode" % (args.rns, args.lxmf),
            "hardware": "ESP32-S3", "exec": "station.py", "driver": "driver.py"}
    if libs:
        node["env"] = {"LD_LIBRARY_PATH": "./lib"}
    os.makedirs(args.out, exist_ok=True)
    dest = os.path.join(args.out, name + ".zip")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target",
                        os.path.join(tmp, "python"), "rns==%s" % args.rns,
                        "lxmf==%s" % args.lxmf], check=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("node.yaml", yaml.safe_dump(node, sort_keys=False))
            executable(zf, os.path.join(HERE, "station.py"), "station.py")
            executable(zf, args.rnode, "rnode")
            zf.write(os.path.join(HERE, "driver.py"), "driver.py")
            add_tree(zf, os.path.join(tmp, "python"), "python")
            for soname, path in libs:
                zf.write(path, os.path.join("lib", soname))
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
