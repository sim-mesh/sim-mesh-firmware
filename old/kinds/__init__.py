"""Station kinds: what the testbed must know about one kind of firmware.

A station is a process that keeps the station contract (STATION.md): it reads
its identity, directory, address and ether from `SIM_MESH_*` in its
environment, runs in its directory, and treats stdin/stdout as its console.
Everything beyond that differs by firmware — how to tell it is up, how to
type a line at it, what role it plays in the mesh, whether it has a web UI,
how an intent is said in its language — and that is a kind.

A node's firmware, a device (devices.py) a script names with `firmware()`,
says which kind runs it: `node.yaml`'s `kind` names the class here by its
`type_name`. One `Kind` object stands for one resolved device, its
executable, `/fixed` tree, tools and environment; nodes on the same firmware
share it. `ensure_builds` settles every firmware a run uses before a station
of it starts, and a simulation may be started with a build of its own, which
then replaces every firmware of that build's kind.

**Lines and intents.** A line is text typed at a station, in its kind's own
language: the Reticulous CLI, `rncfg` arguments for `sergeyculum`. The same
text typed at another firmware means something else or nothing, so a line
goes only to stations of one kind. An **intent** (`Kind.lines(verb, …)`) is
what a script means, which each kind turns into its own lines: `name`,
`role`, `radio` (its settings), `radio_up` (the radio started, nothing for
a kind whose radio needs no start), `tx_power`, `announce`, `message`,
`path`, `peer_tcp`. A kind that has no way to say one raises CommandError naming
itself and the verb.

Lines keep `{name}`, `{id}`, `{addr}` and `{addr:<node>}` macros until a
station is given them (`expand`).
"""

import asyncio
import os
import re

import devices as devices_module
import stations as stations_module

HERE = os.path.dirname(os.path.abspath(__file__))
# The time shim every station of a virtual-time run is started with.
SHIM = os.path.normpath(os.path.join(HERE, "..", "..", "radio", "build", "libsimclock.so"))

# What a station does for the mesh, as its kind reports it: forwards for
# others (transport, router, repeater) or only for itself (client).
ROLES = ("transport", "router", "repeater", "client")
INTENTS = ("name", "role", "radio", "radio_up", "tx_power", "announce", "message", "path",
           "peer_tcp")
MACRO_RE = re.compile(r"\{([a-z_]+)(?::([A-Za-z0-9_.-]+))?\}")


class CommandError(Exception):
    """A station could not be asked, or did not answer in time, or its kind
    has no way to say what was meant."""


# ---- macros --------------------------------------------------------------

def expand(line, name, node_id, ids=None, extra=None):
    """Fill `{name}`, `{id}` and `{addr}` in one line for one station, and
    `{addr:<node>}` with another node's address; `extra` is more of them by
    name (`{max_dbm}`, the node's maximum power).

    `ids` maps the nodeset's node names to their station ids; the address a
    station binds comes from the simulation's network, which the front picks
    per simulation, so a line that means "the internet gateway" must name the
    node rather than write down the address it had in one run.

    A macro this does not define, or a node `ids` does not have, is left
    exactly as written: a line is somebody's text and may legitimately
    contain braces, and silently emptying something that only looked like a
    macro is worse than passing it through for the station to complain about.
    """
    values = {"name": name, "id": str(node_id), "addr": stations_module.bind_addr(node_id)}
    values.update({k: str(v) for k, v in (extra or {}).items()})

    def fill(m):
        key, node = m.group(1), m.group(2)
        if node is None:
            return values.get(key, m.group(0))
        if key == "addr" and node in (ids or {}):
            return stations_module.bind_addr(ids[node])
        return m.group(0)
    return MACRO_RE.sub(fill, line)


def expand_all(lines, name, node_id, ids=None, extra=None):
    """The lines a station is actually given: expanded, minus blanks and comments."""
    out = []
    for line in lines:
        line = expand(str(line), name, node_id, ids, extra).strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


# ---- the kind ------------------------------------------------------------

