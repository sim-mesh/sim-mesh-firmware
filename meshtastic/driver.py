"""meshtasticd on sim-mesh: the driver, category `meshtastic`.

```
sim-mesh ── framed RPC on the console ──► host.py ── a command ──► meshtasticd's TCP API
host.py ── "mthost: {json}" lines ──► console_line ──► msg.status, msg.received
```

Every verb is one of host.py's commands, answered in JSON. The host is a
second process of the station with its own id in the ether (node id +
1 000 000), and it reads the console.

Settings are applied in one transaction, since the firmware reboots after
each: while a station is being set up, the settings verbs only record what
they were told and `flush` sends it all at once; after setup, each applies
in a transaction of its own and waits for the restart that follows.
"""

import asyncio
import json
import os
import shlex

from sim_mesh.driver import CommandError, chip_dbm, tagged, untagged
from sim_mesh.meshtastic.driver import ROLES, ROUTER_ROLES, MeshtasticDriver

HOST_SID_OFFSET = 1_000_000
HOST_LINE = "mthost: "
CONFIGURED = "sim-configured"           # in state/: set up, written when the settings are saved
RESTART_WAIT_S = 30.0                   # flush: the restart after the commit
SETTLE_WAIT_S = 60.0                    # a settings verb after setup: back up
TRACEROUTE_WAIT_S = 60.0
# The host bounds a setting at 30 s (a first region makes keys): asked with
# more, so it is never asked twice.
SETTINGS_COMMANDS = ("edit", "owner", "lora", "device")
SETTINGS_TIMEOUT_S = 35.0
STEP_S = 0.5
SYNC_WORD = 0x2B
PREAMBLE = 16
EARLY_KEPT = 64                         # events for ids not yet known, per station
# Errors that mean a message never went out, which a channel message reports;
# everything else about a channel message is the mesh's, unacknowledged.
REFUSALS = ("RATE_LIMIT_EXCEEDED", "DUTY_CYCLE_LIMIT", "NO_CHANNEL", "TOO_LARGE",
            "NO_INTERFACE", "BAD_REQUEST")

# The firmware's region table (src/mesh/RadioInterface.cpp), in its order:
# name, start and end MHz.
REGIONS = (
    ("US", 902.0, 928.0), ("EU_433", 433.0, 434.0), ("EU_868", 869.4, 869.65),
    ("CN", 470.0, 510.0), ("JP", 920.5, 923.5), ("ANZ", 915.0, 928.0),
    ("ANZ_433", 433.05, 434.79), ("RU", 868.7, 869.2), ("KR", 920.0, 923.0),
    ("TW", 920.0, 925.0), ("IN", 865.0, 867.0), ("NZ_865", 864.0, 868.0),
    ("TH", 920.0, 925.0), ("UA_433", 433.0, 434.7), ("UA_868", 868.0, 868.6),
    ("MY_433", 433.0, 435.0), ("MY_919", 919.0, 924.0), ("SG_923", 917.0, 925.0),
    ("PH_433", 433.0, 434.7), ("PH_868", 868.0, 869.4), ("PH_915", 915.0, 918.0),
    ("KZ_433", 433.075, 434.775), ("KZ_863", 863.0, 868.0), ("NP_865", 865.0, 868.0),
    ("BR_902", 902.0, 907.5), ("LORA_24", 2400.0, 2483.5))
# kHz -> the firmware's `bandwidth` code (src/mesh/MeshRadio.h).
BANDWIDTHS = {31.25: 31, 62.5: 62, 125.0: 125, 250.0: 250, 500.0: 500,
              203.125: 200, 406.25: 400, 812.5: 800, 1625.0: 1600}
WIDE = (203.125, 406.25, 812.5, 1625.0)       # 2.4 GHz only


def bandwidth_code(bw_khz):
    code = BANDWIDTHS.get(float(bw_khz))
    if code is None:
        raise CommandError("Meshtastic has no bandwidth of %g kHz (there are %s)"
                           % (bw_khz, ", ".join("%g" % k for k in sorted(BANDWIDTHS))))
    return code


