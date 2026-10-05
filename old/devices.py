#!/usr/bin/env python3
"""Devices: the station builds a node can run.

```
sim-mesh ── GET <base>/index.html ────────────────────────────────► site     which catalogues there are
sim-mesh ── GET <catalogue>/index.html ───────────────────────────► site or builds/<catalogue>/
sim-mesh: the newest <project>_hw-sim-mesh-<arch>_<stamp>.zip per project, this machine's arch only,
          into devices/latest/index.yaml; a fetched one older than that is removed
sim-mesh ── its node.yaml, read from the zip (a range request on the web) ► site or builds/
          once per new build: what it plays and its kind, before it is fetched
script  firmware("all", "reticulous_dev_latest") ──► ensure() ──► resolve()
sim-mesh ── GET <catalogue>/<project>_hw-sim-mesh-<arch>_<stamp>.zip ► site    only when used
sim-mesh: unzip to devices/latest/.part-…, check node.yaml, rename to
          devices/latest/<project>_<catalogue>_<stamp>/
page ── Save ──► devices/saved/<project>_<catalogue>_<stamp>/      a copy that stays
page ── POST /api/devices/import?name=<zip name> (the zip) ──► devices/saved/<project>_imported_<stamp>/
```

A **device file** is one station build ready to run: a zip holding an
executable, whatever it needs beside it, and a `node.yaml` saying what those
are (NODE.md is the spec). It is published in a build catalogue exactly as a
board image is, named `<project>_<entry>_<stamp>.zip` with
`hw-sim-mesh-<arch>` as the entry, and listed in that catalogue's `index.html`.
The executable is native code dynamically linked against the builder's C
library, so a device runs only on a machine of the architecture its
`node.yaml` names.

**What a device is called.** `<project>_<catalogue>_<stamp>`, one name for
one build: `reticulous_dev_20260927140352`. The project is the catalogue
filename's slug, the catalogue is where it was published. Scripts name
devices this way (`firmware(which, name)`), and two more forms:

- `<project>_<catalogue>_latest`: the newest build of that project in that
  catalogue, whichever it is at the moment it is used;
- a path: a package directory (it holds `node.yaml`), or a workspace's
  `build.linux` (it holds `reticulous.elf` and `data_merged/`).

**Latest.** A survey reads every catalogue's listing, fetching nothing, and
keeps the newest build per project and catalogue for this machine in
`devices/latest/index.yaml`. The catalogues are the ones the web's
`SIM_MESH_CATALOGUES` index lists (by default `https://reticulous.net/builds/`)
and every catalogue directory (one holding an `index.html`) in `builds/`
beside sim-mesh, a local one joining the web's of its name, the newer build
winning; one called `imported` is `builds-imported`. Each new build's `node.yaml` is read from its zip as
it is surveyed (on the web by HTTP range requests: the zip's directory and
that one member, not the build), so the listing says what it plays and its
kind before it is fetched. A `_latest` build is downloaded when it is used,
into `devices/latest/<name>/`, and a fetched one is removed as soon as a
survey sees a newer one for its project and catalogue, fetched or not. So
`devices/latest/` holds at most one build per project and catalogue.

**Compiled builds** are for a project that publishes no sim-mesh build yet:
`devices/local/<project>_<catalogue>.yaml`, a `node.yaml` that is not in a
package, its `elf`, `fixed` and `tools` paths relative to the file and free
to point anywhere, run in place from wherever it was last compiled. Each is
the latest of its catalogue, `sergeyculum_local_latest` for Sergeyculum's
`fw/sim-mesh`, its stamp its executable's modification time.

**Saved** builds are copies that stay: `devices/saved/<name>/`, a package
directory like any other, made by Save from a latest build (fetching it first
when it has not been) or by importing a zip, which joins the catalogue
`imported`. Only deleting one removes it.

A directory is whole or absent: a package is unzipped or copied under a
`.part-` name, checked, and renamed into place.

Fetching is aiohttp in the caller's loop, and unzipping and copying run in a
worker thread, so nothing here blocks an event loop. Run as a script it is
the CLI behind `sim-mesh devices`.
"""

import argparse
import asyncio
import datetime
import html.parser
import json
import os
import platform
import re
import shutil
import stat
import sys
import urllib.parse
import zipfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEVICES_DIR = os.path.join(ROOT, "devices")
BUILDS_DIR = os.path.join(os.path.dirname(ROOT), "builds")
LOCAL = "local"                 # devices/local/: compiled builds, run in place
LATEST = "latest"               # devices/latest/: the newest of each catalogue, once used
SAVED = "saved"                 # devices/saved/: copies that stay
IMPORTED = "imported"           # the catalogue an imported zip joins
COMPILED = "compiled"           # a latest row's source: a compiled build, run in place
NOT_BUILT = "no firmware"       # a compiled build whose executable is not there
PEEK_TAIL = 1 << 16             # a zip's end read for its directory, by range request
INDEX_YAML = "index.yaml"
DEFAULT_BASE = os.environ.get("SIM_MESH_CATALOGUES", "https://reticulous.net/builds/")
WEB_FALLBACK = ("stable", "dev")    # the web's catalogues when its index cannot be read
ENTRY_PREFIX = "hw-sim-mesh-"
NODE_YAML = "node.yaml"
ORIGIN_YAML = "origin.yaml"
PART_PREFIX = ".part-"
CHUNK = 1 << 16
FETCH_TIMEOUT_S = 600
SURVEY_TIMEOUT_S = 20

# A workspace build.linux: what spangap leaves for a `target: linux` build.
WORKSPACE_ELF = "reticulous.elf"
WORKSPACE_FIXED = "data_merged"
WORKSPACE_KIND = "reticulous"

