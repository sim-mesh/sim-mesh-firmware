#!/usr/bin/env python3
"""The Meshtastic station's host: meshtasticd, and the client that talks to
its TCP API, in one station.

```
station console pty ◄──► host ── TCP <bind addr>:4403, 0x94 0xC3 len16 + ToRadio ──► meshtasticd (SX1262)
                          ├─ descriptor 0
                          │    ├─ framed RPC → a command → reply frame     for the driver
                          │    └─ every other byte → a command line        for a person
                          ├─ FromRadio → "mthost: {json}" lines            for the driver
                          └─ meshtasticd's stdout and stderr → "fw: …"     its log
```

The driver starts it with SIM_MESH_NODE_ID set to the host's own id in the
ether and MESHTASTIC_FIRMWARE_NODE_ID to the station's, which meshtasticd is
given. In a virtual-time run the host joins the ether as a station of its
own, with no radio, before anything waits on time.

One connection to the firmware, made once: when it is lost while the
firmware runs, or the firmware exits (a reboot), the host exits, and the
station is started again as a whole.
"""

import asyncio
import ctypes
import json
import os
import pty
import shlex
import signal
import struct
import sys
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAM = os.path.join(HERE, "meshtasticd")
PORT = 4403
HWID_OFFSET = 16                # node number = node id + 16; meshtasticd needs at least 4
START1, START2 = 0x94, 0xC3
MAX_FRAME = 512
RPC_MAGIC = b"\xf5SG\x01"
COMMAND_MAX = 4096
REPLY_MAX = 16384
STALL_S = 1.0                   # no progress this long abandons a frame
CONNECT_RETRY_S = 0.5
ANSWER_S = 4.0                  # a command's answer, on the run's clock
ADMIN_ANSWER_S = 30.0           # a setting's: the first region set makes keys, ~10 s of modem entropy
HEARTBEAT_S = 300.0
BROADCAST = 0xFFFFFFFF
UNKNOWN_HOP = 0xFFFFFFFF
RESERVED_NONCES = (69420, 69421)
ONLY_NODES = 69421              # a config request for the node database alone
MARKER = b"serial: framed rpc v1\r\n"
CHIP_MAX_DBM = 22

PORT_TEXT = 1
PORT_NODEINFO = 4
PORT_ROUTING = 5
PORT_ADMIN = 6
PORT_TRACEROUTE = 70


def say(text):
    """A line of the host's own, on the console."""
    print(text, flush=True)


def event(obj):
    say("mthost: " + json.dumps(obj, separators=(",", ":")))


def join_ether():
    """In a virtual-time run, this process is a station of its own in the
    ether (its id is SIM_MESH_NODE_ID), with no radio."""
    if os.environ.get("SIM_MESH_TIME") != "virtual":
        return
    lib = ctypes.CDLL(os.environ["SIM_MESH_RADIO_LIB"])
    lib.simradio_station_open.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p]
    lib.simradio_station_open.restype = ctypes.c_int
    if lib.simradio_station_open(int(os.environ["SIM_MESH_NODE_ID"]),
                                 os.environ["SIM_MESH_BIND_ADDR"].encode(),
                                 os.environ["SIM_MESH_ETHER"].encode()) != 0:
        say("mthost: cannot join the ether at %s" % os.environ["SIM_MESH_ETHER"])
        sys.exit(1)


if __name__ == "__main__":
    join_ether()

from google.protobuf import json_format  # noqa: E402
from meshtastic import admin_pb2, config_pb2, mesh_pb2  # noqa: E402

LORA = config_pb2.Config.LoRaConfig
ROLE = config_pb2.Config.DeviceConfig.Role


def leave(code):
    sys.stdout.flush()
    os._exit(code)


def die_with_parent():
    libc = ctypes.CDLL(None)
    libc.prctl(1, signal.SIGTERM)       # PR_SET_PDEATHSIG


def node_id(num):
    return "!%08x" % num


