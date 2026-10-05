"""Sergeyculum, the Rust Reticulum stack at git.emcomm.cc/berlinmesh/reticulum,
as its Linux station `fw/sim-mesh`.

Its device names its configuration tool among its tools, `tools: { rncfg:
<path> }`; `rncfg` on PATH when it names none.

A station of this kind has no text console and no web UI. Its host door is
RNode KISS on a pty it makes itself and links as `kiss` in its directory; the
console (stdout) carries its log lines. It is configured with `rncfg`, one
invocation per line, the way a person configures a Sergeyculum board over USB.
Its key-value writes are synchronous, so there is nothing to flush.

A line is `rncfg` without the program and the port: `<verb> <args…>`, and
the port goes in after the verb, which is where `rncfg` has it (`rncfg <verb>
<PORT> …`). So `set --freq-hz 869525000 --sf 8` runs `rncfg set <dir>/kiss
--freq-hz 869525000 --sf 8`, and `name set {name}` names the station.

Its intents, in `rncfg`'s words:

    name        name set {name}
    radio       set --freq-hz --sf --bw-hz --cr --txpower-dbm, for the figures
                the radio gives; sync word and preamble are the firmware's own
                (0x12, 18) and not settable
    tx_power    set --txpower-dbm <dBm>
    role       transport on (transport) or off (client); the firmware keeps
                this in RAM only, so it is said again at every boot, not only
                at setup (`role_volatile`)
    announce    announce now
    message     send <dest> <text>

and its address is the `lxmf.delivery` line of `rncfg addr`.

What `rncfg` prints, read from tools/rncfg/src/main.rs:

    detect:           `DETECT   : ok (0x..)` when the station answers
    transport [get]:  `transport: on` | `transport: off`
    addr:             `lxmf.delivery     : <32 hex digits>` among others
    a failure:        `error: …` on stderr, exit status 1
"""

import asyncio
import os
import re
import shlex
import shutil

from . import CommandError, Kind, run_tool

DETECT_OK = re.compile(r"^DETECT\s*:\s*ok", re.MULTILINE)
TRANSPORT = re.compile(r"^transport:\s*(on|off)\s*$", re.MULTILINE)
DELIVERY = re.compile(r"^lxmf\.delivery\s*:\s*([0-9a-f]{32})", re.MULTILINE)
# The radio's figures as `rncfg set` takes them, and the scale from the nodeset's units.
RADIO_FLAGS = (("freq_mhz", "--freq-hz", 1e6), ("sf", "--sf", 1), ("bw_khz", "--bw-hz", 1e3),
               ("cr", "--cr", 1), ("tx_dbm", "--txpower-dbm", 1))

TOOL_TIMEOUT_S = 10.0       # one rncfg invocation, KISS round trips included
TOOL_TRIES = 3              # in a virtual-time run, a line whose reply timed out is tried again
POLL_S = 0.5                # how often a booting station is asked whether it is up


class Sergeyculum(Kind):
    type_name = "sergeyculum"
    role_volatile = True

    def __init__(self, device):
        super().__init__(device)
        self.rncfg = self.tools.get("rncfg") or shutil.which("rncfg")
        # One rncfg at a time per station: two on one pty interleave their
        # KISS frames and both read garbage.
        self.locks = {}

    def env(self, station):
        env = super().env(station)
        if station.clock is not None:
            # A plain process: the time shim says it is idle when all its
            # threads are blocked.
            env["SIM_MESH_IDLE"] = "threads"
        return env

    def kiss(self, station):
        return os.path.join(station.dir, "kiss")

    def lock_for(self, station):
        return self.locks.setdefault(station.dir, asyncio.Lock())

    async def rncfg_run(self, station, verb, args, timeout=TOOL_TIMEOUT_S):
        """`rncfg <verb> <kiss> <args…>`: its exit status and what it printed."""
        if not self.rncfg:
            raise CommandError("no rncfg: name it under the kind's tools: or put it on PATH")
        async with self.lock_for(station):
            return await run_tool([self.rncfg, verb, self.kiss(station), *args], timeout)

    async def wait_up(self, station, timeout):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if os.path.exists(self.kiss(station)):
                try:
                    _, out = await self.rncfg_run(station, "detect", [])
                    if DETECT_OK.search(out):
                        return True
                except CommandError:
                    pass
            await self.pause(station, POLL_S)
        return False

    async def run(self, station, line, timeout=TOOL_TIMEOUT_S):
        try:
            words = shlex.split(line)
        except ValueError as err:
            raise CommandError("%r: %s" % (line, err)) from err
        if not words:
            return ""
        for _ in range(TOOL_TRIES):
            code, out = await self.rncfg_run(station, words[0], words[1:], timeout)
            # rncfg waits for each reply on the wall clock; a station in a
            # virtual-time run answers on its own, which a busy stretch of
            # the run can make slower than that.
            if code == 0 or station.clock is None or "timeout" not in out:
                break
        if code != 0:
            raise CommandError(out.strip() or "rncfg exited %d" % code)
        return out

    async def role(self, station):
        """`transport` while `rncfg transport get` says on, else `client`."""
        code, out = await self.rncfg_run(station, "transport", ["get"])
        found = TRANSPORT.search(out) if code == 0 else None
        if found is None:
            return None
        return "transport" if found.group(1) == "on" else "client"

    async def address(self, station):
        found = DELIVERY.search(await self.run(station, "addr"))
        return found.group(1) if found else None

    def lines(self, verb, **args):
        if verb == "name":
            return ["name set {name}"]
        if verb == "radio":
            flags = ["%s %d" % (flag, round(args[key] * scale)) for key, flag, scale in RADIO_FLAGS
                     if args.get(key) is not None]
            return ["set " + " ".join(flags)] if flags else []
        if verb == "radio_up":
            return []                   # its radio runs from its start
        if verb == "tx_power":
            return ["set --txpower-dbm %d" % round(float(args["dbm"]))]
        if verb == "role":
            return ["transport %s" % ("on" if args["role"] == "transport" else "off")]
        if verb == "announce":
            return ["announce now"]
        if verb == "message":
            return ["send %s %s" % (args["dest"], args["text"])]
        return super().lines(verb, **args)

    def configured(self, station):
        state = os.path.join(station.dir, "state")
        return os.path.isdir(state) and bool(os.listdir(state))