ARCH_ALIASES = {"arm64": "aarch64", "amd64": "x86_64", "x64": "x86_64"}


class DeviceError(Exception):
    """A device that cannot be used, or a name that names none."""


def machine_arch():
    """This machine's architecture, spelled as `uname -m` spells it on Linux."""
    arch = platform.machine().lower()
    return ARCH_ALIASES.get(arch, arch)


def split_image_name(name):
    """A catalogue image filename as (slug, entry, stamp), or None.

    Images are `<slug>_<entry>_<stamp>.zip`. The slug never holds an
    underscore and the stamp is all digits, so the entry, which may hold
    underscores, is what lies between the first and the last one.
    """
    if not name.endswith(".zip"):
        return None
    head, _, stamp = name[:-4].rpartition("_")
    if not head or not stamp.isdigit():
        return None
    slug, _, entry = head.partition("_")
    if not slug or not entry:
        return None
    return slug, entry, stamp


def split_name(name):
    """A device name as (project, catalogue, stamp), the stamp `latest` or
    all digits; None for anything else. The project holds no underscore and
    the stamp none, so the catalogue is what lies between."""
    head, _, stamp = str(name).rpartition("_")
    if not head or not (stamp == LATEST or stamp.isdigit()):
        return None
    project, _, catalogue = head.partition("_")
    if not project or not catalogue:
        return None
    return project, catalogue, stamp


def slug_of(project):
    """A project's name as a catalogue filename's slug: lower case, every
    run of other than a–z and 0–9 one `-`, no `-` at either end."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(project).lower()).strip("-")
    return slug or "builds"


def entry_arch(entry):
    """The architecture a `hw-sim-mesh-<arch>` entry is for, or None for any
    other entry."""
    if entry.startswith(ENTRY_PREFIX) and len(entry) > len(ENTRY_PREFIX):
        return entry[len(ENTRY_PREFIX):]
    return None


def when_of(stamp):
    """A build stamp as `YYYY-MM-DD hh:mm`, or the stamp as it is."""
    if len(stamp) >= 12 and stamp.isdigit():
        return "%s-%s-%s %s:%s" % (stamp[:4], stamp[4:6], stamp[6:8], stamp[8:10], stamp[10:12])
    return stamp


def display_name(node, catalogue=None, slug=None):
    """What the page calls a device: its own `name`, else its project,
    catalogue and build time."""
    if node.get("name"):
        return str(node["name"])
    project = node.get("project") or slug or node.get("kind") or "device"
    return " ".join(str(p) for p in (project, catalogue or node.get("catalogue"),
                                     when_of(str(node.get("stamp", "")))) if p)


def stamp_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- node.yaml ---------------------------------------------------------------

def _inside(path, key, value):
    inner = os.path.normpath(str(value))
    if os.path.isabs(inner) or inner == ".." or inner.startswith("../"):
        raise DeviceError("%s: `%s` leaves the package" % (path, key))
    return inner


def _load_mapping(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except OSError as err:
        raise DeviceError("%s: %s" % (path, err.strerror)) from err
    except yaml.YAMLError as err:
        raise DeviceError("%s: %s" % (path, err)) from err
    if not isinstance(doc, dict):
        raise DeviceError("%s: not a mapping" % path)
    return doc


def _check_maps(path, doc):
    for key in ("tools", "env"):
        value = doc.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise DeviceError("%s: `%s` is a mapping" % (path, key))
        doc[key] = {str(k): str(v) for k, v in value.items()}


def read_node_yaml(directory):
    """`node.yaml` of an expanded package, checked: a dict with every required
    key, its stamp a string, and its `elf`, `fixed` and `tools` inside the
    package."""
    path = os.path.join(directory, NODE_YAML)
    doc = _load_mapping(path)
    for key in ("kind", "arch", "stamp", "elf"):
        if doc.get(key) in (None, ""):
            raise DeviceError("%s: no `%s`" % (path, key))
    doc["stamp"] = str(doc["stamp"])
    if not doc["stamp"].isdigit():
        raise DeviceError("%s: stamp %r is not all digits" % (path, doc["stamp"]))
    _check_maps(path, doc)
    for key in ("elf", "fixed"):
        if doc.get(key) not in (None, ""):
            doc[key] = _inside(path, key, doc[key])
    for tool, value in (doc.get("tools") or {}).items():
        doc["tools"][tool] = _inside(path, "tools.%s" % tool, value)
        if not os.path.isfile(os.path.join(directory, doc["tools"][tool])):
            raise DeviceError("%s: tool %s is not in the package" % (path, value))
    if not os.path.isfile(os.path.join(directory, doc["elf"])):
        raise DeviceError("%s: elf %s is not in the package" % (path, doc["elf"]))
    if doc.get("fixed") and not os.path.isdir(os.path.join(directory, doc["fixed"])):
        raise DeviceError("%s: fixed %s is not in the package" % (path, doc["fixed"]))
    return doc


def read_origin(directory):
    try:
        with open(os.path.join(directory, ORIGIN_YAML), encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _env(base, env):
    """An `env` mapping, a value starting `./` or `../` taken as a path from `base`."""
    return {k: (os.path.normpath(os.path.join(base, v)) if v.startswith(("./", "../")) else v)
            for k, v in (env or {}).items()}


def package_result(directory):
    """What `resolve` hands back for an expanded package directory, its name
    the directory's own."""
    node = read_node_yaml(directory)
    origin = read_origin(directory)
    parts = split_name(os.path.basename(directory))
    project = parts[0] if parts else slug_of(node.get("project") or node["kind"])
    catalogue = parts[1] if parts else (origin.get("catalogue") or node.get("catalogue"))
    return {
        "ref": os.path.basename(directory),
        "elf": os.path.join(directory, node["elf"]),
        "fixed": os.path.join(directory, node["fixed"]) if node.get("fixed") else None,
        "tools": {k: os.path.join(directory, v) for k, v in (node.get("tools") or {}).items()},
        "env": _env(directory, node.get("env")),
        "kind_type": str(node["kind"]),
        "stamp": node["stamp"],
        "arch": str(node["arch"]),
        "name": display_name(node, catalogue, project),
        "virtual_hardware": node.get("virtual_hardware"),
        "virtual_radio": node.get("virtual_radio"),
        "source": origin.get("url") or directory,
        "project": project,
        "catalogue": catalogue,
        "dir": directory,
        "node": node,
    }