def config_yaml(max_dbm=CHIP_MAX_DBM):
    return ("Lora:\n"
            "  Module: sx1262\n"
            "  CS: 1\n"
            "  Reset: 2\n"
            "  Busy: 3\n"
            "  IRQ: 4\n"
            "  DIO2_AS_RF_SWITCH: true\n"
            "  DIO3_TCXO_VOLTAGE: true\n"
            "  SX126X_MAX_POWER: %d\n"
            "Logging:\n"
            "  LogLevel: info\n"
            "  AsciiLogs: true\n"
            "General:\n"
            "  MaxNodes: 200\n"
            "  MaxMessageQueue: 100\n" % max_dbm)


def frame(msg):
    data = msg.SerializeToString()
    return bytes((START1, START2, len(data) >> 8, len(data) & 0xFF)) + data


def unframe(buf):
    """(payloads, rest): the whole frames at the front of `buf`, anything
    else before them skipped, and what may still become one."""
    out = []
    while True:
        at = buf.find(bytes((START1,)))
        if at < 0:
            return out, b""
        buf = buf[at:]
        if len(buf) < 2:
            return out, buf
        if buf[1] != START2:
            buf = buf[1:]
            continue
        if len(buf) < 4:
            return out, buf
        n = (buf[2] << 8) | buf[3]
        if n > MAX_FRAME:
            buf = buf[1:]
            continue
        if len(buf) < 4 + n:
            return out, buf
        out.append(buf[4:4 + n])
        buf = buf[4 + n:]


def set_field(msg, key, text):
    """One `key=value` into a protobuf message, by the field's type."""
    field = msg.DESCRIPTOR.fields_by_name.get(key)
    if field is None:
        raise ValueError("no setting %r" % key)
    if field.enum_type is not None:
        value = field.enum_type.values_by_name.get(text.upper())
        if value is None:
            raise ValueError("%s: no %r (there are %s)" % (key, text, ", ".join(
                v.name for v in field.enum_type.values)))
        setattr(msg, key, value.number)
    elif field.type == field.TYPE_BOOL:
        setattr(msg, key, text.lower() in ("1", "true", "yes", "on"))
    elif field.type in (field.TYPE_FLOAT, field.TYPE_DOUBLE):
        setattr(msg, key, float(text))
    else:
        setattr(msg, key, int(text, 0))


class Firmware:
    """meshtasticd, its stdout and stderr on a pty of our own."""

    async def start(self):
        state = os.path.abspath("state")
        os.makedirs(state, exist_ok=True)
        conf = os.path.abspath("config.yaml")
        with open(conf, "w") as out:
            out.write(config_yaml())
        sid = int(os.environ.get("MESHTASTIC_FIRMWARE_NODE_ID", os.environ["SIM_MESH_NODE_ID"]))
        env = dict(os.environ, SIM_MESH_NODE_ID=str(sid))
        master, slave = pty.openpty()
        tty.setraw(slave)
        self.proc = await asyncio.create_subprocess_exec(
            PROGRAM, "-c", conf, "-d", state, "-h", str(sid + HWID_OFFSET),
            stdin=asyncio.subprocess.DEVNULL, stdout=slave, stderr=slave, env=env,
            preexec_fn=die_with_parent)
        os.close(slave)
        self.master = master
        self.partial = b""
        asyncio.get_running_loop().add_reader(master, self.readable)
        asyncio.create_task(self.watch())

    def readable(self):
        try:
            data = os.read(self.master, 4096)
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.master)
            return
        *lines, self.partial = (self.partial + data).split(b"\n")
        for line in lines:
            say("fw: " + line.rstrip(b"\r").decode("utf-8", "replace"))

    async def watch(self):
        code = await self.proc.wait()
        if self.partial:
            say("fw: " + self.partial.decode("utf-8", "replace"))
        say("mthost: the firmware exited (%s); restarting the station" % code)
        leave(0)


