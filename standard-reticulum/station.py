#!/usr/bin/env python3
"""A standard Reticulum node on sim-mesh: the RNode firmware as a Linux process,
and Reticulum's Python reference implementation with an LXMF router behind
it, as a person runs rnsd and an LXMF client on a computer with an RNode on
its USB port.

    station.py ── spawns ──► rnode        the RNode firmware, its KISS host port at
                                          SIM_MESH_BIND_ADDR:7633, the chip model below
    station.py ── RNodeInterface, tcp://SIM_MESH_BIND_ADDR ──► rnode
    testbed ── framed RPC on the console ──► station.py

Both processes join a virtual-time run, each as a station of the ether's:
the RNode as the node's own id (SR_RNODE_ID), which is the radio the loss
tables know, and this process as its own id (SIM_MESH_NODE_ID), a station with
no radio that reads the console. The ether then holds T for the KISS bytes
between them as for any TCP between two stations.

Settings live in `state/settings.json`, and Reticulum and the router read
them when this process starts: a `set` that changes one says so in `status`
(`pending: yes`), and the driver's flush restarts the station to apply it.

The console speaks framed RPC (sim-mesh's testbed/rpc.py) and, for a person,
one command per line:

    show s.net.hostname    the name, as `<key> = <value>`
    set <key> <value>      name, freq_hz, bw_hz, sf, cr, txpower (the chip's
                           dBm), transport (1 or 0), loglevel
    lxmf create <name>     the LXMF display name (set name <name>)
    status                 `state: starting|up` and `pending: yes|no`
    addr | lxmf            `lxmf.delivery : <hash>`
    transport              `transport: on|off`
    announce               announce the LXMF delivery destination
    send <dest> <text>     an LXMF message, DIRECT: `queued <mid>`
    path [<dest>] [-i <iface>]
                           the path table, `{"paths": [{dest, next_hop, iface,
                           hops}]}`: that destination's and that interface's
                           when given, all without
    paths                  `<N> paths total`
    restart                exit, for the supervisor to start it again

An outbound message's fate is logged against its mid, with the log's stamp
on the node's wall clock: `lxmf: queued mid=o_…`, then `lxmf: DIRECT
delivered mid=o_…` (`DIRECT resource delivered` for one sent as a resource)
or `lxmf: failed mid=o_… <why>`.
"""

import ctypes
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time

KISS_PORT = 7633
RPC_MAGIC = b"\xf5\x53\x47\x01"
RPC_HEADER = len(RPC_MAGIC) + 3
MARKER = "[serial] framed rpc v1"
PR_SET_PDEATHSIG = 1
IDENTITY_WAIT_S = 60.0      # how long a send waits for the recipient's identity
PATH_ASK_S = 15.0           # and how often it asks for a path meanwhile
LISTEN_POLL_S = 0.05
SETTING_TYPES = {"name": str, "freq_hz": int, "bw_hz": int, "sf": int, "cr": int,
                 "txpower": int, "transport": int, "loglevel": int}
RADIO_KEYS = ("freq_hz", "bw_hz", "sf", "cr", "txpower")
DEFAULTS = {"name": None, "transport": 0, "loglevel": 3}

HERE = os.getcwd()
STATE = os.path.join(HERE, "state")
SETTINGS = os.path.join(STATE, "settings.json")

out_lock = threading.Lock()


def emit(data):
    """Bytes onto the console, whole: a frame or a line never interleaves
    with another thread's."""
    with out_lock:
        view = memoryview(data)
        while view:
            try:
                n = os.write(1, view)
            except InterruptedError:
                continue
            view = view[n:]


def stamp():
    now = time.time()
    return time.strftime("%b %d %H:%M:%S", time.gmtime(now)) + ".%03d" % (int(now * 1000) % 1000)


def log(text):
    emit(("%s %s\n" % (stamp(), text)).encode("utf-8", "replace"))


def load_settings():
    try:
        with open(SETTINGS) as f:
            return dict(DEFAULTS, **json.load(f))
    except FileNotFoundError:
        return dict(DEFAULTS)


def save_settings(settings):
    os.makedirs(STATE, exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=1, sort_keys=True)
    os.replace(tmp, SETTINGS)


# ---- the RNode ---------------------------------------------------------------