# ---- compiled builds ------------------------------------------------------------

def local_dir(devices_dir=None):
    return os.path.join(devices_dir or DEVICES_DIR, LOCAL)


def local_names(devices_dir=None):
    """Every compiled build, by its `<project>_<catalogue>` file name."""
    base = local_dir(devices_dir)
    if not os.path.isdir(base):
        return []
    return sorted(e[:-5] for e in os.listdir(base)
                  if e.endswith(".yaml") and not e.startswith(".")
                  and split_name(e[:-5] + "_" + LATEST))


def local_result(key, devices_dir=None, arch=None):
    """A compiled build: `devices/local/<key>.yaml`, its paths from the file."""
    path = os.path.join(local_dir(devices_dir), key + ".yaml")
    doc = _load_mapping(path)
    for field in ("kind", "elf"):
        if doc.get(field) in (None, ""):
            raise DeviceError("%s: no `%s`" % (path, field))
    _check_maps(path, doc)
    base = os.path.dirname(path)
    project, catalogue, _ = split_name(key + "_" + LATEST)

    def where(value):
        return os.path.normpath(os.path.join(base, os.path.expanduser(str(value))))

    elf = where(doc["elf"])
    if not os.path.isfile(elf):
        # A compiled build is a tree someone builds themselves, so this is the
        # ordinary state of one nobody has built yet, on every row of every
        # listing until they do: it says that and no more. Where its executable
        # would be is in the file that names it.
        raise DeviceError(NOT_BUILT)
    fixed = where(doc["fixed"]) if doc.get("fixed") else None
    stamp = datetime.datetime.fromtimestamp(os.stat(elf).st_mtime, datetime.timezone.utc)
    stamp = stamp.strftime("%Y%m%d%H%M%S")
    return {
        "ref": "%s_%s" % (key, LATEST),
        "elf": elf,
        "fixed": fixed if fixed and os.path.isdir(fixed) else None,
        "tools": {k: where(v) for k, v in (doc.get("tools") or {}).items()},
        "env": _env(base, doc.get("env")),
        "kind_type": str(doc["kind"]),
        "stamp": stamp,
        "arch": str(doc.get("arch") or arch or machine_arch()),
        "name": display_name({"project": doc.get("project") or project, "stamp": stamp},
                             catalogue),
        "virtual_hardware": doc.get("virtual_hardware"),
        "virtual_radio": doc.get("virtual_radio"),
        "source": path,
        "project": project,
        "catalogue": catalogue,
        "dir": base,
        "node": doc,
    }


# ---- what is here ------------------------------------------------------------

def packages(where):
    """Every package directory in `where` (devices/latest or devices/saved),
    as {name: (project, catalogue, stamp, dir)}."""
    found = {}
    try:
        names = os.listdir(where)
    except FileNotFoundError:
        return found
    for name in names:
        parts = split_name(name)
        path = os.path.join(where, name)
        if name.startswith(".") or not parts or parts[2] == LATEST or not os.path.isdir(path):
            continue
        found[name] = parts + (path,)
    return found


def fetched(key, devices_dir=None):
    """The fetched builds of one `<project>_<catalogue>` in devices/latest,
    as [(stamp, dir)], newest first."""
    mine = [(p[2], p[3]) for n, p in packages(os.path.join(devices_dir or DEVICES_DIR, LATEST))
            .items() if "%s_%s" % (p[0], p[1]) == key]
    return sorted(mine, reverse=True)


def read_index(devices_dir=None):
    """The last survey: {`<project>_<catalogue>`: {project, catalogue, stamp,
    where, local, source, node}}, `node` what the build's node.yaml says of
    it (kind, virtual_hardware, virtual_radio, name) when it could be read."""
    path = os.path.join(devices_dir or DEVICES_DIR, LATEST, INDEX_YAML)
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {str(k): v for k, v in (doc.get("latest") or {}).items() if isinstance(v, dict)}


def write_index(index, devices_dir=None):
    where = os.path.join(devices_dir or DEVICES_DIR, LATEST)
    os.makedirs(where, exist_ok=True)
    tmp = os.path.join(where, PART_PREFIX + INDEX_YAML)
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump({"latest": index}, f, sort_keys=True)
    os.replace(tmp, os.path.join(where, INDEX_YAML))


def prune_latest(index, devices_dir=None):
    """Remove every fetched build a newer one of its project and catalogue
    has been seen for. Returns the removed names."""
    gone = []
    where = os.path.join(devices_dir or DEVICES_DIR, LATEST)
    by_key = {}
    for name, (project, catalogue, stamp, path) in packages(where).items():
        by_key.setdefault("%s_%s" % (project, catalogue), []).append((stamp, name, path))
    for key, mine in by_key.items():
        newest = max([s for s, _, _ in mine] + [str((index.get(key) or {}).get("stamp") or "")])
        for stamp, name, path in mine:
            if stamp < newest:
                shutil.rmtree(path, ignore_errors=True)
                gone.append(name)
    return sorted(gone)


