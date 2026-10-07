#!/usr/bin/env python3
"""A sim-mesh firmware zip of a standard Reticulum node.

    make_zip.py RNODE [--rns V] [--lxmf V] [--out DIR] [--version V]

RNODE is the RNode the station drives, one of:

- microReticulum_Firmware's Linux daemon built with `[env:sim-mesh-rnode]`
  (`pio run -e sim-mesh-rnode`, once `sim` has built the radio), linked with
  sim-mesh's radio library by name. The zip holds it as `rnode`, with the
  shared libraries it needs that the firmware contract does not promise
  under lib/. It is named `rns-<rns>-rnode-sx1262_<arch>_<version>.zip`,
  the architecture this machine's and the version this moment's UTC stamp
  unless given.
- a sim-mesh firmware zip of Reticulous or Sergeyculum, whose files the zip
  holds under `rnode/`, with `rnode.json` saying how station.py starts it.
  It is named `rns-<rns>-<its base>_<its arch>_<its version>.zip`.

Either way the zip holds this directory's station.py and driver.py, and
Reticulum, LXMF and pyserial from PyPI under python/ (pip --target, for the
python3 sim-mesh runs drivers with), without their compiled dependencies, so
it runs on any architecture and any Python: Reticulum then uses its own
cryptography. rns and lxmf are PyPI's latest unless given.
"""

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PROVIDED = re.compile(r"^(libc|libm|libdl|librt|libpthread|libstdc\+\+|libgcc_s|ld-linux.*"
                      r"|libsimradio-.*)\.so")
NESTED = ("reticulous", "sergeyculum")


def latest(package):
    with urllib.request.urlopen("https://pypi.org/pypi/%s/json" % package, timeout=30) as r:
        return json.load(r)["info"]["version"]


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


def nested(path):
    """A firmware zip's node.yaml, and what station.py needs to start it."""
    with zipfile.ZipFile(path) as src:
        inner = yaml.safe_load(src.read("node.yaml"))
    kind = str(inner["base"]).split("-")[0]
    if kind not in NESTED:
        sys.exit("%s: %s is none of %s" % (path, inner["base"], ", ".join(NESTED)))
    env = dict(inner.get("env") or {})
    # Sergeyculum's KISS door as a pty, which RNodeInterface opens as a serial port.
    env.pop("SIM_MESH_DOOR", None)
    spec = {"kind": kind, "exec": inner["exec"], "fixed": inner.get("fixed"), "env": env}
    return inner, spec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("rnode")
    ap.add_argument("--rns")
    ap.add_argument("--lxmf")
    ap.add_argument("--out", default=".")
    ap.add_argument("--version")
    args = ap.parse_args(argv)
    rns = args.rns or latest("rns")
    lxmf = args.lxmf or latest("lxmf")
    title = "Reticulum %s with LXMF %s" % (rns, lxmf)
    if zipfile.is_zipfile(args.rnode):
        inner, spec = nested(args.rnode)
        node = {"base": "rns-%s-%s" % (rns, inner["base"]), "arch": inner["arch"],
                "version": args.version or str(inner["version"]), "category": "reticulum",
                "radio": inner["radio"], "title": "%s on %s as its RNode" % (title, inner["title"])}
        if inner.get("hardware"):
            node["hardware"] = inner["hardware"]
        node.update({"exec": "station.py", "driver": "driver.py",
                     "rnode": "%s_%s_%s" % (inner["base"], inner["arch"], inner["version"])})
    else:
        inner = None
        arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine().lower(),
                                                           platform.machine().lower())
        version = args.version or datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
        node = {"base": "rns-%s-rnode-sx1262" % rns, "arch": arch, "version": version,
                "category": "reticulum", "radio": "sx1262", "title": "%s on an RNode" % title,
                "hardware": "ESP32-S3", "exec": "station.py", "driver": "driver.py"}
        libs = needed_libraries(args.rnode)
        if libs:
            node["env"] = {"LD_LIBRARY_PATH": "./lib"}
    name = "%s_%s_%s" % (node["base"], node["arch"], node["version"])
    os.makedirs(args.out, exist_ok=True)
    dest = os.path.join(args.out, name + ".zip")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
                        "--target", os.path.join(tmp, "python"), "rns==%s" % rns,
                        "lxmf==%s" % lxmf, "pyserial"], check=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("node.yaml", yaml.safe_dump(node, sort_keys=False))
            executable(zf, os.path.join(HERE, "station.py"), "station.py")
            zf.write(os.path.join(HERE, "driver.py"), "driver.py")
            add_tree(zf, os.path.join(tmp, "python"), "python")
            if inner is None:
                executable(zf, args.rnode, "rnode")
                for soname, path in libs:
                    zf.write(path, os.path.join("lib", soname))
            else:
                zf.writestr("rnode/rnode.json", json.dumps(spec, indent=1) + "\n")
                with zipfile.ZipFile(args.rnode) as src:
                    for info in src.infolist():
                        if info.filename == inner["driver"]:
                            continue
                        data = b"" if info.is_dir() else src.read(info)
                        info.filename = "rnode/" + info.filename
                        zf.writestr(info, data)
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