class Api:
    """The one connection to meshtasticd's TCP API."""

    def __init__(self, firmware=None):
        self.firmware = firmware
        self.reader = self.writer = None
        self.my_num = None
        self.nodes = {}             # number -> {name, id, hops_away, snr, last_heard}
        self.lora = LORA()
        self.device = config_pb2.Config.DeviceConfig()
        self.waiting = {}           # (kind, id) -> future

    # ---- the connection ---------------------------------------------------

    async def connect(self, host, port=PORT):
        while True:
            try:
                self.reader, self.writer = await asyncio.open_connection(host, port)
                return
            except OSError:
                await asyncio.sleep(CONNECT_RETRY_S)

    def send(self, to_radio):
        self.writer.write(frame(to_radio))

    async def read_loop(self):
        buf = b""
        while True:
            data = await self.reader.read(4096)
            if not data:
                break
            frames, buf = unframe(buf + data)
            for payload in frames:
                msg = mesh_pb2.FromRadio()
                try:
                    msg.ParseFromString(payload)
                except Exception:   # noqa: BLE001 - a frame that is no FromRadio is skipped
                    continue
                self.take(msg)
        if self.firmware is not None and self.firmware.proc.returncode is None:
            # The firmware closes it as it reboots: its exit restarts the station.
            try:
                await asyncio.wait_for(self.firmware.proc.wait(), 30)
            except asyncio.TimeoutError:
                say("mthost: lost the firmware's API while it runs; restarting the station")
                leave(0)

    async def handshake(self):
        nonce = 0
        while nonce in (0,) + RESERVED_NONCES:
            nonce = int.from_bytes(os.urandom(4), "little")
        fut = self.expect(("config", nonce))
        self.send(mesh_pb2.ToRadio(want_config_id=nonce))
        await fut

    async def heartbeat(self):
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            self.send(mesh_pb2.ToRadio(heartbeat=mesh_pb2.Heartbeat(nonce=0)))

    # ---- what the firmware says -------------------------------------------

    def take(self, msg):
        which = msg.WhichOneof("payload_variant")
        if which == "my_info":
            self.my_num = msg.my_info.my_node_num
        elif which == "node_info":
            self.node_from_info(msg.node_info)
        elif which == "config":
            kind = msg.config.WhichOneof("payload_variant")
            if kind == "lora":
                self.lora.CopyFrom(msg.config.lora)
            elif kind == "device":
                self.device.CopyFrom(msg.config.device)
        elif which == "config_complete_id":
            self.resolve(("config", msg.config_complete_id), True)
        elif which == "queueStatus":
            self.resolve(("queued", msg.queueStatus.mesh_packet_id), msg.queueStatus.res)
        elif which == "packet":
            self.packet(msg.packet)

    def node_from_info(self, info):
        node = self.nodes.setdefault(info.num, {"num": info.num, "id": node_id(info.num)})
        if info.HasField("user"):
            node["name"] = info.user.long_name
        node["hops_away"] = info.hops_away if info.HasField("hops_away") else None
        node["snr"] = round(info.snr, 2)
        node["last_heard"] = info.last_heard

    def packet(self, p):
        if p.WhichOneof("payload_variant") != "decoded":
            return
        frm = getattr(p, "from")
        d = p.decoded
        if frm and frm != self.my_num:
            node = self.nodes.setdefault(frm, {"num": frm, "id": node_id(frm)})
            node["snr"] = round(p.rx_snr, 2)
            node["last_heard"] = p.rx_time
            if p.hop_start:
                node["hops_away"] = p.hop_start - p.hop_limit
        if d.portnum == PORT_NODEINFO and frm:
            user = mesh_pb2.User()
            try:
                user.ParseFromString(d.payload)
                self.nodes.setdefault(frm, {"num": frm, "id": node_id(frm)})["name"] = user.long_name
            except Exception:   # noqa: BLE001 - not a User, nothing learnt
                pass
        elif d.portnum == PORT_ROUTING and d.request_id:
            routing = mesh_pb2.Routing()
            try:
                routing.ParseFromString(d.payload)
            except Exception:   # noqa: BLE001
                return
            error = mesh_pb2.Routing.Error.Name(routing.error_reason)
            self.resolve(("routed", d.request_id), error)
            event({"event": "routing", "id": d.request_id, "from": node_id(frm), "error": error})
        elif d.portnum == PORT_TEXT and frm != self.my_num:
            event({"event": "recv", "from": node_id(frm),
                   "to": "^all" if p.to == BROADCAST else node_id(p.to), "ch": p.channel,
                   "text": d.payload.decode("utf-8", "replace"), "snr": round(p.rx_snr, 2),
                   "rssi": p.rx_rssi,
                   "hops": p.hop_start - p.hop_limit if p.hop_start else None})
        elif d.portnum == PORT_TRACEROUTE and d.request_id:
            route = mesh_pb2.RouteDiscovery()
            try:
                route.ParseFromString(d.payload)
            except Exception:   # noqa: BLE001
                return
            event({"event": "traceroute", "id": d.request_id,
                   "route": [self.hop(n) for n in route.route],
                   "snr_towards": [s / 4 for s in route.snr_towards],
                   "route_back": [self.hop(n) for n in route.route_back],
                   "snr_back": [s / 4 for s in route.snr_back]})

    def hop(self, num):
        if num == UNKNOWN_HOP:
            return None
        return self.nodes.get(num, {}).get("name") or node_id(num)

    # ---- waiting on an answer ---------------------------------------------

    def expect(self, key):
        fut = asyncio.get_running_loop().create_future()
        self.waiting[key] = fut
        return fut

    def resolve(self, key, value):
        fut = self.waiting.pop(key, None)
        if fut is not None and not fut.done():
            fut.set_result(value)

    async def answer(self, key, fut, bound=None):
        try:
            return await asyncio.wait_for(fut, ANSWER_S if bound is None else bound)
        except asyncio.TimeoutError:
            self.waiting.pop(key, None)
            raise CommandFailed("no answer from the firmware") from None

    # ---- sending ----------------------------------------------------------

    @staticmethod
    def new_id():
        while True:
            pid = int.from_bytes(os.urandom(4), "little")
            if pid:
                return pid

    def dest(self, name):
        if name == "^all":
            return BROADCAST
        if name.startswith("!"):
            try:
                return int(name[1:], 16)
            except ValueError:
                raise CommandFailed("unknown node %s" % name) from None
        for num, node in self.nodes.items():
            if node.get("name") == name and num != self.my_num:
                return num
        raise CommandFailed("unknown node %s" % name)

    def packet_out(self, to, portnum, payload, channel=0, want_ack=False, want_response=False):
        pid = self.new_id()
        p = mesh_pb2.MeshPacket(to=to, channel=channel, id=pid, want_ack=want_ack)
        p.decoded.portnum = portnum
        p.decoded.payload = payload
        p.decoded.want_response = want_response
        return pid, p

    async def admin(self, **fields):
        """An AdminMessage to the station itself, answered by its routing
        reply: the firmware has done what it says by then."""
        msg = admin_pb2.AdminMessage(**fields)
        pid, p = self.packet_out(self.my_num, PORT_ADMIN, msg.SerializeToString(),
                                 want_response=True)
        fut = self.expect(("routed", pid))
        self.send(mesh_pb2.ToRadio(packet=p))
        error = await self.answer(("routed", pid), fut, ADMIN_ANSWER_S)
        if error != "NONE":
            raise CommandFailed(error)
        return {}

    # ---- the console's commands -------------------------------------------

    async def command(self, line):
        """One command line: its reply, a JSON object or list."""
        try:
            args = shlex.split(line)
        except ValueError as err:
            raise CommandFailed(str(err)) from None
        if not args:
            raise CommandFailed("no command")
        verb, rest = args[0], args[1:]
        handler = COMMANDS.get(verb)
        if handler is None:
            raise CommandFailed("unknown command %s (there are %s)" % (verb, ", ".join(COMMANDS)))
        return await handler(self, rest)

    async def c_info(self, rest):
        own = self.nodes.get(self.my_num, {})
        return {"num": self.my_num, "id": node_id(self.my_num or 0), "name": own.get("name"),
                "role": ROLE.Name(self.device.role),
                "lora": json_format.MessageToDict(self.lora, preserving_proto_field_name=True,
                                                  always_print_fields_with_no_presence=True),
                "hop_limit": self.lora.hop_limit}

    async def c_edit(self, rest):
        if rest == ["begin"]:
            return await self.admin(begin_edit_settings=True)
        if rest == ["commit"]:
            return await self.admin(commit_edit_settings=True)
        raise CommandFailed("edit begin | edit commit")

    async def c_owner(self, rest):
        if len(rest) != 2:
            raise CommandFailed("owner <long name> <short name>")
        return await self.admin(set_owner=mesh_pb2.User(long_name=rest[0], short_name=rest[1]))

    async def c_lora(self, rest):
        lora = LORA()
        lora.CopyFrom(self.lora)
        try:
            for each in rest:
                key, _, value = each.partition("=")
                set_field(lora, key, value)
        except ValueError as err:
            raise CommandFailed(str(err)) from None
        got = await self.admin(set_config=config_pb2.Config(lora=lora))
        self.lora.CopyFrom(lora)
        return got

    async def c_device(self, rest):
        device = config_pb2.Config.DeviceConfig()
        device.CopyFrom(self.device)
        try:
            for each in rest:
                key, _, value = each.partition("=")
                if key != "role":
                    raise ValueError("device takes role=<ROLE>")
                set_field(device, key, value)
        except ValueError as err:
            raise CommandFailed(str(err)) from None
        got = await self.admin(set_config=config_pb2.Config(device=device))
        self.device.CopyFrom(device)
        return got

    async def c_send(self, rest):
        if len(rest) < 4:
            raise CommandFailed("send <dest> <ch> <ack 0/1> <text…>")
        to = self.dest(rest[0])
        try:
            ch, ack = int(rest[1]), rest[2] not in ("0", "false")
        except ValueError:
            raise CommandFailed("send <dest> <ch> <ack 0/1> <text…>") from None
        text = " ".join(rest[3:]).encode("utf-8")
        pid, p = self.packet_out(to, PORT_TEXT, text, channel=ch, want_ack=ack)
        fut = self.expect(("queued", pid))
        self.send(mesh_pb2.ToRadio(packet=p))
        res = await self.answer(("queued", pid), fut)
        if res:
            raise CommandFailed("refused (%d)" % res)
        return {"id": pid, "res": res, "to": "^all" if to == BROADCAST else node_id(to)}

    async def c_traceroute(self, rest):
        if len(rest) != 1:
            raise CommandFailed("traceroute <dest>")
        to = self.dest(rest[0])
        pid, p = self.packet_out(to, PORT_TRACEROUTE, mesh_pb2.RouteDiscovery().SerializeToString(),
                                 want_response=True)
        fut = self.expect(("queued", pid))
        self.send(mesh_pb2.ToRadio(packet=p))
        res = await self.answer(("queued", pid), fut)
        if res:
            raise CommandFailed("refused (%d)" % res)
        return {"id": pid}

    async def c_nodes(self, rest):
        # The firmware's node database as it is now, which can know a node
        # this connection has seen no packet of.
        fut = self.expect(("config", ONLY_NODES))
        self.send(mesh_pb2.ToRadio(want_config_id=ONLY_NODES))
        await self.answer(("config", ONLY_NODES), fut)
        return [dict(n, name=n.get("name")) for num, n in sorted(self.nodes.items())
                if num != self.my_num]

    async def c_nodeinfo(self, rest):
        self.send(mesh_pb2.ToRadio(heartbeat=mesh_pb2.Heartbeat(nonce=1)))
        return {}