def _row(result, **extra):
    return {"name": result["name"], "virtual_hardware": result["virtual_hardware"],
            "virtual_radio": result["virtual_radio"],
            "kind": result["kind_type"], "stamp": result["stamp"],
            "project": result["project"], "catalogue": result["catalogue"], **extra}


def listing(devices_dir=None, arch=None):
    """What the Devices tab shows: {latest: [row…], saved: [row…]}.

    A latest row is one project's newest in one catalogue, named
    `<project>_<catalogue>_latest`: {ref, project, catalogue, stamp, fetched,
    source (web, builds or compiled), name?, virtual_hardware?, virtual_radio?,
    kind?, error?}; what it plays, its radio and its kind come from its
    node.yaml, read as it was surveyed or from the fetched build. A saved row
    is {ref, project, catalogue, stamp, name, virtual_hardware, virtual_radio,
    kind, error?}.
    """
    devices_dir = devices_dir or DEVICES_DIR
    arch = arch or machine_arch()
    index = read_index(devices_dir)
    latest = {}
    for key, entry in index.items():
        node = entry.get("node") or {}
        latest[key] = {"ref": "%s_%s" % (key, LATEST), "project": entry.get("project"),
                       "catalogue": entry.get("catalogue"), "stamp": str(entry.get("stamp")),
                       "fetched": False, "source": "builds" if entry.get("local") else "web",
                       "kind": node.get("kind"), "virtual_hardware": node.get("virtual_hardware"),
                       "virtual_radio": node.get("virtual_radio"),
                       "name": display_name(dict(node, stamp=entry.get("stamp")),
                                            entry.get("catalogue"), entry.get("project"))}
    for key in list(latest) + [k for k in {"%s_%s" % (p[0], p[1]) for p in packages(
            os.path.join(devices_dir, LATEST)).values()} if k not in latest]:
        have = fetched(key, devices_dir)
        if not have:
            continue
        stamp, path = have[0]
        row = latest.setdefault(key, {"ref": "%s_%s" % (key, LATEST), "source": "web"})
        if str(row.get("stamp") or "") > stamp:
            continue
        try:
            row.update(_row(package_result(path)), fetched=True)
        except DeviceError as err:
            row.update(stamp=stamp, error=str(err))
    for key in local_names(devices_dir):
        project, catalogue, _ = split_name(key + "_" + LATEST)
        row = {"ref": "%s_%s" % (key, LATEST), "project": project, "catalogue": catalogue,
               "fetched": True, "source": COMPILED}
        try:
            row.update(_row(local_result(key, devices_dir, arch)))
        except DeviceError as err:
            row["error"] = str(err)
        latest[key] = row
    saved = []
    for name, (project, catalogue, stamp, path) in sorted(
            packages(os.path.join(devices_dir, SAVED)).items(),
            key=lambda kv: (kv[1][2], kv[0]), reverse=True):
        row = {"ref": name, "project": project, "catalogue": catalogue, "stamp": stamp}
        try:
            got = package_result(path)
            row.update(_row(got))
            if got["arch"] != arch:
                row["error"] = "built for %s, this machine is %s" % (got["arch"], arch)
        except DeviceError as err:
            row["error"] = str(err)
        saved.append(row)
    return {"latest": sorted(latest.values(), key=lambda r: (r.get("project") or "",
                                                             r.get("catalogue") or "")),
            "saved": saved}


# ---- resolve -----------------------------------------------------------------

def _looks_like_path(ref):
    return (os.sep in ref or ref.startswith(".") or ref.startswith("~")
            or ref.endswith(".zip"))


def resolve(ref, base_dir=None, devices_dir=None, arch=None):
    """A device name as the paths a kind needs, from what is here: a
    `_latest` build not fetched yet is refused (`ensure` fetches it).

    Returns {ref, elf, fixed, tools, env, kind_type, stamp, arch, name,
    virtual_hardware, virtual_radio, source, project, catalogue, dir, node}:
    `fixed` may be None, `source` is where the build came from (a URL, a
    catalogue directory, or the path given), `node` is the package's
    `node.yaml` (None for a workspace build.linux). A relative path is taken from `base_dir`.
    """
    arch = arch or machine_arch()
    devices_dir = devices_dir or DEVICES_DIR
    if not isinstance(ref, str) or ref.strip() == "":
        raise DeviceError("device: empty")
    ref = ref.strip()

    if _looks_like_path(ref):
        path = os.path.expanduser(ref)
        if not os.path.isabs(path):
            path = os.path.join(base_dir or os.getcwd(), path)
        return _resolve_path(os.path.normpath(path), arch)

    parts = split_name(ref)
    if parts is None:
        raise DeviceError("device %s: a device is <project>_<catalogue>_latest, "
                          "<project>_<catalogue>_<stamp> or a path" % ref)
    project, catalogue, stamp = parts
    key = "%s_%s" % (project, catalogue)
    if stamp == LATEST:
        if key in local_names(devices_dir):
            return local_result(key, devices_dir, arch)
        have = fetched(key, devices_dir)
        if not have:
            raise DeviceError("device %s: not fetched yet (it is fetched when a simulation "
                              "uses it, or by sim-mesh devices fetch %s)" % (ref, ref))
        return _package_here(have[0][1], ref, arch)
    for where in (SAVED, LATEST):
        path = os.path.join(devices_dir, where, ref)
        if os.path.isfile(os.path.join(path, NODE_YAML)):
            return _package_here(path, ref, arch)
    raise DeviceError("device %s: not saved (the Devices tab lists what there is)" % ref)


def _package_here(path, ref, arch):
    """A package named outright, refused unless it runs here."""
    got = package_result(path)
    if got["arch"] != arch:
        raise DeviceError("device %s: built for %s, this machine is %s"
                          % (ref, got["arch"], arch))
    return got


