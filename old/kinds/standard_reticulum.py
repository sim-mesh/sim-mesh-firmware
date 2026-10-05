"""A standard Reticulum node: the RNode firmware with Reticulum's Python
reference implementation and an LXMF router behind it
(stations/standard_reticulum/station.py).

Its device's `elf` is station.py, and it names the RNode among its tools,
`tools: { rnode: <path> }`: microReticulum_Firmware's Linux daemon built with
`[env:sim-mesh-rnode]`, whose own stack is never started, so the radio is the
host's alone, as on the standard RNode firmware. The station needs a
python3 with Reticulum (`rns`) and LXMF (`lxmf`), which sim-mesh's image has.

Two processes of a station join a virtual-time run: the RNode as the node's
own id, which is the radio the loss tables know, and station.py as a station
with no radio, the node's id plus COMPANION, which reads the console.

A station of this kind is asked things over framed RPC on its console, as a
`reticulous` one is, in station.py's commands. Its settings take effect
when it starts: a line that changes one leaves it pending, and a flush
restarts the station to apply it, unless the station is still being set up
(setup is several batches with a flush after each); simd flushes once more
when setup is done.

Its intents:

    name        set name {name}
    radio       set freq_hz, bw_hz, sf and cr, and txp, for each figure given;
                sync word and preamble are the RNode's own (0x12, 18)
    tx_power    txp <dBm>
    role        set transport 1 (transport) or 0 (client)
    announce    announce
    message     send <dest> <text>
    path        path <dest>

`txp <dBm>` is the power at the connector, which the kind turns into the
chip's (`set txpower`, boards.chip_dbm), as the microreticulum kind does.
"""

import asyncio
import os
import re

import boards as boards_module
import rpc as rpc_module
import stations as stations_module

from . import HERE, CommandError, Kind

COMPANION = 1_000_000       # station.py's ether id, above the node's
SIMRADIO = os.path.normpath(os.path.join(HERE, "..", "..", "radio", "build", "libsimradio.so"))
DELIVERY = re.compile(r"^lxmf\.delivery\s*:\s*([0-9a-f]{32})", re.MULTILINE)
TRANSPORT = re.compile(r"^transport:\s*(on|off)\s*$", re.MULTILINE)
# The radio's figures as station.py's settings, and the scale from the nodeset's units.
RADIO_KEYS = (("freq_mhz", "freq_hz", 1e6), ("bw_khz", "bw_hz", 1e3), ("sf", "sf", 1),
              ("cr", "cr", 1))
MARKER_WAIT_S = 20.0        # how long a started station has to print the framed-RPC marker
POLL_S = 0.5
RESTART_TIMEOUT_S = 120.0   # how long a flush waits for the restarted station to be up


class StandardReticulum(Kind):
    type_name = "standard_reticulum"

    def sids(self, station):
        return (station.node_id, station.node_id + COMPANION)

    def console_sid(self, station):
        return station.node_id + COMPANION

    def env(self, station):
        env = super().env(station)
        if station.clock is not None:
            # Plain processes: the time shim says each is idle when all its
            # threads are blocked.
            env["SIM_MESH_IDLE"] = "threads"
        env.update(SIM_MESH_NODE_ID=str(station.node_id + COMPANION),
                   SR_RNODE_ID=str(station.node_id),
                   SR_RNODE=self.tools.get("rnode") or "",
                   SR_SIMRADIO=SIMRADIO,
                   PYTHONUNBUFFERED="1")
        return env

    def client(self, station):
        if station.rpc is None:
            raise CommandError("%s is not running" % station.name)
        return station.rpc

    async def status(self, station):
        """{"state": starting|up, "pending": yes|no}."""
        out = await self.ask(station, "status")
        return dict(re.findall(r"^(\w+):\s*(\S+)", out, re.MULTILINE))

    async def ask(self, station, line, timeout=None):
        try:
            return await self.client(station).query(line, timeout=timeout)
        except rpc_module.RpcError as err:
            raise CommandError(str(err)) from err

    async def wait_up(self, station, timeout):
        """Up once it answers a frame and Reticulum and its router are running."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if not await self.client(station).wait_ready(
                timeout, MARKER_WAIT_S, lambda s: self.pause(station, s)):
            return False
        while loop.time() < deadline:
            try:
                if (await self.status(station)).get("state") == "up":
                    return True
            except CommandError:
                pass
            await self.pause(station, POLL_S)
        return False

    async def run(self, station, line, timeout=None):
        words = line.split()
        if len(words) == 2 and words[0] == "txp":
            try:
                connector = round(float(words[1]))
            except ValueError as err:
                raise CommandError("%r: txp takes dBm at the connector" % line) from err
            chip = max(0, boards_module.chip_dbm(station.board, connector))
            line = "set txpower %d" % chip
        return await self.ask(station, line, timeout)

    async def flush(self, station):
        """Restart the station on its settings as they now are, when one is
        pending and the station is not in the middle of being set up."""
        if station.status != stations_module.UP:
            return
        try:
            if (await self.status(station)).get("pending") != "yes":
                return
        except CommandError:
            return
        before = station.starts
        await station.restart()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESTART_TIMEOUT_S
        while station.starts == before and loop.time() < deadline:
            await self.pause(station, POLL_S)
        if not await self.wait_up(station, max(0.0, deadline - loop.time())):
            raise CommandError("%s did not come back up after its restart" % station.name)

    async def role(self, station):
        found = TRANSPORT.search(await self.ask(station, "transport"))
        if found is None:
            return None
        return "transport" if found.group(1) == "on" else "client"

    async def address(self, station):
        found = DELIVERY.search(await self.ask(station, "addr"))
        return found.group(1) if found else None

    def configured(self, station):
        return os.path.exists(os.path.join(station.dir, "state", "settings.json"))

    def lines(self, verb, **args):
        if verb == "name":
            return ["set name {name}"]
        if verb == "radio":
            out = ["set %s %d" % (key, round(args[name] * scale)) for name, key, scale in RADIO_KEYS
                   if args.get(name) is not None]
            if args.get("tx_dbm") is not None:
                out.append("txp %d" % round(args["tx_dbm"]))
            return out
        if verb == "radio_up":
            return []                   # Reticulum brings its RNode up when it starts
        if verb == "tx_power":
            return ["txp %d" % round(float(args["dbm"]))]
        if verb == "role":
            return ["set transport %d" % (1 if args["role"] == "transport" else 0)]
        if verb == "announce":
            return ["announce"]
        if verb == "message":
            return ["send %s %s" % (args["dest"], args["text"])]
        if verb == "path":
            return ["path %s" % args["dest"]]
        return super().lines(verb, **args)
