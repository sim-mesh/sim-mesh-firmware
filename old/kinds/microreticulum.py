"""microReticulum_Firmware (github.com/attermann/microReticulum_Firmware), the
RNode firmware with the microReticulum stack embedded, as its Portduino
Linux daemon built with `[env:sim-mesh]`: a transport node on LoRa, and nothing
else.

Its radio is sim-mesh's chip through the Portduino backend library
(radio/portduino), which reads the pins it drives the chip on from
SIMRADIO_PIN_*; the kind gives it the same pins it writes into the
daemon's config.

A station of this kind has no console to type at: the console (stdout)
carries its log, and it is configured by `rnoded.conf` in its directory,
which it reads when it starts. So its lines are edits to that file:

    set <key> <value>   sets one key, keeping the rest of the file
    unset <key>         removes one
    txp <dBm>           sets lora_txp to the chip power that puts <dBm> at
                        the antenna connector through the node's front end
                        when it has one (boards.chip_dbm), as a board build of the firmware
                        converts through its amplifier's table; lora_txp
                        itself is the SX1262's own power

and a flush restarts the station when a line has changed the file. The radio
figures (`lora_*`) seed its EEPROM image only when it has none, so a flush
that changed one removes `state/eeprom` first; the image holds nothing but
those figures and the daemon's provisioning header, which it writes again.
A flush while the station is still being set up restarts nothing: setup is
several batches with a flush after each, and a restart ends setup where it
stands. simd flushes the station once more when setup is done, and that one
applies the lot.

Its intents:

    radio       set lora_freq_hz, lora_bw_hz, lora_sf and lora_cr, and txp,
                for each figure given; sync word and preamble are the
                firmware's own (0x12, 18) and not settable
    tx_power    txp <dBm>
    role        transport, which it always is, says nothing; client is refused
    name        says nothing: the daemon has no name to set

and announce, message, path and peer_tcp are refused: it is a transport node
with no destination of its own to announce or send from.

It is up once its log shows `RNS Transport is READY!` since its latest start,
which it prints once transport is running; `RNS is inoperable` there means
its radio did not come up. Its device's `env:` may carry
MR_LORA_INTERFACE_MODE, the LoRa interface's Reticulum mode (gateway when
unset), which the kind writes into a new station's `rnoded.conf`.
"""

import asyncio
import os
import re

import boards as boards_module
import stations as stations_module

from . import CommandError, Kind

# The chip's pins, as the daemon drives them and the backend library binds them.
PINS = (("pin_cs", "SIMRADIO_PIN_NSS", 1), ("pin_reset", "SIMRADIO_PIN_RESET", 2),
        ("pin_busy", "SIMRADIO_PIN_BUSY", 3), ("pin_dio", "SIMRADIO_PIN_DIO1", 4))
# What a new station's rnoded.conf says before any line: the chip, its pins, the
# pins it does not have, no KISS server, and an exit for a reboot so the
# supervisor restarts it; env() adds a device_id of its own, since every station
# shares the host's machine-id.
FIXED = ([("data_dir", "./state"), ("modem", "SX1262")]
         + [(key, str(pin)) for key, _, pin in PINS]
         + [(key, "-1") for key in ("pin_rxen", "pin_txen", "pin_tcxo_enable",
                                    "pin_led_rx", "pin_led_tx")]
         + [("kiss_tcp_port", "0"), ("reboot_mode", "exit")])
# The radio's figures as rnoded.conf keys, and the scale from the nodeset's units.
RADIO_KEYS = (("freq_mhz", "lora_freq_hz", 1e6), ("bw_khz", "lora_bw_hz", 1e3),
              ("sf", "lora_sf", 1), ("cr", "lora_cr", 1))
SEEDS_EEPROM = {key for _, key, _ in RADIO_KEYS} | {"lora_txp"}
MODE_ENV = "MR_LORA_INTERFACE_MODE"

READY = b"RNS Transport is READY!"
INOPERABLE = b"RNS is inoperable"
KEY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=")
POLL_S = 0.5                # how often a booting station's log is read
RESTART_TIMEOUT_S = 60.0    # how long a flush waits for the restarted station to be up


def conf_path(station):
    return os.path.join(station.dir, "rnoded.conf")


def edit_conf(path, key, value):
    """Set `key` to `value` in the file at `path`, or remove it when `value` is
    None, keeping every other line as it was. True when the file changed."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    out, found = [], False
    for line in lines:
        m = KEY_RE.match(line.split("#", 1)[0])
        if m and m.group(1) == key:
            if value is not None and not found:
                out.append("%s = %s" % (key, value))
            found = True
            continue
        out.append(line)
    if value is not None and not found:
        out.append("%s = %s" % (key, value))
    if out == lines:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    return True


class LogScan:
    """How far one station's log has been read, and what it said since the
    station's latest start."""

    def __init__(self):
        self.starts = None
        self.pos = 0
        self.tail = b""
        self.ready = False
        self.inoperable = False