def _resolve_path(path, arch):
    if os.path.isfile(path):
        raise DeviceError("device %s: a file; name a package directory or a "
                          "build.linux directory (Import on the Devices tab takes a zip)" % path)
    if not os.path.isdir(path):
        raise DeviceError("device %s: no such directory" % path)
    if os.path.isfile(os.path.join(path, NODE_YAML)):
        return _package_here(path, path, arch)
    elf = os.path.join(path, WORKSPACE_ELF)
    if os.path.isfile(elf):
        fixed = os.path.join(path, WORKSPACE_FIXED)
        when = datetime.datetime.fromtimestamp(os.stat(elf).st_mtime, datetime.timezone.utc)
        return {
            "ref": path,
            "elf": elf,
            "fixed": fixed if os.path.isdir(fixed) else None,
            "tools": {},
            "env": {},
            "kind_type": WORKSPACE_KIND,
            "stamp": when.strftime("%Y%m%d%H%M%S"),
            "arch": arch,
            "name": "workspace %s" % path,
            "virtual_hardware": None,
            "virtual_radio": None,
            "source": path,
            "project": None,
            "catalogue": None,
            "dir": path,
            "node": None,
        }
    raise DeviceError("device %s: neither a device package (no %s) nor a workspace "
                      "build (no %s)" % (path, NODE_YAML, WORKSPACE_ELF))


# ---- catalogues --------------------------------------------------------------

class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            got = {k: (v or "") for k, v in attrs}
            if got.get("href"):
                self.links.append(got)


def parse_listing(text):
    """A catalogue listing's links, as attribute dicts (`href` plus whatever
    `data-*` facts the row carries)."""
    parser = _Links()
    parser.feed(text)
    parser.close()
    return parser.links


def newest_packages(links, arch):
    """The newest device package for `arch` per project of a listing, as
    {project: {href, name, slug, stamp, arch}}. Other images are left out."""
    newest = {}
    for attrs in links:
        href = attrs["href"]
        name = urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1])
        parts = split_image_name(name)
        if not parts or entry_arch(parts[1]) != arch:
            continue
        slug, _, stamp = parts
        if slug not in newest or stamp > newest[slug]["stamp"]:
            newest[slug] = {"href": href, "name": name, "slug": slug, "stamp": stamp,
                            "arch": arch}
    return newest


def catalogue_names(links):
    """The catalogues a site's index lists: its links to directories."""
    out = []
    for attrs in links:
        href = attrs["href"]
        if "://" in href or href.startswith(("/", ".", "?", "#")) or not href.endswith("/"):
            continue
        name = urllib.parse.unquote(href.rstrip("/"))
        if name and "/" not in name and name not in out:
            out.append(name)
    return out


def builds_sources(builds_dir=None):
    """The catalogue directories in `builds/` beside sim-mesh: each one holding
    an `index.html`."""
    base = builds_dir or BUILDS_DIR
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [os.path.join(base, n) for n in names
            if not n.startswith(".") and os.path.isfile(os.path.join(base, n, "index.html"))]


class Source:
    """Where one catalogue is: `location` is a URL ending in `/`, or a local
    directory; `name` is the catalogue's name."""

    def __init__(self, spec, base=DEFAULT_BASE):
        self.spec = spec
        if "://" in spec:
            self.local = False
            self.location = spec if spec.endswith("/") else spec + "/"
        elif os.path.isdir(spec) and os.path.isfile(os.path.join(spec, "index.html")):
            self.local = True
            self.location = os.path.abspath(spec)
        elif os.sep not in spec and not spec.startswith("."):
            self.local = False
            self.location = urllib.parse.urljoin(base if base.endswith("/") else base + "/",
                                                 spec + "/")
        else:
            raise DeviceError("%s: not a URL, a catalogue name, or a directory "
                              "holding an index.html" % spec)
        last = self.location.rstrip("/").rsplit("/", 1)[-1]
        if not self.local:
            last = urllib.parse.unquote(last)
        if last.startswith("catalogue-"):
            last = last[len("catalogue-"):]
        if not last or last.startswith(".") or os.sep in last:
            raise DeviceError("%s: cannot tell the catalogue's name" % spec)
        if last == IMPORTED:
            if not self.local:
                raise DeviceError("%s: a catalogue cannot be called %s" % (spec, last))
            last = "builds-" + last
        self.name = last

    def where(self, href):
        if self.local:
            if "://" in href or os.path.isabs(href):
                raise DeviceError("%s: link %s leaves the directory" % (self.spec, href))
            return os.path.join(self.location, urllib.parse.unquote(href))
        return urllib.parse.urljoin(self.location, href)

    async def listing(self, session):
        if self.local:
            path = os.path.join(self.location, "index.html")
            return await asyncio.to_thread(_read_text, path)
        async with session.get(self.location + "index.html") as resp:
            if resp.status != 200:
                raise DeviceError("%sindex.html: HTTP %d" % (self.location, resp.status))
            return await resp.text()


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


async def web_catalogues(session, base=DEFAULT_BASE, say=print):
    """The catalogues the web's index lists, or WEB_FALLBACK when it cannot
    be read."""
    import aiohttp

    url = (base if base.endswith("/") else base + "/") + "index.html"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise DeviceError("%s: HTTP %d" % (url, resp.status))
            names = catalogue_names(parse_listing(await resp.text()))
    except (DeviceError, aiohttp.ClientError, asyncio.TimeoutError) as err:
        say("%s: %s; trying %s" % (url, str(err) or type(err).__name__, ", ".join(WEB_FALLBACK)))
        return list(WEB_FALLBACK)
    return names


# ---- expanding ---------------------------------------------------------------

