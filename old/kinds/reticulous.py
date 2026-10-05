"""The reticulous firmware (spangap/reticulous), built for the Linux host.

Its device supplies the executable and the build's merged `/fixed` tree
(`fixed` in node.yaml; a workspace's `esp-idf/build.linux/data_merged`).

A station of this kind is set up and asked things over framed RPC on its
console pty (rpc.py): every setup line, every Run command line, the flush and
the role poll is one frame, answered with what the command printed. Its role
is `transport` while `s.rnsd.transport_enabled` is on, else `client`. It
has a web UI on port 80, a store that coalesces writes until `save`, and
`state/boot` once it has booted.

Its intents, in its CLI:

    name        hostname {name}
    radio       lora 0 freq|sf|bw|cr|txp|sync|preamble for each figure the
                radio gives, then lora up to start it
    tx_power    lora 0 txp <dBm>
    role        set s.rnsd.transport_enabled 1 (transport) or 0 (client)
    announce    lora 0 a
    message     lxmf send <dest> <text>
    path        rnpath -j <dest>
    peer_tcp    tcp peer add <addr>:<port>

and its address is the `lxmf.delivery` hash its `lxmf` listing shows.
"""

import asyncio
import os
import re

import rpc as rpc_module

from . import CommandError, Kind

TRANSPORT_KEY = "s.rnsd.transport_enabled"
BOOTED_KEY = "s.sys.reset_reason"      # written once every service's init has run
DEST = re.compile(r"\*\s*\d+\s+\S+\s+([0-9a-f]{32})")
# The radio's figures as its CLI takes them, in the order they are set.
RADIO_LINES = (("freq_mhz", "lora 0 freq %g"), ("sf", "lora 0 sf %d"), ("bw_khz", "lora 0 bw %g"),
               ("cr", "lora 0 cr %d"), ("tx_dbm", "lora 0 txp %g"),
               ("sync", "lora 0 sync 0x%02x"), ("preamble", "lora 0 preamble %d"))
MARKER_WAIT_S = 20.0        # how long a started station has to print the framed-RPC marker
SLOW_S = rpc_module.EXEC_BOUND_S - 0.5   # a reply this late may have been cut at the bound
SETTLE_POLL_S = 0.5

# A line whose effect lands after its reply, and the one key that says it has.
CONFIRM = {
    "lora up": ("s.lora.0.enable", "1"),
}


class Reticulous(Kind):
    type_name = "reticulous"

    def env(self, station):
        env = super().env(station)
        # What this firmware reads: its board (hw-linux) takes its identity,
        # directory, address, ether, /fixed tree and board (its radio's front
        # end and ceiling) from these names.
        env.update(SPANGAP_NODE_ID=str(station.node_id),
                   SPANGAP_NODE_DIR=station.dir,
                   SPANGAP_BIND_ADDR=station.addr,
                   SPANGAP_ETHER=station.ether_addr)
        if self.fixed:
            env["SPANGAP_FIXED_DIR"] = self.fixed
        if env.get("SIM_MESH_BOARD"):
            env["SPANGAP_BOARD"] = env["SIM_MESH_BOARD"]
        return env

    def client(self, station):
        if station.rpc is None:
            raise CommandError("%s is not running" % station.name)
        return station.rpc

    async def wait_up(self, station, timeout):
        """Up once it answers a frame and its boot has initialised every
        service: the console answers from early in boot, and a setting typed
        before a service's own init has run can be undone by that init (a
        radio's frequency is). The boot writes BOOTED_KEY once they all have,
        and on a first boot, which is when a station is set up, it is absent
        until then."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if not await self.client(station).wait_ready(
                timeout, MARKER_WAIT_S, lambda s: self.pause(station, s)):
            return False
        while loop.time() < deadline:
            try:
                if await self.show(station, BOOTED_KEY):
                    return True
            except CommandError:
                pass
            await self.pause(station, SETTLE_POLL_S)
        return False

    async def run(self, station, line, timeout=None):
        try:
            return await self.client(station).query(line, timeout=timeout)
        except rpc_module.RpcError as err:
            raise CommandError(str(err)) from err

    async def show(self, station, key):
        return rpc_module.parse_setting(await self.run(station, "show %s" % key), key)

    async def settle(self, station, line, elapsed):
        """After a line, wait until what it asked for has landed.

        A reply that came back at the station's exec bound may have been cut
        there with the command still running, so the next frame waits for the
        CLI to answer again. A line in CONFIRM is followed until its key says
        it took.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2 * rpc_module.QUERY_TIMEOUT_S
        if elapsed >= SLOW_S:
            while loop.time() < deadline:
                try:
                    if (await self.run(station, rpc_module.PROBE)).strip():
                        break
                except CommandError:
                    pass
                await self.pause(station, SETTLE_POLL_S)
        want = CONFIRM.get(" ".join(line.split()))
        if want is None:
            return
        key, value = want
        while loop.time() < deadline:
            if await self.show(station, key) == value:
                return
            await self.pause(station, SETTLE_POLL_S)
        raise CommandError("%s: %s never read %s" % (line, key, value))

    async def setup_line(self, station, line):
        """Each line as its own frame, confirmed where it has to be."""
        loop = asyncio.get_running_loop()
        began = loop.time()
        reply = await self.run(station, line)
        await self.settle(station, line, loop.time() - began)
        return reply

    async def flush(self, station):
        """`save`: the store coalesces writes for `s.storage.flash_delay`
        seconds — a minute by default — so a station set up and then reset
        inside that window would come back with none of it. `save` is not a
        setting, which is why the testbed sends it and a script need not."""
        try:
            await self.run(station, "save")
        except CommandError:
            pass

    async def role(self, station):
        value = await self.show(station, TRANSPORT_KEY)
        if value is None:
            return None
        return "client" if value in ("0", "") else "transport"

    async def address(self, station):
        found = DEST.search(await self.run(station, "lxmf"))
        return found.group(1) if found else None

    def lines(self, verb, **args):
        if verb == "name":
            return ["hostname {name}"]
        if verb == "radio":
            return [line % args[key] for key, line in RADIO_LINES
                    if args.get(key) is not None]
        if verb == "radio_up":
            return ["lora up"]
        if verb == "tx_power":
            return ["lora 0 txp %g" % float(args["dbm"])]
        if verb == "role":
            return ["set %s %d" % (TRANSPORT_KEY, 1 if args["role"] == "transport" else 0)]
        if verb == "announce":
            return ["lora 0 a"]
        if verb == "message":
            return ["lxmf send %s %s" % (args["dest"], args["text"])]
        if verb == "path":
            return ["rnpath -j %s" % args["dest"]]
        if verb == "peer_tcp":
            return ["tcp peer add %s:%d" % (args["addr"], int(args.get("port") or 4965))]
        return super().lines(verb, **args)

    def web_port(self):
        return 80

    def configured(self, station):
        return os.path.exists(os.path.join(station.dir, "state", "boot"))
