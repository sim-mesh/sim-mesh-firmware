#!/usr/bin/env python3
"""meshtasticd as a sim-mesh firmware zip.

    make_zip.py PROGRAM [--arch ARCH] [--out DIR]

PROGRAM is meshtasticd built with the meshtastic clone's [env:sim-mesh]
(.pio/build/sim-mesh/meshtasticd; for the other architecture, built with
SIM_MESH_ARCH and a PLATFORMIO_BUILD_DIR of its own). The zip holds it as
`meshtasticd`, the station's host (host.py), the driver (driver.py),
node.yaml, lib/: every shared library it loads beyond the C library, the C++
runtime and the radio, from that architecture's multiarch directory (its
packages installed there, `<pkg>:amd64` for x86_64 on an aarch64 host), and
pylib/: the protobuf bindings generated from the clone's own protobufs and the
pure-Python protobuf runtime. ARCH is the program's own unless given.
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CLONE = os.path.normpath(os.path.join(HERE, "..", "..", "competition", "meshtastic_firmware"))
BASE = "meshtastic-sx1262"
GRPCIO_TOOLS = "grpcio-tools==1.84.0"
PROTOBUF = "protobuf==7.36.2"
CHECK = "from meshtastic import mesh_pb2, admin_pb2, portnums_pb2, config_pb2"
# What a station may count on (the contract, "What a firmware may count on"),
# and the radio, which sim-mesh provides: everything else it loads is in lib/.
PROVIDED = re.compile(r"^(libc|libm|libdl|librt|libpthread|libstdc\+\+|libgcc_s)\.so|^ld-linux|"
                      r"^libsimradio-")
MACHINES = {"AArch64": "aarch64", "Advanced Micro Devices X86-64": "x86_64"}


def elf_arch(path):
    """The architecture an ELF file is built for."""
    out = subprocess.run(["readelf", "-h", path], check=True, capture_output=True,
                         text=True).stdout
    machine = re.search(r"Machine:\s*(.+)", out).group(1).strip()
    if machine not in MACHINES:
        raise SystemExit("%s: built for %s" % (path, machine))
    return MACHINES[machine]


def needed(path):
    out = subprocess.run(["readelf", "-d", path], check=True, capture_output=True,
                         text=True).stdout
    return re.findall(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", out)


def bundle_libs(program, arch, dest):
    """Every library `program` loads that a station is not given, and theirs,
    copied into `dest` under their sonames: [soname]."""
    dirs = ["/usr/lib/%s-linux-gnu" % arch, "/lib/%s-linux-gnu" % arch]
    os.makedirs(dest, exist_ok=True)
    todo, done = needed(program), []
    while todo:
        name = todo.pop(0)
        if name in done or PROVIDED.search(name):
            continue
        found = next((os.path.join(d, name) for d in dirs if os.path.exists(os.path.join(d, name))),
                     None)
        if found is None:
            raise SystemExit("%s needs %s, which no %s directory holds (install its %s package)"
                             % (program, name, arch, arch))
        shutil.copyfile(os.path.realpath(found), os.path.join(dest, name))
        done.append(name)
        todo += needed(found)
    return done


def pip(*args):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                    *args], check=True)


def build_pylib(dest, clone=CLONE):
    """The bindings and the runtime into `dest`, checked to import under the
    pure-Python runtime."""
    protos = os.path.join(clone, "protobufs")
    with tempfile.TemporaryDirectory() as tmp:
        tools = os.path.join(tmp, "tools")
        pylib = os.path.join(tmp, "pylib")
        os.makedirs(pylib)
        pip("--target", tools, GRPCIO_TOOLS)
        sources = sorted(os.path.join(protos, "meshtastic", name)
                         for name in os.listdir(os.path.join(protos, "meshtastic"))
                         if name.endswith(".proto"))
        subprocess.run([sys.executable, "-m", "grpc_tools.protoc", "-I", protos,
                        "--python_out=" + pylib, *sources, os.path.join(protos, "nanopb.proto")],
                       check=True, env=dict(os.environ, PYTHONPATH=tools))
        pip("--target", pylib, "--no-deps", "--only-binary", ":all:", "--platform", "any",
            "--implementation", "py", "--abi", "none", PROTOBUF)
        subprocess.run([sys.executable, "-c", CHECK], check=True,
                       env=dict(os.environ, PYTHONPATH=pylib,
                                PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION="python"))
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(pylib, dest)


def node_yaml(arch, version):
    return {"base": BASE, "arch": arch, "version": version, "category": "meshtastic",
            "radio": "sx1262", "title": "Meshtastic 2.7.26 (meshtasticd)",
            "hardware": "Raspberry Pi", "exec": "host.py", "driver": "driver.py",
            "env": {"PYTHONPATH": "./pylib", "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
                    "LD_LIBRARY_PATH": "./lib"}}


def make_zip(program, arch, out_dir):
    if elf_arch(program) != arch:
        raise SystemExit("%s is built for %s, not %s" % (program, elf_arch(program), arch))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    name = "%s_%s_%s" % (BASE, arch, stamp)
    path = os.path.join(out_dir, name + ".zip")
    with tempfile.TemporaryDirectory() as tmp:
        pylib = os.path.join(tmp, "pylib")
        build_pylib(pylib)
        bundle_libs(program, arch, os.path.join(tmp, "lib"))
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            def put(src, arc, mode):
                info = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = (0o100000 | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(src, "rb") as handle:
                    zf.writestr(info, handle.read())
            put(program, "meshtasticd", 0o755)
            put(os.path.join(HERE, "host.py"), "host.py", 0o755)
            put(os.path.join(HERE, "driver.py"), "driver.py", 0o644)
            zf.writestr("node.yaml", yaml.safe_dump(node_yaml(arch, stamp), sort_keys=False))
            for each in sorted(os.listdir(os.path.join(tmp, "lib"))):
                put(os.path.join(tmp, "lib", each), "lib/" + each, 0o644)
            for root, _, files in os.walk(pylib):
                for each in sorted(files):
                    if each.endswith(".pyc"):
                        continue
                    src = os.path.join(root, each)
                    put(src, os.path.relpath(src, tmp), 0o644)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("program")
    parser.add_argument("--arch", help="the program's own when not given")
    parser.add_argument("--out", default=".")
    args = parser.parse_args()
    print(make_zip(args.program, args.arch or elf_arch(args.program), args.out))


if __name__ == "__main__":
    main()