def expand(zip_path, dest, origin, arch):
    """Unzip a package to `dest` (named `<project>_<catalogue>_<stamp>`),
    whole or not at all.

    The members go under a `.part-` sibling first; `node.yaml` is read and its
    architecture and stamp checked against this machine and the name before
    the rename, so a package directory that exists is one that was checked.
    Members that would land outside the package are refused. Permission bits
    the zip carries are kept, and the ELF and the tools are made executable
    either way.
    """
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    part = os.path.join(parent, PART_PREFIX + os.path.basename(dest))
    shutil.rmtree(part, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                inner = os.path.normpath(info.filename)
                if os.path.isabs(info.filename) or inner == ".." or inner.startswith("../"):
                    raise DeviceError("%s: member %s leaves the package"
                                      % (zip_path, info.filename))
                target = os.path.join(part, inner)
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    os.chmod(target, mode)
        node = read_node_yaml(part)
        parts = split_name(os.path.basename(dest))
        if str(node["arch"]) != arch:
            raise DeviceError("%s: built for %s, this machine is %s"
                              % (os.path.basename(zip_path), node["arch"], arch))
        if parts and node["stamp"] != parts[2]:
            raise DeviceError("%s: node.yaml says stamp %s, the filename %s"
                              % (os.path.basename(zip_path), node["stamp"], parts[2]))
        for inner in [node["elf"]] + list((node.get("tools") or {}).values()):
            path = os.path.join(part, inner)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        with open(os.path.join(part, ORIGIN_YAML), "w", encoding="utf-8") as f:
            yaml.safe_dump(origin, f, sort_keys=False)
        if not os.path.isfile(os.path.join(dest, NODE_YAML)):
            shutil.rmtree(dest, ignore_errors=True)
            os.rename(part, dest)
    except zipfile.BadZipFile as err:
        raise DeviceError("%s: %s" % (os.path.basename(zip_path), err)) from err
    finally:
        shutil.rmtree(part, ignore_errors=True)
    return dest


def import_zip(zip_path, filename, devices_dir=None, arch=None):
    """A device zip someone handed over, whatever it is called, into the
    saved builds as `<project>_imported_<stamp>`, the project from its
    `node.yaml`'s `project` or else its `kind`. It must be for this
    machine's architecture, and one saved under that name already is kept,
    not replaced. Returns the package's `resolve` result."""
    arch = arch or machine_arch()
    shown = os.path.basename(filename or "") or "the upload"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            node = yaml.safe_load(zf.read(NODE_YAML))
    except KeyError as err:
        raise DeviceError("%s holds no %s at its top" % (shown, NODE_YAML)) from err
    except zipfile.BadZipFile as err:
        raise DeviceError("%s: %s" % (shown, err)) from err
    except yaml.YAMLError as err:
        raise DeviceError("%s: %s: %s" % (shown, NODE_YAML, err)) from err
    if not isinstance(node, dict) or any(node.get(k) in (None, "") for k in ("kind", "arch", "stamp")):
        raise DeviceError("%s: %s needs kind, arch and stamp" % (shown, NODE_YAML))
    if str(node["arch"]) != arch:
        raise DeviceError("%s is built for %s, and this machine is %s" % (shown, node["arch"], arch))
    name = "%s_%s_%s" % (slug_of(node.get("project") or node["kind"]), IMPORTED, node["stamp"])
    dest = os.path.join(devices_dir or DEVICES_DIR, SAVED, name)
    if os.path.isfile(os.path.join(dest, NODE_YAML)):
        raise DeviceError("%s has been imported already, as %s" % (shown, name))
    expand(zip_path, dest, {"catalogue": IMPORTED, "url": shown, "fetched": now_utc()}, arch)
    return package_result(dest)


# ---- the survey ----------------------------------------------------------------

async def survey(sources=None, devices_dir=None, arch=None, say=print, base=DEFAULT_BASE,
                 web=True):
    """Read the catalogues' listings, fetching nothing, and keep the newest
    build per project and catalogue in devices/latest/index.yaml; then
    remove every fetched build a newer one has been seen for.

    `sources` are catalogue names, URLs or directories; by default the web's
    catalogues (when `web`) and then every catalogue in builds/. A source
    that cannot be read is said and the others still run. Returns (index,
    changed): whether the survey saw anything new or removed anything.
    """
    import aiohttp

    arch = arch or machine_arch()
    devices_dir = devices_dir or DEVICES_DIR
    index = read_index(devices_dir)
    before = dict(index)
    timeout = aiohttp.ClientTimeout(total=SURVEY_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if sources is None:
            sources = (await web_catalogues(session, base, say) if web else []) \
                + builds_sources()
        for spec in sources:
            try:
                source = Source(spec, base)
                newest = newest_packages(parse_listing(await source.listing(session)), arch)
            except (DeviceError, OSError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                say("%s: %s" % (spec, str(err) or type(err).__name__))
                continue
            for slug, pkg in sorted(newest.items()):
                key = "%s_%s" % (slug, source.name)
                held = index.get(key) or {}
                if str(held.get("stamp") or "") > pkg["stamp"]:
                    continue
                if str(held.get("stamp") or "") == pkg["stamp"]:
                    if "node" in held:
                        continue
                    entry = dict(held)      # surveyed before its node.yaml was read
                else:
                    entry = {"project": slug, "catalogue": source.name, "stamp": pkg["stamp"],
                             "where": source.where(pkg["href"]), "local": source.local,
                             "source": source.location}
                    say("%s: %s_%s is %s" % (source.name, key, LATEST, pkg["stamp"]))
                entry["node"] = await peek_node(session, entry["where"], entry["local"], say)
                index[key] = entry
    changed = index != before
    if changed:
        await asyncio.to_thread(write_index, index, devices_dir)
    gone = await asyncio.to_thread(prune_latest, index, devices_dir)
    if gone:
        say("removed %s" % ", ".join(gone))
    return index, changed or bool(gone)


NODE_FACTS = ("kind", "virtual_hardware", "virtual_radio", "name", "project", "arch")


def _facts(text):
    """What a listing says of a build from its node.yaml's text, or {}."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return {k: str(doc[k]) for k in NODE_FACTS if isinstance(doc, dict) and doc.get(k)}


def _peek_local(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return _facts(zf.read(NODE_YAML))
    except (OSError, KeyError, zipfile.BadZipFile):
        return {}


async def _range(session, url, spec):
    async with session.get(url, headers={"Range": "bytes=" + spec}) as resp:
        if resp.status != 206:
            raise DeviceError("%s: no range requests (HTTP %d)" % (url, resp.status))
        return await resp.read()


async def peek_node(session, where, local, say=print):
    """What a build's node.yaml says of it (kind, virtual_hardware,
    virtual_radio, name, arch), read from its zip without fetching it: a local
    zip opened, a web one read by range requests, its end for the central directory and then the one
    member. {} when it cannot be read, which leaves the listing blank there
    until the build is fetched."""
    import struct
    import zlib

    import aiohttp

    if local:
        return await asyncio.to_thread(_peek_local, where)
    try:
        tail = await _range(session, where, "-%d" % PEEK_TAIL)
        end = tail.rfind(b"PK\x05\x06")
        if end < 0:
            raise DeviceError("%s: no zip directory in its last %d bytes" % (where, PEEK_TAIL))
        size, offset = struct.unpack_from("<II", tail, end + 12)
        start = end - size
        directory = tail[start:end] if start >= 0 else \
            await _range(session, where, "%d-%d" % (offset, offset + size - 1))
        at = 0
        while at + 46 <= len(directory) and directory[at:at + 4] == b"PK\x01\x02":
            method = struct.unpack_from("<H", directory, at + 10)[0]
            csize = struct.unpack_from("<I", directory, at + 20)[0]
            nlen, xlen, clen = struct.unpack_from("<HHH", directory, at + 28)
            local_at = struct.unpack_from("<I", directory, at + 42)[0]
            name = directory[at + 46:at + 46 + nlen].decode("utf-8", "replace")
            at += 46 + nlen + xlen + clen
            if name != NODE_YAML:
                continue
            head = await _range(session, where, "%d-%d" % (local_at, local_at + 29))
            lnlen, lxlen = struct.unpack_from("<HH", head, 26)
            begin = local_at + 30 + lnlen + lxlen
            data = await _range(session, where, "%d-%d" % (begin, begin + max(csize, 1) - 1))
            if method == 8:
                data = zlib.decompressobj(-15).decompress(data)
            elif method != 0:
                return {}
            return _facts(data)
        return {}
    except (DeviceError, struct.error, zlib.error, aiohttp.ClientError, asyncio.TimeoutError) as err:
        say("%s: its node.yaml could not be read before fetching it: %s"
            % (where, str(err) or type(err).__name__))
        return {}


async def _download(session, url, path):
    size = 0
    async with session.get(url) as resp:
        if resp.status != 200:
            raise DeviceError("%s: HTTP %d" % (url, resp.status))
        with open(path, "wb") as f:
            async for chunk in resp.content.iter_chunked(CHUNK):
                f.write(chunk)
                size += len(chunk)
    return size


async def fetch(key, devices_dir=None, arch=None, say=print):
    """The newest surveyed build of one `<project>_<catalogue>`, fetched into
    devices/latest unless it is there: its directory."""
    import aiohttp

    arch = arch or machine_arch()
    devices_dir = devices_dir or DEVICES_DIR
    entry = read_index(devices_dir).get(key)
    if entry is None:
        raise DeviceError("device %s_%s: no catalogue has one for %s"
                          % (key, LATEST, arch))
    dest = os.path.join(devices_dir, LATEST, "%s_%s" % (key, entry["stamp"]))
    if os.path.isfile(os.path.join(dest, NODE_YAML)):
        return dest
    where = str(entry["where"])
    origin = {"catalogue": entry.get("catalogue"), "url": where, "fetched": now_utc()}
    tmp_zip = None
    try:
        if entry.get("local"):
            zip_path = where
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp_zip = os.path.join(os.path.dirname(dest), PART_PREFIX + os.path.basename(dest)
                                   + ".zip")
            timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await _download(session, where, tmp_zip)
            zip_path = tmp_zip
        await asyncio.to_thread(expand, zip_path, dest, origin, arch)
    except (OSError, aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise DeviceError("device %s_%s: fetching %s: %s"
                          % (key, LATEST, where, str(err) or type(err).__name__)) from err
    finally:
        if tmp_zip:
            try:
                os.unlink(tmp_zip)
            except FileNotFoundError:
                pass
    say("fetched %s" % os.path.basename(dest))
    await asyncio.to_thread(prune_latest, read_index(devices_dir), devices_dir)
    return dest


async def ensure(ref, base_dir=None, devices_dir=None, arch=None, say=print, look=True):
    """A device name resolved, a `_latest` one fetched first: looked for
    afresh in its catalogue when `look` (a survey of that catalogue alone,
    whose failure leaves the last survey standing), then downloaded unless
    it is here."""
    devices_dir = devices_dir or DEVICES_DIR
    parts = split_name(str(ref).strip()) if isinstance(ref, str) else None
    if parts and parts[2] == LATEST:
        key = "%s_%s" % parts[:2]
        if key not in local_names(devices_dir):
            if look:
                await survey(None, devices_dir, arch, lambda line: None)
            if key in read_index(devices_dir):
                await fetch(key, devices_dir, arch, say)
    return resolve(ref, base_dir, devices_dir, arch)


# ---- saving ------------------------------------------------------------------

def _copy_package(got, dest):
    """A resolved build copied to `dest` as a package directory of its own,
    whole or not at all: a package's tree as it is, a compiled build's
    executable, `/fixed` tree and tools gathered, with a node.yaml saying so."""
    part = os.path.join(os.path.dirname(dest), PART_PREFIX + os.path.basename(dest))
    shutil.rmtree(part, ignore_errors=True)
    try:
        if got.get("node") is not None and os.path.isfile(os.path.join(got["dir"], NODE_YAML)):
            shutil.copytree(got["dir"], part, symlinks=True)
        else:
            os.makedirs(part)
            node = {"kind": got["kind_type"], "arch": got["arch"], "stamp": got["stamp"],
                    "elf": os.path.basename(got["elf"]), "project": got.get("project"),
                    "catalogue": got.get("catalogue")}
            for fact in ("virtual_hardware", "virtual_radio"):
                if got.get(fact):
                    node[fact] = got[fact]
            shutil.copy2(got["elf"], os.path.join(part, node["elf"]))
            if got.get("fixed"):
                shutil.copytree(got["fixed"], os.path.join(part, "fixed"), symlinks=True)
                node["fixed"] = "fixed"
            if got.get("tools"):
                os.makedirs(os.path.join(part, "tools"))
                node["tools"] = {}
                for tool, path in got["tools"].items():
                    shutil.copy2(path, os.path.join(part, "tools", os.path.basename(path)))
                    node["tools"][tool] = "tools/" + os.path.basename(path)
            if got.get("env"):
                node["env"] = dict(got["env"])
            with open(os.path.join(part, NODE_YAML), "w", encoding="utf-8") as f:
                yaml.safe_dump(node, f, sort_keys=False)
        with open(os.path.join(part, ORIGIN_YAML), "w", encoding="utf-8") as f:
            yaml.safe_dump({"catalogue": got.get("catalogue"), "url": got.get("source"),
                            "saved": now_utc()}, f, sort_keys=False)
        read_node_yaml(part)
        os.rename(part, dest)
    finally:
        shutil.rmtree(part, ignore_errors=True)
    return dest


async def save(ref, devices_dir=None, arch=None, say=print):
    """A `_latest` build kept as a saved one, fetched first when it has not
    been: its `resolve` result under its saved name."""
    devices_dir = devices_dir or DEVICES_DIR
    parts = split_name(str(ref))
    if not parts or parts[2] != LATEST:
        raise DeviceError("save %s: only a <project>_<catalogue>_latest build is saved" % ref)
    got = await ensure(ref, devices_dir=devices_dir, arch=arch, say=say, look=False)
    name = "%s_%s_%s" % (parts[0], parts[1], got["stamp"])
    dest = os.path.join(devices_dir, SAVED, name)
    if os.path.isfile(os.path.join(dest, NODE_YAML)):
        raise DeviceError("%s is saved already" % name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    await asyncio.to_thread(_copy_package, got, dest)
    say("saved %s" % name)
    return package_result(dest)


def delete_saved(ref, devices_dir=None):
    """Remove one saved build."""
    parts = split_name(str(ref))
    path = os.path.join(devices_dir or DEVICES_DIR, SAVED, str(ref))
    if not parts or parts[2] == LATEST or not os.path.isdir(path):
        raise DeviceError("no saved build called %s" % ref)
    shutil.rmtree(path)


# ---- the CLI -----------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="sim-mesh devices",
                                 description="the station builds a node can run")
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("refresh", help="survey the catalogues for their newest builds "
                       "(default: the web's, then every catalogue in %s); fetches nothing"
                       % BUILDS_DIR)
    p.add_argument("sources", nargs="*", metavar="SOURCE",
                   help="a catalogue name, a catalogue URL, or a local catalogue directory")
    sub.add_parser("list", help="the latest and the saved builds")
    p = sub.add_parser("fetch", help="download a <project>_<catalogue>_latest build now")
    p.add_argument("device")
    p = sub.add_parser("save", help="keep a <project>_<catalogue>_latest build as a saved one")
    p.add_argument("device")
    p = sub.add_parser("delete", help="remove a saved build")
    p.add_argument("device")
    p = sub.add_parser("import", help="a device zip into the saved builds")
    p.add_argument("zip")
    p = sub.add_parser("resolve", help="what a device name runs, as JSON")
    p.add_argument("device")
    p.add_argument("--base-dir", default=None,
                   help="the directory relative paths are taken from")
    args = ap.parse_args(argv)

    try:
        if args.verb == "refresh":
            asyncio.run(survey(args.sources or None))
            return 0
        if args.verb == "list":
            shown = listing()
            for row in shown["latest"]:
                state = "fetched" if row.get("fetched") else "not fetched"
                mark = "   ! %s" % row["error"] if row.get("error") else ""
                print("%-40s %-15s %-12s %s%s" % (row["ref"], row.get("stamp") or "", state,
                                                  row.get("name") or "", mark))
            for row in shown["saved"]:
                mark = "   ! %s" % row["error"] if row.get("error") else ""
                print("%-40s %-15s %-12s %s%s" % (row["ref"], row["stamp"], "saved",
                                                  row.get("name") or "", mark))
            return 0
        if args.verb == "import":
            got = import_zip(args.zip, os.path.basename(args.zip))
            print("imported %s as %s" % (got["name"], got["ref"]))
            return 0
        if args.verb == "fetch":
            got = asyncio.run(ensure(args.device))
        elif args.verb == "save":
            got = asyncio.run(save(args.device))
        elif args.verb == "delete":
            delete_saved(args.device)
            return 0
        else:
            got = resolve(args.device, args.base_dir)
    except DeviceError as err:
        print("sim-mesh devices: %s" % err, file=sys.stderr)
        return 1
    print(json.dumps(got, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