class Kind:
    """One kind of station on one device. Subclasses say how to talk to it."""

    type_name = None                # the `kind` a device's node.yaml names this class by

    def __init__(self, device):
        self.device = dict(device or {})
        self.name = self.type_name
        self.elf = self.device.get("elf")
        self.fixed = self.device.get("fixed")
        self.tools = dict(self.device.get("tools") or {})
        self.extra_env = {str(k): str(v) for k, v in (self.device.get("env") or {}).items()}

    @property
    def label(self):
        """What the page calls the device: its name, and the hardware it plays."""
        name = self.device.get("name") or self.device.get("ref") or self.name
        hardware = self.device.get("virtual_hardware")
        return "%s (virtual %s)" % (name, hardware) if hardware else name

    # ---- the contract ----------------------------------------------------

    def env(self, station):
        """The station's environment: the contract, then the device's own."""
        env = {"SIM_MESH_NODE_ID": str(station.node_id),
               "SIM_MESH_NODE_DIR": station.dir,
               "SIM_MESH_BIND_ADDR": station.addr,
               "SIM_MESH_ETHER": station.ether_addr}
        if station.clock is not None:
            env.update(SIM_MESH_TIME="virtual",
                       SIM_MESH_EPOCH_US=str(station.clock.epoch),
                       SIM_MESH_SEED=str(station.clock.seed),
                       LD_PRELOAD=SHIM)
        if getattr(station, "board", None):
            env["SIM_MESH_BOARD"] = station.board
        env.update(self.extra_env)
        return env

    def sids(self, station):
        """The ether ids of the station's processes that join the run: its
        own, and those of processes it starts that join as stations without
        a radio."""
        return (station.node_id,)

    def console_sid(self, station):
        """The ether id of the process that reads the station's console."""
        return station.node_id

    async def pause(self, station, seconds):
        """Wait on the run's clock: the ether's T in a virtual-time run, so a
        poll between two questions costs the station the same time in either
        mode."""
        if station.clock is not None:
            await station.clock.sleep(seconds)
        else:
            await asyncio.sleep(seconds)

    async def wait_up(self, station, timeout):
        """True once the station answers the way this kind answers."""
        raise NotImplementedError

    async def run(self, station, line, timeout=None):
        """One line typed at the station; what it said back."""
        raise NotImplementedError

    async def setup_line(self, station, line):
        """One line of a station's setup, and whatever waiting it takes for
        what it asked to have landed; what the station said back."""
        return await self.run(station, line)

    async def setup(self, station, lines):
        """Lines for a station that has never been set up, then a flush.

        Every line is tried; the ones that could not be asked are reported
        together at the end.
        """
        failed = []
        for line in lines:
            if not line.strip() or line.strip().startswith("#"):
                continue
            try:
                await self.setup_line(station, line)
            except CommandError as err:
                failed.append("%s: %s" % (line, err))
        await self.flush(station)
        if failed:
            raise CommandError("; ".join(failed))

    async def flush(self, station):
        """Make what the station has been told durable; best effort."""

    async def role(self, station):
        """What the station does for the mesh, one of ROLES, read live.

        None when this kind cannot say; CommandError when the station could
        not be asked this time, which leaves the last answer standing.
        """
        return None

    async def address(self, station):
        """The station's LXMF delivery address, 32 hex digits, or None while
        it has none yet; CommandError when this kind cannot say."""
        raise CommandError("a %s station cannot say its address" % self.name)

    def web_port(self):
        """The port its web UI answers on, or None when it has none."""
        return None

    def configured(self, station):
        """True when this station's directory has been set up already."""
        raise NotImplementedError

    # ---- intents ---------------------------------------------------------

    def lines(self, verb, **args):
        """An intent in this kind's own lines. A subclass handles the verbs
        it can say and hands the rest here, which refuses them."""
        if verb not in INTENTS:
            raise CommandError("no such intent %r (there are %s)" % (verb, ", ".join(INTENTS)))
        raise CommandError("a %s station has no way to %s" % (self.name, verb.replace("_", " ")))

    # True for a kind whose station forgets its role when it restarts: the
    # role intents of its first-boot rules are then said at every boot.
    role_volatile = False

    def describe(self):
        return "%s: %s (%s) %s" % (self.device.get("ref"), self.label, self.type_name,
                                   self.elf or "no binary")


def kind_types():
    """Every kind class by its type name."""
    from . import microreticulum, reticulous, sergeyculum, standard_reticulum
    return {cls.type_name: cls for cls in (reticulous.Reticulous, sergeyculum.Sergeyculum,
                                           microreticulum.Microreticulum,
                                           standard_reticulum.StandardReticulum)}


BUILD_KEYS = ("ref", "elf", "fixed", "tools", "env", "kind_type", "stamp", "arch", "name",
              "virtual_hardware", "virtual_radio", "source", "project", "catalogue", "asked")


async def ensure_builds(refs, override=None, say=None):
    """Where each firmware a run names runs from: {firmware: resolved},
    which a run keeps in run.yaml as its `builds`. A `_latest` one is looked
    for afresh and fetched when it is not here (devices.ensure).

    `override` is a simulation's own build (a device name or a path), used in
    place of every firmware whose kind is the one that build is of. A
    firmware that cannot be resolved raises CommandError naming it and what
    would supply it.
    """
    say = say or (lambda line: None)
    forced = None
    try:
        if override not in (None, ""):
            forced = await devices_module.ensure(str(override), say=say)
        out = {}
        for ref in sorted(set(refs)):
            try:
                got = await devices_module.ensure(ref, say=say)
            except devices_module.DeviceError:
                if forced is None:
                    raise
                got = None
            if forced is not None and (got is None or got["kind_type"] == forced["kind_type"]):
                got = dict(forced, asked=str(override))
            out[ref] = {k: got.get(k) for k in BUILD_KEYS}
    except devices_module.DeviceError as err:
        raise CommandError(str(err)) from err
    return out


def build_present(build):
    """Whether a kept build still has its executable: a `_latest` one is
    removed once a newer one is seen."""
    return bool(build) and bool(build.get("elf")) and os.path.isfile(build["elf"])


def make_kinds(builds):
    """A `Kind` per firmware, from what `ensure_builds` said (a run's `builds`)."""
    types = kind_types()
    kinds = {}
    for ref, device in (builds or {}).items():
        cls = types.get(device.get("kind_type"))
        if cls is None:
            raise CommandError("device %s is of kind %r, which the testbed does not know "
                               "(it knows %s)" % (ref, device.get("kind_type"),
                                                  ", ".join(sorted(types))))
        kinds[ref] = cls(device)
    return kinds


async def run_tool(argv, timeout):
    """Run a helper program to completion: its exit code and all it printed."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as err:
        raise CommandError("%s: %s" % (argv[0], err)) from err
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as err:
        proc.kill()
        await proc.wait()
        raise CommandError("%s gave no answer in %.0fs" % (os.path.basename(argv[0]),
                                                          timeout)) from err
    return proc.returncode, out.decode("utf-8", "replace")
