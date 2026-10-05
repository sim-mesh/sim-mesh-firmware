"""host.py against a fake meshtasticd: an asyncio TCP server on loopback
that speaks FromRadio.

The protobuf bindings and runtime are built once into .pylib (make_zip's
build_pylib), as the zip carries them.
"""

import asyncio
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PYLIB = os.path.join(HERE, ".pylib")
if not os.path.isdir(os.path.join(PYLIB, "meshtastic")):
    sys.path.insert(0, HERE)
    import make_zip  # noqa: E402
    make_zip.build_pylib(PYLIB)
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
sys.path.insert(0, PYLIB)
sys.path.insert(0, HERE)

import host  # noqa: E402
from meshtastic import admin_pb2, config_pb2, mesh_pb2  # noqa: E402

OWN, BRAVO, CHARLIE = 20, 21, 22


class FakeFirmware:
    """What meshtasticd says on its API, and what it was sent."""

    def __init__(self):
        self.got = []
        self.writer = None
        self.server = None
        self.refuse_text = False

    async def start(self):
        self.server = await asyncio.start_server(self.client, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    def out(self, **fields):
        self.writer.write(host.frame(mesh_pb2.FromRadio(**fields)))

    def packet(self, frm, to, portnum, payload, **fields):
        p = mesh_pb2.MeshPacket(to=to, **fields)
        setattr(p, "from", frm)
        p.decoded.portnum = portnum
        p.decoded.payload = payload
        return p

    def routing(self, frm, request_id, error="NONE"):
        r = mesh_pb2.Routing(error_reason=mesh_pb2.Routing.Error.Value(error))
        p = self.packet(frm, OWN, host.PORT_ROUTING, r.SerializeToString())
        p.decoded.request_id = request_id
        self.out(packet=p)

    async def client(self, reader, writer):
        self.writer = writer
        buf = b""
        while True:
            data = await reader.read(4096)
            if not data:
                return
            frames, buf = host.unframe(buf + data)
            for payload in frames:
                msg = mesh_pb2.ToRadio()
                msg.ParseFromString(payload)
                self.got.append(msg)
                self.answer(msg)
            await writer.drain()

    def answer(self, msg):
        which = msg.WhichOneof("payload_variant")
        if which == "want_config_id" and msg.want_config_id == host.ONLY_NODES:
            self.out(node_info=mesh_pb2.NodeInfo(
                num=CHARLIE, user=mesh_pb2.User(long_name="charlie", short_name="char"),
                hops_away=2))
            self.out(config_complete_id=host.ONLY_NODES)
        elif which == "want_config_id":
            # Log text and a broken frame first: the reader resynchronises.
            self.writer.write(b"INFO  | some log line\r\n\x94\x00junk\x94")
            self.out(my_info=mesh_pb2.MyNodeInfo(my_node_num=OWN))
            for num, name in ((OWN, "alpha"), (BRAVO, "bravo")):
                self.out(node_info=mesh_pb2.NodeInfo(
                    num=num, user=mesh_pb2.User(long_name=name, short_name=name[:4]),
                    snr=6.25, last_heard=1000, hops_away=1 if num == BRAVO else 0))
            lora = config_pb2.Config.LoRaConfig(use_preset=True, hop_limit=3, region=3)
            self.out(config=config_pb2.Config(lora=lora))
            self.out(config=config_pb2.Config(device=config_pb2.Config.DeviceConfig(role=0)))
            self.out(config_complete_id=msg.want_config_id)
        elif which == "packet":
            p = msg.packet
            port = p.decoded.portnum
            if port == host.PORT_ADMIN:
                assert p.to == OWN and p.decoded.want_response
                admin = admin_pb2.AdminMessage()
                admin.ParseFromString(p.decoded.payload)
                self.routing(OWN, p.id, "BAD_REQUEST" if admin.HasField("set_owner")
                             and admin.set_owner.long_name == "bad" else "NONE")
            elif port == host.PORT_TEXT:
                self.out(queueStatus=mesh_pb2.QueueStatus(res=3 if self.refuse_text else 0,
                                                          mesh_packet_id=p.id))
                if p.want_ack and p.to != host.BROADCAST and not self.refuse_text:
                    self.routing(p.to, p.id)
            elif port == host.PORT_TRACEROUTE:
                self.out(queueStatus=mesh_pb2.QueueStatus(res=0, mesh_packet_id=p.id))
                rd = mesh_pb2.RouteDiscovery(route=[BRAVO, 0xFFFFFFFF], snr_towards=[24, -8, 10],
                                             route_back=[BRAVO], snr_back=[12, 6])
                reply = self.packet(p.to, OWN, host.PORT_TRACEROUTE, rd.SerializeToString())
                reply.decoded.request_id = p.id
                self.out(packet=reply)


@pytest.fixture
def said(monkeypatch):
    lines = []
    monkeypatch.setattr(host, "say", lines.append)
    return lines


def events(lines):
    return [json.loads(line[len("mthost: "):]) for line in lines if line.startswith("mthost: {")]


async def connected(monkeypatch=None):
    fake = FakeFirmware()
    port = await fake.start()
    api = host.Api()
    await api.connect("127.0.0.1", port)
    reading = asyncio.create_task(api.read_loop())
    await asyncio.wait_for(api.handshake(), 5)
    return fake, api, reading


def test_framing_resynchronises_on_the_magic():
    good = host.frame(mesh_pb2.ToRadio(want_config_id=7))
    frames, rest = host.unframe(b"noise\x94\x94\xc3\xff\xff" + good + good[:3])
    assert frames == [good[4:]] and rest == good[:3]
    frames, rest = host.unframe(rest + good[3:])
    assert frames == [good[4:]] and rest == b""
    assert host.unframe(b"plain text only")[0] == []


def test_the_handshake_and_every_command(said):
    async def go():
        fake, api, reading = await connected()
        assert api.my_num == OWN and api.nodes[BRAVO]["name"] == "bravo"
        info = json.loads(await host.reply_to(api, "info"))
        assert info["num"] == OWN and info["id"] == "!00000014" and info["name"] == "alpha"
        assert info["role"] == "CLIENT" and info["hop_limit"] == 3
        assert info["lora"]["region"] == "EU_868"

        assert json.loads(await host.reply_to(api, "edit begin")) == {}
        assert json.loads(await host.reply_to(api, "owner carol caro")) == {}
        got = json.loads(await host.reply_to(api, "owner bad bad"))
        assert got == {"error": "BAD_REQUEST"}
        assert json.loads(await host.reply_to(
            api, "lora use_preset=false bandwidth=250 spread_factor=9 override_frequency=869.525 "
                 "region=eu_868")) == {}
        assert json.loads(await host.reply_to(api, "lora hop_limit=5")) == {}
        sent = admin_pb2.AdminMessage()
        sent.ParseFromString(fake.got[-1].packet.decoded.payload)
        lora = sent.set_config.lora
        # The second builds on the first.
        assert (lora.use_preset, lora.bandwidth, lora.spread_factor, lora.hop_limit) == (
            False, 250, 9, 5)
        assert abs(lora.override_frequency - 869.525) < 1e-4
        assert "no setting" in json.loads(await host.reply_to(api, "lora nonsense=1"))["error"]
        assert json.loads(await host.reply_to(api, "device role=router")) == {}
        assert api.device.role == config_pb2.Config.DeviceConfig.Role.Value("ROUTER")
        assert json.loads(await host.reply_to(api, "edit commit")) == {}

        got = json.loads(await host.reply_to(api, "send bravo 0 1 hello there #a.1"))
        assert got["res"] == 0 and got["id"] == fake.got[-1].packet.id
        assert fake.got[-1].packet.to == BRAVO and fake.got[-1].packet.want_ack
        got = json.loads(await host.reply_to(api, "send ^all 1 0 to all"))
        assert fake.got[-1].packet.to == host.BROADCAST and fake.got[-1].packet.channel == 1
        assert json.loads(await host.reply_to(api, "send nobody 0 1 hi")) == {
            "error": "unknown node nobody"}
        fake.refuse_text = True
        assert "refused" in json.loads(await host.reply_to(api, "send !00000015 0 1 x"))["error"]

        got = json.loads(await host.reply_to(api, "traceroute bravo"))
        tr_id = got["id"]
        assert json.loads(await host.reply_to(api, "nodeinfo")) == {}
        await asyncio.sleep(0.1)
        assert fake.got[-1].heartbeat.nonce == 1
        nodes = json.loads(await host.reply_to(api, "nodes"))
        assert [(n["name"], n["id"], n["hops_away"]) for n in nodes] == [
            ("bravo", "!00000015", 1), ("charlie", "!00000016", 2)]   # asked of the firmware
        assert "unknown command" in json.loads(await host.reply_to(api, "frob"))["error"]

        # A received text, broadcast and direct.
        for to in (host.BROADCAST, OWN):
            p = fake.packet(CHARLIE, to, host.PORT_TEXT, "hi #b.2".encode(), hop_start=3,
                            hop_limit=1, rx_snr=5.5, rx_rssi=-90, channel=0)
            fake.out(packet=p)
        await asyncio.sleep(0.2)
        ev = events(said)
        first = next(g.packet.id for g in fake.got if g.WhichOneof("payload_variant") == "packet")
        assert {"event": "routing", "id": first, "from": "!00000014", "error": "NONE"} in ev
        texts = [e for e in ev if e["event"] == "routing" and e["from"] == "!00000015"]
        assert texts and texts[0]["error"] == "NONE"
        recv = [e for e in ev if e["event"] == "recv"]
        assert recv == [
            {"event": "recv", "from": "!00000016", "to": "^all", "ch": 0, "text": "hi #b.2",
             "snr": 5.5, "rssi": -90, "hops": 2},
            {"event": "recv", "from": "!00000016", "to": "!00000014", "ch": 0, "text": "hi #b.2",
             "snr": 5.5, "rssi": -90, "hops": 2}]
        tr = [e for e in ev if e["event"] == "traceroute"]
        assert tr == [{"event": "traceroute", "id": tr_id, "route": ["bravo", None],
                       "snr_towards": [6.0, -2.0, 2.5], "route_back": ["bravo"],
                       "snr_back": [3.0, 1.5]}]
        assert api.nodes[CHARLIE]["hops_away"] == 2
        reading.cancel()
        fake.server.close()
    asyncio.run(go())


def test_a_command_with_no_answer_says_so(said, monkeypatch):
    monkeypatch.setattr(host, "ANSWER_S", 0.3)
    monkeypatch.setattr(host, "ADMIN_ANSWER_S", 0.3)

    async def go():
        fake, api, reading = await connected()
        fake.answer = lambda msg: None
        assert json.loads(await host.reply_to(api, "send bravo 0 1 hi")) == {
            "error": "no answer from the firmware"}
        assert json.loads(await host.reply_to(api, "edit begin")) == {
            "error": "no answer from the firmware"}
        reading.cancel()
        fake.server.close()
    asyncio.run(go())


def test_framed_rpc_and_a_person_share_the_console(said):
    async def go():
        fake, api, reading = await connected()
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        host.Console(api, in_r, out_w)
        payload = b"info"
        os.write(in_w, b"no" + host.RPC_MAGIC + bytes((9, 0, len(payload))) + payload + b"des\r")
        await asyncio.sleep(0.3)
        out = os.read(out_r, 65536)
        at = out.index(host.RPC_MAGIC)
        n = (out[at + 5] << 8) | out[at + 6]
        assert out[at + 4] == 9
        assert json.loads(out[at + 7:at + 7 + n])["num"] == OWN
        rest = out[:at] + out[at + 7 + n:]
        assert b"nodes\r\n" in rest and b"bravo" in rest      # echoed, then its reply
        reading.cancel()
        fake.server.close()
    asyncio.run(go())


def test_the_firmware_closing_its_api_waits_for_it_to_exit(said, monkeypatch):
    left = []
    monkeypatch.setattr(host, "leave", left.append)

    class Proc:
        returncode = None

        async def wait(self):
            await asyncio.sleep(0.1)
            self.returncode = 0
            return 0

    class Gone:
        proc = Proc()

    async def go():
        fake = FakeFirmware()
        port = await fake.start()
        api = host.Api(Gone())
        await api.connect("127.0.0.1", port)
        reading = asyncio.create_task(api.read_loop())
        await asyncio.wait_for(api.handshake(), 5)
        fake.writer.close()
        await asyncio.wait_for(reading, 5)
        assert left == []           # its exit, not the lost connection, ends the station
        fake.server.close()
    asyncio.run(go())


def test_the_firmware_exiting_ends_the_host(said, monkeypatch):
    left = []
    monkeypatch.setattr(host, "leave", left.append)

    class Proc:
        returncode = None

        async def wait(self):
            return 0

    async def go():
        fw = host.Firmware()
        fw.proc = Proc()
        fw.partial = b"last words"
        await fw.watch()
    asyncio.run(go())
    assert left == [0]
    assert said[-2:] == ["fw: last words",
                         "mthost: the firmware exited (0); restarting the station"]


def test_config_yaml_names_the_chip_and_its_pins():
    text = host.config_yaml()
    for line in ("Module: sx1262", "CS: 1", "Reset: 2", "Busy: 3", "IRQ: 4",
                 "SX126X_MAX_POWER: 22", "AsciiLogs: true"):
        assert line in text
    assert "Webserver" not in text and "spidev" not in text