COMMANDS = {"info": Api.c_info, "edit": Api.c_edit, "owner": Api.c_owner, "lora": Api.c_lora,
            "device": Api.c_device, "send": Api.c_send, "traceroute": Api.c_traceroute,
            "nodes": Api.c_nodes, "nodeinfo": Api.c_nodeinfo}


class CommandFailed(Exception):
    pass


async def reply_to(api, line):
    """A command's reply, one JSON line; an error as {"error": …}."""
    try:
        got = await api.command(line)
    except CommandFailed as err:
        got = {"error": str(err)}
    except Exception as err:    # noqa: BLE001 - the reply says what went wrong
        got = {"error": "%s: %s" % (type(err).__name__, err)}
    return json.dumps(got, separators=(",", ":")) + "\n"


class Console:
    """Descriptor 0: framed RPC frames to the driver's queue, every other
    byte to a command line a person types."""

    def __init__(self, api, fd_in=0, fd_out=1):
        self.api = api
        self.fd_in, self.fd_out = fd_in, fd_out
        self.frames = asyncio.Queue()
        self.buf = b""
        self.line = b""
        self.last = 0.0
        loop = asyncio.get_running_loop()
        if os.isatty(fd_in):
            tty.setraw(fd_in)
        loop.add_reader(fd_in, self.readable)
        asyncio.create_task(self.answer())

    def readable(self):
        try:
            data = os.read(self.fd_in, 4096)
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.fd_in)
            return
        now = asyncio.get_running_loop().time()
        if self.buf and now - self.last > STALL_S:
            if not self.buf.startswith(RPC_MAGIC):
                self.typed(self.buf)
            self.buf = b""
        self.last = now
        self.buf += data
        self.take()

    def take(self):
        passed = b""
        buf = self.buf
        while buf:
            at = buf.find(RPC_MAGIC[0:1])
            if at < 0:
                passed += buf
                buf = b""
                break
            passed += buf[:at]
            buf = buf[at:]
            if not RPC_MAGIC.startswith(buf[:4]):
                passed += buf[:1]
                buf = buf[1:]
                continue
            if len(buf) < 7:
                break
            n = (buf[5] << 8) | buf[6]
            if len(buf) < 7 + n:
                break
            self.frames.put_nowait((buf[4], buf[7:7 + n], n))
            buf = buf[7 + n:]
        self.buf = buf
        if passed:
            self.typed(passed)

    def typed(self, data):
        """A person's keys: echoed, and each line run as a command."""
        for b in data:
            if b in (0x0D, 0x0A):
                os.write(self.fd_out, b"\r\n")
                line, self.line = self.line.decode("utf-8", "replace").strip(), b""
                if line:
                    asyncio.create_task(self.person(line))
            elif b in (0x08, 0x7F):
                if self.line:
                    self.line = self.line[:-1]
                    os.write(self.fd_out, b"\b \b")
            elif b >= 0x20:
                self.line += bytes((b,))
                os.write(self.fd_out, bytes((b,)))

    async def person(self, line):
        os.write(self.fd_out, (await reply_to(self.api, line)).replace("\n", "\r\n").encode())

    async def answer(self):
        while True:
            frame_id, payload, n = await self.frames.get()
            if n > COMMAND_MAX:
                reply = json.dumps({"error": "command over 4096 bytes"}) + "\n"
            else:
                reply = await reply_to(self.api, payload.decode("utf-8", "replace").strip())
            out = reply.encode("utf-8", "replace")
            if len(out) > REPLY_MAX:
                out = out[:out.rfind(b"\n", 0, REPLY_MAX) + 1]
            os.write(self.fd_out, RPC_MAGIC + bytes((frame_id, len(out) >> 8, len(out) & 0xFF))
                     + out)


async def main():
    firmware = Firmware()
    await firmware.start()
    api = Api(firmware)
    await api.connect(os.environ["SIM_MESH_BIND_ADDR"])
    reading = asyncio.create_task(api.read_loop())
    await api.handshake()
    Console(api)
    asyncio.create_task(api.heartbeat())
    os.write(1, MARKER)
    await reading
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    leave(0)