class Microreticulum(Kind):
    type_name = "microreticulum"

    def __init__(self, device):
        super().__init__(device)
        self.dirty = {}             # station dir -> whether a change needs the EEPROM reseeded
        self.scans = {}             # station dir -> LogScan

    def env(self, station):
        env = super().env(station)
        if station.clock is not None:
            # A plain process: the time shim says it is idle when all its
            # threads are blocked.
            env["SIM_MESH_IDLE"] = "threads"
        env.update(MR_CONFIG=conf_path(station),
                   MR_DATA_DIR=os.path.join(station.dir, "state"))
        env.update({var: str(pin) for _, var, pin in PINS})
        if not os.path.exists(conf_path(station)):
            fixed = list(FIXED) + [("device_id", "sim-mesh-%d" % station.node_id)]
            if self.extra_env.get(MODE_ENV):
                fixed.append(("lora_interface_mode", self.extra_env[MODE_ENV]))
            with open(conf_path(station), "w") as f:
                f.write("".join("%s = %s\n" % kv for kv in fixed))
        return env

    # ---- the log ---------------------------------------------------------

    def scan(self, station):
        """Read what the station's log has gained, and say what this start has
        shown so far."""
        scan = self.scans.setdefault(station.dir, LogScan())
        if scan.starts != station.starts:
            scan.starts = station.starts
            scan.ready = scan.inoperable = False
        start = b"--- station %s starting ---" % station.name.encode("utf-8")
        try:
            with open(station.log_path, "rb") as f:
                f.seek(scan.pos)
                data = f.read()
        except FileNotFoundError:
            return scan
        scan.pos += len(data)
        lines = (scan.tail + data).split(b"\n")
        scan.tail = lines.pop()
        for line in lines:
            if start in line:
                scan.ready = scan.inoperable = False
            elif READY in line:
                scan.ready = True
            elif INOPERABLE in line:
                scan.inoperable = True
        return scan

    async def wait_up(self, station, timeout):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            scan = self.scan(station)
            if scan.ready:
                return True
            if scan.inoperable:
                return False
            await self.pause(station, POLL_S)
        return False

    # ---- lines -----------------------------------------------------------

    async def run(self, station, line, timeout=None):
        words = line.split(None, 2)
        if len(words) == 3 and words[0] == "set":
            key, value = words[1], words[2].strip()
        elif len(words) == 2 and words[0] == "unset":
            key, value = words[1], None
        elif len(words) == 2 and words[0] == "txp":
            try:
                connector = round(float(words[1]))
            except ValueError as err:
                raise CommandError("%r: txp takes dBm at the connector" % line) from err
            key, value = "lora_txp", str(boards_module.chip_dbm(station.board, connector))
        else:
            raise CommandError("%r: a %s line is `set <key> <value>`, `unset <key>` or "
                               "`txp <dBm>`" % (line, self.name))
        if not KEY_RE.match(key + "="):
            raise CommandError("%r: not an rnoded.conf key" % key)
        if edit_conf(conf_path(station), key, value):
            reseed = self.dirty.get(station.dir, False) or key in SEEDS_EEPROM
            self.dirty[station.dir] = reseed
        return ""

    async def flush(self, station):
        """Restart the station on the file as it now is, when a line changed it
        and the station is not in the middle of being set up."""
        if station.dir not in self.dirty or station.status != stations_module.UP:
            return
        reseed = self.dirty.pop(station.dir)
        if reseed:
            try:
                os.remove(os.path.join(station.dir, "state", "eeprom"))
            except FileNotFoundError:
                pass
        before = station.starts
        await station.restart()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESTART_TIMEOUT_S
        while station.starts == before and loop.time() < deadline:
            await self.pause(station, POLL_S)
        if not await self.wait_up(station, max(0.0, deadline - loop.time())):
            raise CommandError("%s did not come back up after its restart" % station.name)

    async def role(self, station):
        return "transport" if self.scan(station).ready else None

    async def address(self, station):
        return None

    def configured(self, station):
        state = os.path.join(station.dir, "state")
        return (os.path.exists(conf_path(station))
                and os.path.isdir(state) and bool(os.listdir(state)))

    # ---- intents ---------------------------------------------------------

    def lines(self, verb, **args):
        if verb == "radio":
            out = ["set %s %d" % (key, round(args[name] * scale)) for name, key, scale in RADIO_KEYS
                   if args.get(name) is not None]
            if args.get("tx_dbm") is not None:
                out.append("txp %d" % round(args["tx_dbm"]))
            return out
        if verb == "radio_up":
            return []                   # its radio runs from its start
        if verb == "tx_power":
            return ["txp %d" % round(float(args["dbm"]))]
        if verb == "role":
            if args["role"] != "transport":
                raise CommandError("a %s station is always a transport node" % self.name)
            return []
        if verb == "name":
            return []
        return super().lines(verb, **args)