def rnode_conf(addr, node_id):
    """rnoded.conf for the RNode: the chip on sim-mesh's pins, its KISS host
    port on this station's address, and an exit for a reboot."""
    lines = [("data_dir", "./state/rnode"), ("modem", "SX1262"),
             ("pin_cs", 1), ("pin_reset", 2), ("pin_busy", 3), ("pin_dio", 4)]
    lines += [(key, -1) for key in ("pin_rxen", "pin_txen", "pin_tcxo_enable",
                                    "pin_led_rx", "pin_led_tx")]
    lines += [("kiss_tcp_port", KISS_PORT), ("kiss_tcp_bind", addr),
              ("reboot_mode", "exit"), ("device_id", "sim-mesh-%s" % node_id)]
    return "".join("%s = %s\n" % kv for kv in lines)


def die_with_parent():
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


def start_rnode(addr):
    node_id = os.environ["SR_RNODE_ID"]
    conf = os.path.join(HERE, "rnoded.conf")
    with open(conf, "w") as f:
        f.write(rnode_conf(addr, node_id))
    os.makedirs(os.path.join(STATE, "rnode"), exist_ok=True)
    env = dict(os.environ, SIM_MESH_NODE_ID=node_id, MR_CONFIG=conf,
               MR_DATA_DIR=os.path.join(STATE, "rnode"),
               SIMRADIO_PIN_NSS="1", SIMRADIO_PIN_RESET="2",
               SIMRADIO_PIN_BUSY="3", SIMRADIO_PIN_DIO1="4")
    return subprocess.Popen([os.environ["SR_RNODE"]], env=env, stdin=subprocess.DEVNULL,
                            preexec_fn=die_with_parent)


def listening(addr, port):
    """Whether something listens at addr:port, read from /proc/net/tcp: a
    probe connection would take the RNode's one host slot."""
    want = "%08X:%04X" % (int.from_bytes(bytes(int(p) for p in addr.split(".")), "little"), port)
    try:
        with open("/proc/net/tcp") as f:
            next(f)
            return any(line.split()[1] == want and line.split()[3] == "0A" for line in f)
    except (OSError, StopIteration):
        return False


# ---- Reticulum and LXMF ---------------------------------------------------------

def rns_config(settings, addr):
    radio = all(settings.get(key) is not None for key in RADIO_KEYS)
    text = ["[reticulum]",
            "  enable_transport = %s" % ("Yes" if settings.get("transport") else "No"),
            "  share_instance = No",
            "  panic_on_interface_error = No",
            "[logging]",
            "  loglevel = %d" % settings["loglevel"],
            "[interfaces]"]
    if radio:
        text += ["  [[RNode LoRa]]",
                 "    type = RNodeInterface",
                 "    enabled = yes",
                 "    port = tcp://%s" % addr,
                 "    frequency = %d" % settings["freq_hz"],
                 "    bandwidth = %d" % settings["bw_hz"],
                 "    txpower = %d" % settings["txpower"],
                 "    spreadingfactor = %d" % settings["sf"],
                 "    codingrate = %d" % settings["cr"]]
    return "\n".join(text) + "\n", radio


