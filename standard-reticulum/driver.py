"""A standard Reticulum node on sim-mesh: the `reticulum` driver.

The zip holds station.py (the executable), the RNode firmware it starts
(`rnode`, microReticulum_Firmware's Linux daemon built with
`[env:sim-mesh-rnode]`, whose own stack is never started), and Reticulum and
LXMF themselves under python/, which station.py runs on.

Two processes of a station join a virtual-time run: the RNode as the node's
own id, which is the radio the loss tables know, and station.py as a station
with no radio, the node's id plus COMPANION, which reads the console.

A station is asked things over framed RPC on its console, in station.py's
commands. Its settings take effect when it starts: a line that changes one
leaves it pending, and a flush restarts the station to apply it, unless the
station is still being set up.

Its verbs, in station.py's commands:

    name            set name <name>
    radio           set freq_hz, bw_hz, sf and cr, and txpower, for each
                    figure given; sync word and preamble are the RNode's own
                    (0x12, 18)
    tx_power        set txpower <the chip's dBm>
    role            set transport 1 (transport) or 0 (client)
    path            path [<dest>] [-i <iface>] -> {"paths": [{dest, next_hop, iface, hops}]}
    lxmf.create     lxmf create <name>: its one identity is there from its
                    first start, named after the node (the name verb), and
                    the name is its display name; one given another name
                    already is left as it is (None)
    lxmf.identities its display name (show s.net.hostname) and
                    addr                 -> `lxmf.delivery : <hash>`
    lxmf.announce   announce
    lxmf.send       send <dest> <text>   -> `queued <its own id>`, coupled
                    to sim-mesh's; its log says the rest under its own id
"""

import asyncio
import json
import os
import re

from sim_mesh.driver import UP, CommandError, chip_dbm, parse_setting
from sim_mesh.reticulum.driver import ReticulumDriver

COMPANION = 1_000_000       # station.py's ether id, above the node's
DELIVERY = re.compile(r"^lxmf\.delivery\s*:\s*([0-9a-f]{32})", re.MULTILINE)
TRANSPORT = re.compile(r"^transport:\s*(on|off)\s*$", re.MULTILINE)
QUEUED = re.compile(r"queued (\S+)")
MID = re.compile(r"mid=(o_\S+)")
# The radio's figures as station.py's settings, and the scale from the nodeset's units.
RADIO_KEYS = (("freq_mhz", "freq_hz", 1e6), ("bw_khz", "bw_hz", 1e3), ("sf", "sf", 1),
              ("cr", "cr", 1))
MARKER_WAIT_S = 20.0        # how long a started station has to print the framed-RPC marker
POLL_S = 0.5
RESTART_TIMEOUT_S = 120.0   # how long a flush waits for the restarted station to be up


class StandardReticulum(ReticulumDriver):

    def sids(self, station):
        return (station.node_id, station.node_id + COMPANION)

    def console_sid(self, station):
        return station.node_id + COMPANION

    def env(self, station):
        env = {"SIM_MESH_NODE_ID": str(station.node_id + COMPANION),
               "SR_RNODE_ID": str(station.node_id),
               "SR_RNODE": os.path.join(self.firmware["dir"], "rnode"),
               "PYTHONPATH": os.path.join(self.firmware["dir"], "python"),
               "PYTHONUNBUFFERED": "1"}
        if station.virtual:
            # Plain processes: the time shim says each is idle when all its
            # threads are blocked.
            env["SIM_MESH_IDLE"] = "threads"
        return env

    def configured(self, station):
        return os.path.exists(os.path.join(station.dir, "state", "settings.json"))

    async def run(self, station, line, timeout=None):
        return await self.rpc_query(station, line, timeout)

    async def status(self, station):
        """{"state": starting|up, "pending": yes|no}."""
        out = await self.run(station, "status")
        return dict(re.findall(r"^(\w+):\s*(\S+)", out, re.MULTILINE))

    async def wait_up(self, station, timeout):
        """Up once it answers a frame and Reticulum and its router are running."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if not await self.rpc_ready(station, timeout, MARKER_WAIT_S):
            return False
        while loop.time() < deadline:
            try:
                if (await self.status(station)).get("state") == "up":
                    return True
            except CommandError:
                pass
            await self.pause(station, POLL_S)
        return False

    async def flush(self, station):
        """Restart the station on its settings as they now are, when one is
        pending and the station is not in the middle of being set up."""
        if station.status != UP:
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

    # ---- the reticulum verbs -----------------------------------------------

    async def name(self, station, name):
        await self.run(station, "set name %s" % name)

    async def role(self, station, role):
        await self.run(station, "set transport %d" % (1 if role == "transport" else 0))

    async def radio(self, station, **figures):
        for name, key, scale in RADIO_KEYS:
            if figures.get(name) is not None:
                await self.run(station, "set %s %d" % (key, round(figures[name] * scale)))
        if figures.get("tx_dbm") is not None:
            await self.tx_power(station, figures["tx_dbm"])

    async def tx_power(self, station, dbm):
        # The power at the connector, as the chip's own through the board's front end.
        chip = max(0, chip_dbm(station.board, round(float(dbm))))
        await self.run(station, "set txpower %d" % chip)

    async def path(self, station, dest=None, iface=None):
        line = "path" + (" %s" % dest if dest else "") + (" -i %s" % iface if iface else "")
        out = await self.run(station, line)
        try:
            return json.loads(out[out.index("{"):]).get("paths") or []
        except ValueError as err:
            raise CommandError(out.strip()[:200]) from err

    async def current_role(self, station):
        found = TRANSPORT.search(await self.run(station, "transport"))
        if found is None:
            return None
        return "transport" if found.group(1) == "on" else "client"

    async def diagnostics(self, station):
        return {"paths": await self.run(station, "paths")}

    # ---- LXMF: one identity, from its first start --------------------------

    async def display_name(self, station):
        return parse_setting(await self.run(station, "show s.net.hostname"), "s.net.hostname")

    async def lxmf_create(self, station, name):
        had = await self.display_name(station)
        if had not in (name, station.name):
            return None             # its one identity has another name already
        if had != name:
            await self.run(station, "lxmf create %s" % name)
        found = DELIVERY.search(await self.run(station, "addr"))
        return found.group(1) if found else None

    async def lxmf_identities(self, station):
        found = DELIVERY.search(await self.run(station, "addr"))
        return [(await self.display_name(station), found.group(1))] if found else []

    async def lxmf_announce(self, station, name=None):
        await self.run(station, "announce")

    async def lxmf_send(self, station, dest, text, mid, sender=None):
        if sender is not None and await self.display_name(station) != sender:
            raise CommandError("%s has no LXMF identity named %s" % (station.name, sender))
        out = await self.run(station, "send %s %s" % (dest, text))
        found = QUEUED.search(out)
        if not found:
            self.lxmf_status(station, mid, "failed", out.strip()[:300] or "no message id")
            return
        self.lxmf_couple(station, mid, found.group(1))

    # ---- what it prints ----------------------------------------------------

    def console_line(self, station, line):
        found = MID.search(line)
        if not found:
            return
        said = line.split("lxmf:", 1)[-1].strip()
        if "delivered mid=" in line:
            self.lxmf_native(station, found.group(1), "delivered")
        elif "failed" in line:
            self.lxmf_native(station, found.group(1), "failed", said)
        else:
            self.lxmf_native(station, found.group(1), "pending", said)


DRIVER = StandardReticulum