def region_for(freq_mhz, bw_khz):
    """The first region in the firmware's order whose band holds the
    frequency and is at least the bandwidth wide."""
    for name, start, end in REGIONS:
        if start <= freq_mhz <= end and end - start >= bw_khz / 1000.0:
            if float(bw_khz) in WIDE and name != "LORA_24":
                continue
            return name
    raise CommandError("no Meshtastic region holds %g MHz at %g kHz" % (freq_mhz, bw_khz))


class Meshtasticd(MeshtasticDriver):
    def __init__(self, firmware):
        super().__init__(firmware)
        self.pending = {}       # station name -> {"owner": (long, short), "lora": {}, "device": {}}
        self.open = {}          # station name -> {packet id: (mid, its destination's !id)}
        self.channel = {}       # station name -> {packet id: mid}, channel messages, the latest
        self.traces = {}        # station name -> {packet id: future}
        self.early = {}         # station name -> {packet id: event}, in arrival order
        self.regions = {}       # station name -> the region its radio was given

    # ---- starting a station ----------------------------------------------

    def argv(self, station):
        return ["env", "SIM_MESH_NODE_ID=%d" % (station.node_id + HOST_SID_OFFSET),
                "MESHTASTIC_FIRMWARE_NODE_ID=%d" % station.node_id,
                "python3", self.firmware["exec"]]

    def env(self, station):
        return {"SIM_MESH_IDLE": "threads"}

    def sids(self, station):
        return (station.node_id, station.node_id + HOST_SID_OFFSET)

    def console_sid(self, station):
        return station.node_id + HOST_SID_OFFSET

    def configured(self, station):
        return os.path.exists(os.path.join(station.dir, "state", CONFIGURED))

    async def wait_up(self, station, timeout):
        # A message still open from before a restart has no answer coming.
        for mid, _ in self.open.pop(station.name, {}).values():
            self.msg_status(station, mid, "failed", "station restarted")
        self.channel.pop(station.name, None)
        self.traces.pop(station.name, None)
        self.early.pop(station.name, None)
        if not await self.rpc_ready(station, timeout, timeout):
            return False
        try:
            await self.cmd(station, "info")
        except CommandError:
            return False
        return True

    async def flush(self, station):
        pending = self.pending.pop(station.name, None)
        if not pending:
            return
        await self.cmd(station, "edit begin")
        await self.send_settings(station, pending)
        await self.cmd(station, "edit commit")
        state = os.path.join(station.dir, "state")
        os.makedirs(state, exist_ok=True)
        open(os.path.join(state, CONFIGURED), "a").close()
        # The firmware restarts now; this is cancelled when its process exits.
        await self.pause(station, RESTART_WAIT_S)
        raise CommandError("%s did not restart after its settings" % station.name)

    # ---- lines -----------------------------------------------------------

    async def run(self, station, line, timeout=None):
        return await self.rpc_query(station, line, timeout)

    async def cmd(self, station, line):
        """One host command, its JSON reply; an error it reports is raised."""
        timeout = SETTINGS_TIMEOUT_S if line.split()[0] in SETTINGS_COMMANDS else None
        text = (await self.run(station, line, timeout)).strip()
        try:
            got = json.loads(text)
        except ValueError:
            raise CommandError("%s: %s" % (line.split()[0], text or "no answer")) from None
        if isinstance(got, dict) and "error" in got:
            raise CommandError("%s: %s" % (line.split()[0], got["error"]))
        return got

    # ---- settings ---------------------------------------------------------

    async def send_settings(self, station, settings):
        if "owner" in settings:
            await self.cmd(station, "owner %s %s" % tuple(shlex.quote(s) for s in settings["owner"]))
        if settings.get("lora"):
            await self.cmd(station, "lora " + " ".join(
                "%s=%s" % kv for kv in settings["lora"].items()))
        if settings.get("device"):
            await self.cmd(station, "device " + " ".join(
                "%s=%s" % kv for kv in settings["device"].items()))

    async def setting(self, station, kind, value):
        """A setting: recorded while the station is being set up, else
        applied at once in a transaction of its own, the station's restart
        waited for."""
        if not self.configured(station):
            mine = self.pending.setdefault(station.name, {})
            if kind == "owner":
                mine["owner"] = value
            else:
                mine.setdefault(kind, {}).update(value)
            return
        starts = station.starts
        await self.cmd(station, "edit begin")
        await self.send_settings(station, {kind: value})
        await self.cmd(station, "edit commit")
        waited = 0.0
        while waited < SETTLE_WAIT_S:
            await self.pause(station, STEP_S)
            waited += STEP_S
            if station.starts > starts and station.status == "up":
                return
        raise CommandError("%s did not come back up after its settings" % station.name)

    # ---- every firmware's verbs ------------------------------------------

    async def name(self, station, name):
        await self.setting(station, "owner", (name, name[:4]))

    async def radio(self, station, freq_mhz=None, sf=None, bw_khz=None, cr=None, tx_dbm=None,
                    sync=None, preamble=None):
        if sync is not None and int(sync) != SYNC_WORD:
            raise CommandError("Meshtastic's sync word is fixed at 0x%02x" % SYNC_WORD)
        if preamble is not None and int(preamble) != PREAMBLE:
            raise CommandError("Meshtastic's preamble is fixed at %d symbols" % PREAMBLE)
        lora = {}
        if (freq_mhz, sf, bw_khz, cr) != (None, None, None, None):
            if None in (freq_mhz, sf, bw_khz, cr):
                now = (await self.cmd(station, "info"))["lora"]
                freq_mhz = freq_mhz if freq_mhz is not None else now.get("override_frequency")
                sf = sf if sf is not None else now.get("spread_factor")
                cr = cr if cr is not None else now.get("coding_rate")
                if bw_khz is None:
                    code = now.get("bandwidth")
                    bw_khz = next((k for k, v in BANDWIDTHS.items() if v == code), None)
                if not freq_mhz or not sf or not cr or bw_khz is None:
                    raise CommandError("radio: give the frequency, spreading factor, bandwidth "
                                       "and coding rate together")
            code = bandwidth_code(bw_khz)
            region = region_for(float(freq_mhz), float(bw_khz))
            self.regions[station.name] = region
            lora.update({"use_preset": "false", "bandwidth": code, "spread_factor": int(sf),
                         "coding_rate": int(cr), "override_frequency": "%g" % float(freq_mhz),
                         "frequency_offset": 0, "tx_enabled": "true", "region": region})
        if tx_dbm is not None:
            lora["tx_power"] = self.power(station, tx_dbm)
        if lora:
            await self.setting(station, "lora", lora)

    def power(self, station, dbm):
        # 0 is "the region's maximum" to Meshtastic.
        return max(1, int(round(chip_dbm(station.board, dbm))))

    async def tx_power(self, station, dbm):
        await self.setting(station, "lora", {"tx_power": self.power(station, dbm)})

    async def diagnostics(self, station):
        info = await self.cmd(station, "info")
        if station.name in self.regions:
            info["region_asked"] = self.regions[station.name]
        return {"info": json.dumps(info, indent=1), "nodes": json.dumps(
            await self.cmd(station, "nodes"), indent=1)}

    # ---- the category's verbs --------------------------------------------

    async def role(self, station, role):
        role = str(role).lower()
        if role not in ROLES:
            raise CommandError("no Meshtastic role %r (there are %s)" % (role, ", ".join(ROLES)))
        await self.setting(station, "device", {"role": role.upper()})

    async def hop_limit(self, station, n):
        n = int(n)
        if not 0 <= n <= 7:
            raise CommandError("a hop limit is 0 to 7, not %d" % n)
        await self.setting(station, "lora", {"hop_limit": n})

    async def current_role(self, station):
        role = (await self.cmd(station, "info")).get("role", "")
        return "router" if role.lower() in ROUTER_ROLES else "client"

    async def sendtext(self, station, text, mid, dest=None, ch_index=0, want_ack=True):
        line = "send %s %d %d %s" % (shlex.quote(dest) if dest else "^all", int(ch_index),
                                     1 if want_ack else 0, shlex.quote(tagged(text, mid)))
        try:
            sent = await self.cmd(station, line)
        except CommandError as err:
            self.msg_status(station, mid, "failed", str(err).split(": ", 1)[-1])
            return
        self.msg_status(station, mid, "sent")
        pid = sent.get("id")
        if pid is None:
            return
        if dest:
            self.open.setdefault(station.name, {})[pid] = (mid, sent.get("to"))
        else:
            channel = self.channel.setdefault(station.name, {})
            channel[pid] = mid
            while len(channel) > EARLY_KEPT:
                channel.pop(next(iter(channel)))
        said = self.early.get(station.name, {}).pop(pid, None)
        if said is not None:
            self.on_routing(station, said)

    async def traceroute(self, station, dest):
        got = await self.cmd(station, "traceroute %s" % shlex.quote(dest))
        pid = got["id"]
        fut = asyncio.get_running_loop().create_future()
        self.traces.setdefault(station.name, {})[pid] = fut
        said = self.early.get(station.name, {}).pop(pid, None)
        if said is not None:
            fut.set_result(said)
        waited = 0.0
        while not fut.done() and waited < TRACEROUTE_WAIT_S:
            await self.pause(station, STEP_S)
            waited += STEP_S
        self.traces.get(station.name, {}).pop(pid, None)
        if not fut.done():
            raise CommandError("no traceroute answer from %s" % dest)
        said = fut.result()
        return {k: said.get(k) for k in ("route", "snr_towards", "route_back", "snr_back")}

    async def nodes(self, station):
        return [(n.get("name"), n.get("id"), n.get("hops_away"), n.get("snr"), n.get("last_heard"))
                for n in await self.cmd(station, "nodes")]

    async def nodeinfo(self, station):
        await self.cmd(station, "nodeinfo")

    # ---- what the host says ----------------------------------------------

    def console_line(self, station, line):
        at = line.find(HOST_LINE)
        if at < 0:
            return
        try:
            said = json.loads(line[at + len(HOST_LINE):])
        except ValueError:
            return
        if not isinstance(said, dict):
            return
        kind = said.get("event")
        if kind == "routing":
            pid = said.get("id")
            if pid in self.open.get(station.name, {}) or pid in self.channel.get(station.name, {}):
                self.on_routing(station, said)
            else:
                self.keep_early(station, said)
        elif kind == "recv":
            body, mid = untagged(said.get("text") or "")
            if mid is None:
                return
            if said.get("to") == "^all":
                self.msg_received(station, mid, body, chan=said.get("ch", 0))
            else:
                self.msg_received(station, mid, body, sender=said.get("from"))
        elif kind == "traceroute":
            fut = self.traces.get(station.name, {}).get(said.get("id"))
            if fut is None:
                self.keep_early(station, said)
            elif not fut.done():
                fut.set_result(said)

    def keep_early(self, station, said):
        early = self.early.setdefault(station.name, {})
        early[said.get("id")] = said
        while len(early) > EARLY_KEPT:
            early.pop(next(iter(early)))

    def on_routing(self, station, said):
        """A routing answer for a message of ours: an acknowledgement from
        its destination, or an error."""
        pid = said.get("id")
        error = said.get("error")
        channel = self.channel.get(station.name, {})
        if pid in channel:
            if error in REFUSALS:
                self.msg_status(station, channel.pop(pid), "failed", error)
            return
        mine = self.open.get(station.name, {})
        if pid not in mine:
            return
        mid, to = mine[pid]
        if error == "NONE":
            if said.get("from") == to:
                del mine[pid]
                self.msg_status(station, mid, "delivered")
            return
        del mine[pid]
        self.msg_status(station, mid, "failed", error)


DRIVER = Meshtasticd
