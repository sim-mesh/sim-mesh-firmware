#!/usr/bin/env python3
"""meshtasticd as a sim-mesh firmware zip.

    make_zip.py PROGRAM [--arch ARCH] [--out DIR]

PROGRAM is meshtasticd built with the meshtastic clone's [env:sim-mesh]
(.pio/build/sim-mesh/meshtasticd). The zip holds it as `meshtasticd`, the
station's host (host.py), the driver (driver.py), node.yaml, and pylib/: the
protobuf bindings generated from the clone's own protobufs and the pure-Python
protobuf runtime, so nothing in it depends on the architecture but the
program.
"""

import argparse
import datetime
import os
import platform
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
            "env": {"PYTHONPATH": "./pylib", "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python"}}


def make_zip(program, arch, out_dir):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    name = "%s_%s_%s" % (BASE, arch, stamp)
    path = os.path.join(out_dir, name + ".zip")
    with tempfile.TemporaryDirectory() as tmp:
        pylib = os.path.join(tmp, "pylib")
        build_pylib(pylib)
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
    parser.add_argument("--arch", default=platform.machine())
    parser.add_argument("--out", default=".")
    args = parser.parse_args()
    print(make_zip(args.program, args.arch, args.out))


if __name__ == "__main__":
    main()
