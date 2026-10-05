"""microReticulum_Firmware (github.com/attermann/microReticulum_Firmware), the
RNode firmware with the microReticulum stack embedded, as its Portduino Linux
daemon built with `[env:sim-mesh]`: a transport node on LoRa, and nothing
else. The `reticulum` driver.

Its radio is sim-mesh's virtual SX1262 through sim-mesh's Portduino library
(radio/portduino), which reads the pins it drives the chip on from
SIMRADIO_PIN_*; this driver gives it the same pins it writes into the
daemon's config.

A station has no console to type at: the console (stdout) carries its log,
and it is configured by `rnoded.conf` in its directory, which it reads when
it starts. So its lines (a script's `exec`) are edits to that file:

    set <key> <value>   sets one key, keeping the rest of the file
    unset <key>         removes one
    txp <dBm>           sets lora_txp to the chip power that puts <dBm> at the
                        antenna connector through the node's front end

and a flush restarts the station when a line has changed the file. The radio
figures (`lora_*`) seed its EEPROM image only when it has none, so a flush
that changed one removes `state/eeprom` first. A flush while the station is
still being set up restarts nothing: sim-mesh flushes once more when setup is
done, and that one applies the lot.

Its verbs: radio (lora_freq_hz, lora_bw_hz, lora_sf, lora_cr and txp, for
each figure given; sync word and preamble are the firmware's own), tx_power,
role (transport, which it always is, says nothing; client is refused) and
name (nothing: the daemon has no name to set). It has no destination of its
own: lxmf.create makes none, lxmf.identities lists none, and lxmf.announce,
lxmf.send and path are refused.

It is up once its log shows `RNS Transport is READY!` since its latest start;
`RNS is inoperable` there means its radio did not come up. node.yaml's `env`
may carry MR_LORA_INTERFACE_MODE, the LoRa interface's Reticulum mode
(gateway when unset), which is written into a new station's `rnoded.conf`.
"""

import asyncio
import os
import re

from sim_mesh.driver import UP, CommandError, chip_dbm
from sim_mesh.reticulum.driver import ReticulumDriver

# The chip's pins, as the daemon drives them and the radio's Portduino library binds them.
PINS = (("pin_cs", "SIMRADIO_PIN_NSS", 1), ("pin_reset", "SIMRADIO_PIN_RESET", 2),
        ("pin_busy", "SIMRADIO_PIN_BUSY", 3), ("pin_dio", "SIMRADIO_PIN_DIO1", 4))
# What a new station's rnoded.conf says before any line: the chip, its pins, the
# pins it does not have, no KISS server, and an exit for a reboot so the
# supervisor restarts it.
FIXED = ([("data_dir", "./state"), ("modem", "SX1262")]
         + [(key, str(pin)) for key, _, pin in PINS]
         + [(key, "-1") for key in ("pin_rxen", "pin_txen", "pin_tcxo_enable",
                                    "pin_led_rx", "pin_led_tx")]
         + [("kiss_tcp_port", "0"), ("reboot_mode", "exit")])
RADIO_KEYS = (("freq_mhz", "lora_freq_hz", 1e6), ("bw_khz", "lora_bw_hz", 1e3),
              ("sf", "lora_sf", 1), ("cr", "lora_cr", 1))
SEEDS_EEPROM = {key for _, key, _ in RADIO_KEYS} | {"lora_txp"}
MODE_ENV = "MR_LORA_INTERFACE_MODE"
READY = "RNS Transport is READY!"
INOPERABLE = "RNS is inoperable"
KEY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=")
POLL_S = 0.5
RESTART_TIMEOUT_S = 60.0


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


class Microreticulum(ReticulumDriver):

    def __init__(self, firmware):
        super().__init__(firmware)
        self.dirty = {}             # station dir -> whether a change needs the EEPROM reseeded
        self.ready = {}             # station dir -> (start, ready, inoperable), from its log

    def env(self, station):
        env = {"MR_CONFIG": conf_path(station),
               "MR_DATA_DIR": os.path.join(station.dir, "state")}
        if station.virtual:
            env["SIM_MESH_IDLE"] = "threads"
        env.update({var: str(pin) for _, var, pin in PINS})
        if not os.path.exists(conf_path(station)):
            fixed = list(FIXED) + [("device_id", "sim-mesh-%d" % station.node_id)]
            if self.firmware.get("env", {}).get(MODE_ENV):
                fixed.append(("lora_interface_mode", self.firmware["env"][MODE_ENV]))
            with open(conf_path(station), "w") as f:
                f.write("".join("%s = %s\n" % kv for kv in fixed))
        return env

    def configured(self, station):
        state = os.path.join(station.dir, "state")
        return (os.path.exists(conf_path(station))
                and os.path.isdir(state) and bool(os.listdir(state)))

    # ---- its log -----------------------------------------------------------

    def seen(self, station):
        start, ready, bad = self.ready.get(station.dir, (None, False, False))
        if start != station.starts:
            return False, False
        return ready, bad

    def console_line(self, station, line):
        start, ready, bad = self.ready.get(station.dir, (None, False, False))
        if start != station.starts:
            ready = bad = False
        if READY in line:
            ready = True
        elif INOPERABLE in line:
            bad = True
        self.ready[station.dir] = (station.starts, ready, bad)

    async def wait_up(self, station, timeout):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            ready, bad = self.seen(station)
            if ready:
                return True
            if bad:
                return False
            await self.pause(station, POLL_S)
        return False

    # ---- lines -------------------------------------------------------------

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
            key, value = "lora_txp", str(chip_dbm(station.board, connector))
        else:
            raise CommandError("%r: a line is `set <key> <value>`, `unset <key>` or "
                               "`txp <dBm>`" % line)
        if not KEY_RE.match(key + "="):
            raise CommandError("%r: not an rnoded.conf key" % key)
        if edit_conf(conf_path(station), key, value):
            self.dirty[station.dir] = self.dirty.get(station.dir, False) or key in SEEDS_EEPROM
        return ""

    async def flush(self, station):
        """Restart the station on the file as it now is, when a line changed it
        and the station is not in the middle of being set up."""
        if station.dir not in self.dirty or station.status != UP:
            return
        if self.dirty.pop(station.dir):
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

    # ---- the reticulum verbs -----------------------------------------------

    async def name(self, station, name):
        return None                 # the daemon has no name to set

    async def role(self, station, role):
        if role != "transport":
            raise CommandError("a microReticulum station is always a transport node")

    async def radio(self, station, **figures):
        for name, key, scale in RADIO_KEYS:
            if figures.get(name) is not None:
                await self.run(station, "set %s %d" % (key, round(figures[name] * scale)))
        if figures.get("tx_dbm") is not None:
            await self.run(station, "txp %d" % round(figures["tx_dbm"]))

    async def tx_power(self, station, dbm):
        await self.run(station, "txp %d" % round(float(dbm)))

    async def lxmf_create(self, station, name):
        return None                 # it has no destination of its own, and makes none

    async def lxmf_identities(self, station):
        return []

    async def current_role(self, station):
        return "transport" if self.seen(station)[0] else None


DRIVER = Microreticulum