class Node:
    """Reticulum, the LXMF router and the delivery destination, once up."""

    def __init__(self, settings):
        self.settings = settings        # what this start was made with
        self.up = False
        self.RNS = self.LXMF = None
        self.router = self.delivery = None

    def start(self, addr):
        import RNS
        import LXMF
        self.RNS, self.LXMF = RNS, LXMF
        configdir = os.path.join(STATE, "rns")
        os.makedirs(configdir, exist_ok=True)
        config, radio = rns_config(self.settings, addr)
        with open(os.path.join(configdir, "config"), "w") as f:
            f.write(config)
        if radio:
            while not listening(addr, KISS_PORT):
                time.sleep(LISTEN_POLL_S)
        RNS.logtimestamps = False
        RNS.Reticulum(configdir=configdir, loglevel=self.settings["loglevel"],
                      logdest=lambda line: log("rns: " + line))
        ident_path = os.path.join(STATE, "identity")
        identity = RNS.Identity.from_file(ident_path) if os.path.exists(ident_path) else None
        if identity is None:
            identity = RNS.Identity()
            identity.to_file(ident_path)
        self.router = LXMF.LXMRouter(identity=identity, storagepath=os.path.join(STATE, "lxmf"))
        self.router.register_delivery_callback(self.received)
        self.delivery = self.router.register_delivery_identity(
            identity, display_name=self.settings.get("name") or None)
        self.up = True
        log("lxmf: delivery %s" % RNS.hexrep(self.delivery.hash, delimit=False))

    def received(self, message):
        log("lxmf: received %d bytes from %s" % (
            len(message.content or b""), self.RNS.hexrep(message.source_hash, delimit=False)))

    def send(self, dest_hex, text):
        RNS, LXMF = self.RNS, self.LXMF
        dest = bytes.fromhex(dest_hex)
        if len(dest) != RNS.Reticulum.TRUNCATED_HASHLENGTH // 8:
            raise ValueError("a destination is %d hex digits" % (RNS.Reticulum.TRUNCATED_HASHLENGTH // 4))
        mid = "o_" + os.urandom(6).hex()
        log("lxmf: queued mid=%s to %s, %d bytes" % (mid, dest_hex, len(text)))
        threading.Thread(target=self.deliver, args=(mid, dest, text), daemon=True).start()
        return mid

    def deliver(self, mid, dest, text):
        RNS, LXMF = self.RNS, self.LXMF
        identity = RNS.Identity.recall(dest)
        waited = 0.0
        while identity is None and waited < IDENTITY_WAIT_S:
            if waited % PATH_ASK_S == 0:
                RNS.Transport.request_path(dest)
            time.sleep(1.0)
            waited += 1.0
            identity = RNS.Identity.recall(dest)
        if identity is None:
            log("lxmf: failed mid=%s no identity for the recipient" % mid)
            return
        destination = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
                                      "lxmf", "delivery")
        message = LXMF.LXMessage(destination, self.delivery, text, title="",
                                 desired_method=LXMF.LXMessage.DIRECT)

        def delivered(m):
            how = "resource delivered" if m.representation == LXMF.LXMessage.RESOURCE else "delivered"
            log("lxmf: DIRECT %s mid=%s" % (how, mid))

        def failed(m):
            log("lxmf: failed mid=%s state 0x%02x" % (mid, m.state))

        message.register_delivery_callback(delivered)
        message.register_failed_callback(failed)
        self.router.handle_outbound(message)

    def path(self, dest_hex=None, iface=None):
        """The path table, as {"paths": [{dest, next_hop, iface, hops}]}: the
        entries for `dest_hex` and on `iface` when given, all without."""
        RNS = self.RNS
        T = RNS.Transport
        out = []
        for dest, entry in list(T.path_table.items()):
            name = RNS.hexrep(dest, delimit=False)
            via = entry[T.IDX_PT_RVCD_IF]
            via_name = getattr(via, "name", None) or str(via)
            if (dest_hex and name != dest_hex) or (iface and via_name != iface):
                continue
            out.append({"dest": name, "next_hop": RNS.hexrep(entry[T.IDX_PT_NEXT_HOP], delimit=False),
                        "iface": via_name, "hops": entry[T.IDX_PT_HOPS]})
        return json.dumps({"paths": out})


# ---- the console ----------------------------------------------------------------

class Console:
    """Frames and lines off the console, each command run and answered."""

    def __init__(self, node, settings):
        self.node = node
        self.settings = settings        # as saved, which a restart applies
        self.buf = b""                  # console bytes not yet taken
        self.typed = b""                # a person's line not yet ended

    def pending(self):
        return any(self.settings.get(k) != self.node.settings.get(k) for k in SETTING_TYPES)

    def command(self, line):
        words = line.split(None, 2)
        if not words:
            return ""
        verb, node = words[0], self.node
        if verb == "show" and len(words) == 2:
            if words[1] == "s.net.hostname":
                return "s.net.hostname = %s" % (self.settings.get("name") or "")
            return "no such key %s" % words[1]
        if verb == "set" and len(words) == 3 and words[1] in SETTING_TYPES:
            key = words[1]
            try:
                self.settings[key] = SETTING_TYPES[key](words[2].strip())
            except ValueError:
                return "%s takes a %s" % (key, SETTING_TYPES[key].__name__)
            save_settings(self.settings)
            return "%s = %s%s" % (key, self.settings[key],
                                  " (applies at restart)" if self.pending() else "")
        if verb == "lxmf" and len(words) == 3 and words[1] == "create":
            return self.command("set name %s" % words[2])
        if verb == "status":
            return "state: %s\npending: %s" % ("up" if node.up else "starting",
                                              "yes" if self.pending() else "no")
        if verb == "restart":
            restart()
        if not node.up:
            return "not up yet"
        if verb in ("addr", "lxmf") and len(words) == 1:
            return "lxmf.delivery : %s" % node.RNS.hexrep(node.delivery.hash, delimit=False)
        if verb == "transport":
            return "transport: %s" % ("on" if node.RNS.Reticulum.transport_enabled() else "off")
        if verb == "announce":
            node.router.announce(node.delivery.hash)
            return "announced %s" % node.RNS.hexrep(node.delivery.hash, delimit=False)
        if verb == "send" and len(words) == 3:
            return "queued %s" % node.send(words[1], words[2])
        if verb == "path":
            rest = line.split()[1:]
            iface = None
            if "-i" in rest and rest.index("-i") + 1 < len(rest):
                at = rest.index("-i")
                iface, rest = rest[at + 1], rest[:at] + rest[at + 2:]
            return node.path(rest[0] if rest else None, iface)
        if verb == "paths":
            return "%d paths total" % len(node.RNS.Transport.path_table)
        return "unknown command: %s" % line

    def answer(self, line):
        try:
            return self.command(line)
        except Exception as err:        # a command's failure is its answer
            return "error: %s" % err

    def feed(self, data):
        self.buf += data
        while self.buf:
            at = self.buf.find(RPC_MAGIC)
            text = self.buf if at < 0 else self.buf[:at]
            if at < 0:
                # A magic may be arriving in pieces: keep a tail that could be its start.
                keep = next((n for n in range(len(RPC_MAGIC) - 1, 0, -1)
                             if self.buf.endswith(RPC_MAGIC[:n])), 0)
                text = self.buf[:len(self.buf) - keep]
            self.lines(text)
            self.buf = self.buf[len(text):]
            if at < 0 or len(self.buf) < RPC_HEADER:
                return
            fid, n = self.buf[4], (self.buf[5] << 8) | self.buf[6]
            if len(self.buf) < RPC_HEADER + n:
                return
            command = self.buf[RPC_HEADER:RPC_HEADER + n].decode("utf-8", "replace")
            self.buf = self.buf[RPC_HEADER + n:]
            reply = self.answer(command.strip()).encode("utf-8", "replace")[:0xFFFF]
            emit(RPC_MAGIC + bytes((fid, len(reply) >> 8, len(reply) & 0xFF)) + reply)

    def lines(self, text):
        """What a person typed: a command per line, its answer printed."""
        self.typed += text.replace(b"\x03", b"")
        *whole, self.typed = self.typed.replace(b"\r", b"\n").split(b"\n")
        for raw in whole:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                emit(("%s\n" % self.answer(line)).encode("utf-8", "replace"))

    def run(self):
        while True:
            try:
                data = os.read(0, 4096)
            except InterruptedError:
                continue
            except OSError:
                data = b""
            if not data:
                os._exit(0)
            self.feed(data)


def restart():
    """Out, for the supervisor to start the station again, with what Reticulum
    keeps written first."""
    try:
        import RNS
        RNS.Reticulum.exit_handler()
    except Exception:
        pass
    os._exit(0)


def main():
    addr = os.environ["SIM_MESH_BIND_ADDR"]
    if not os.environ.get("SR_RNODE"):
        sys.exit("station: no RNode: SR_RNODE names none")
    rnode = start_rnode(addr)
    if os.environ.get("SIM_MESH_TIME") == "virtual":
        # Joins the run as a station without a radio, through the radio
        # library sim-mesh provides.
        lib = ctypes.CDLL(os.environ["SIM_MESH_RADIO_LIB"])
        lib.simradio_station_open.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)
        if lib.simradio_station_open(int(os.environ["SIM_MESH_NODE_ID"]), addr.encode(),
                                     os.environ.get("SIM_MESH_ETHER", "").encode()) != 0:
            sys.exit("station: no link to the ether")
    settings = load_settings()
    node = Node(dict(settings))
    console = Console(node, settings)
    emit((MARKER + "\n").encode())
    threading.Thread(target=console.run, daemon=True).start()
    node.start(addr)
    pidfd = os.pidfd_open(rnode.pid)
    while True:
        try:
            ready, _, _ = select.select([pidfd], [], [])
        except InterruptedError:
            continue
        if ready:
            log("station: the RNode exited (%s)" % rnode.wait())
            restart()


if __name__ == "__main__":
    main()
