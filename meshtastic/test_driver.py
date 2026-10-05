"""driver.py against a stand-in station: host.py's commands answered from a
table, the console's lines fed in by hand."""

import asyncio
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TESTBED = os.path.normpath(os.path.join(HERE, "..", "..", "sim-mesh", "testbed"))
sys.path.insert(0, TESTBED)
sys.path.insert(0, HERE)

import driver as mt  # noqa: E402
from sim_mesh.driver import CommandError  # noqa: E402

INFO = {"num": 20, "id": "!00000014", "name": "alpha", "role": "CLIENT",
        "lora": {"override_frequency": 869.475, "spread_factor": 7, "bandwidth": 125,
                 "coding_rate": 5}, "hop_limit": 3}


class Station:
    def __init__(self, tmp_path, name="alpha"):
        self.name = name
        self.node_id = 4
        self.dir = str(tmp_path / name)
        os.makedirs(self.dir, exist_ok=True)
        self.board = None
        self.starts = 1
        self.status = "up"
        self.rpc = object()
        self.events = []
        self.slept = 0.0

    def report(self, event, **fields):
        self.events.append(dict(fields, event=event))

    async def sleep(self, seconds):
        self.slept += seconds
        await asyncio.sleep(0)


class Driver(mt.Meshtasticd):
    """Commands answered from `answers` (a prefix -> a reply or a function
    of the line), each line kept in `lines`."""

    def __init__(self, answers=None):
        super().__init__({"firmware": "meshtastic-sx1262_x_1", "base": "meshtastic-sx1262",
                          "title": "Meshtastic", "exec": "/x/host.py"})
        self.lines = []
        self.answers = dict({"info": INFO, "edit": {}, "owner": {}, "lora": {}, "device": {},
                             "nodeinfo": {}}, **(answers or {}))

    async def rpc_ready(self, station, timeout, marker_wait_s):
        return True

    async def rpc_query(self, station, line, timeout=None):
        self.lines.append(line)
        reply = self.answers.get(line.split()[0], {"error": "unknown command"})
        if callable(reply):
            reply = reply(station, line)
        return json.dumps(reply) + "\n"


def run(coro):
    return asyncio.run(coro)


def test_the_region_is_the_first_band_that_holds_the_channel():
    assert mt.region_for(869.475, 125) == "EU_868"
    assert mt.region_for(869.525, 250) == "EU_868"
    assert mt.region_for(915.0, 125) == "US"
    assert mt.region_for(433.5, 125) == "EU_433"
    assert mt.region_for(2440.0, 812.5) == "LORA_24"
    with pytest.raises(CommandError, match="no Meshtastic region holds 869.475 MHz at 500"):
        mt.region_for(869.475, 500)
    with pytest.raises(CommandError, match="no Meshtastic region"):
        mt.region_for(915.0, 812.5)
    assert mt.bandwidth_code(62.5) == 62 and mt.bandwidth_code(125) == 125
    assert mt.bandwidth_code(31.25) == 31 and mt.bandwidth_code(1625) == 1600
    with pytest.raises(CommandError, match="no bandwidth of 41.7"):
        mt.bandwidth_code(41.7)


def test_setup_settings_wait_for_one_transaction(tmp_path):
    st = Station(tmp_path)
    d = Driver()

    async def go():
        assert not d.configured(st)
        await d.name(st, "alpha")
        await d.radio(st, freq_mhz=869.475, sf=7, bw_khz=125, cr=5, tx_dbm=22)
        await d.role(st, "router")
        await d.hop_limit(st, 5)
        assert d.lines == []                    # only recorded
        with pytest.raises(CommandError, match="did not restart"):
            await d.flush(st)
        assert d.lines == [
            "edit begin", "owner alpha alph",
            "lora use_preset=false bandwidth=125 spread_factor=7 coding_rate=5 "
            "override_frequency=869.475 frequency_offset=0 tx_enabled=true region=EU_868 "
            "tx_power=22 hop_limit=5",
            "device role=ROUTER", "edit commit"]
        assert d.configured(st) and st.slept == mt.RESTART_WAIT_S
        d.lines.clear()
        await d.flush(st)                       # nothing pending
        assert d.lines == []
    run(go())


def test_the_setup_flush_ends_with_its_task(tmp_path):
    st = Station(tmp_path)
    d = Driver()

    async def slow_sleep(seconds):
        await asyncio.sleep(10)
    st.sleep = slow_sleep

    async def go():
        await d.role(st, "client")
        task = asyncio.ensure_future(d.flush(st))
        await asyncio.sleep(0.05)
        assert d.configured(st)                 # written before the restart
        task.cancel()                           # the process exited
        with pytest.raises(asyncio.CancelledError):
            await task
    run(go())


def test_a_setting_after_setup_waits_for_the_restart(tmp_path):
    st = Station(tmp_path)
    os.makedirs(os.path.join(st.dir, "state"))
    open(os.path.join(st.dir, "state", mt.CONFIGURED), "w").close()
    d = Driver()

    async def restart():
        await asyncio.sleep(0.01)
        st.status, st.starts = "restarting", 2
        await asyncio.sleep(0.01)
        st.status = "up"

    async def go():
        st.sleep = lambda s: asyncio.sleep(0.002)
        task = asyncio.ensure_future(restart())
        await d.hop_limit(st, 2)
        await task
        assert d.lines == ["edit begin", "lora hop_limit=2", "edit commit"]
        st.sleep = lambda s: asyncio.sleep(0)
        with pytest.raises(CommandError, match="did not come back up"):
            await d.tx_power(st, 10)
    run(go())


def test_what_the_radio_will_not_take(tmp_path):
    st = Station(tmp_path)
    d = Driver()

    async def go():
        with pytest.raises(CommandError, match="sync word is fixed at 0x2b"):
            await d.radio(st, sync=0x12)
        with pytest.raises(CommandError, match="preamble is fixed at 16"):
            await d.radio(st, preamble=8)
        await d.radio(st, sync=0x2B, preamble=16)
        with pytest.raises(CommandError, match="no Meshtastic role"):
            await d.role(st, "boss")
        with pytest.raises(CommandError, match="0 to 7"):
            await d.hop_limit(st, 8)
        await d.radio(st, sf=9)                 # the rest from what it has
        assert d.pending["alpha"]["lora"]["spread_factor"] == 9
        assert d.pending["alpha"]["lora"]["region"] == "EU_868"
        await d.tx_power(st, -20)
        assert d.pending["alpha"]["lora"]["tx_power"] == 1     # never 0, the region's maximum
    run(go())


def test_messages_and_what_becomes_of_them(tmp_path):
    st = Station(tmp_path)
    bravo = Station(tmp_path, "bravo")
    ids = iter(range(100, 200))

    def send(station, line):
        pid = next(ids)
        to = "^all" if line.split()[1] == "^all" else "!00000015"
        if "early" in line:
            # The answer can come before the reply to the send.
            d.console_line(station, 'x mthost: {"event":"routing","id":%d,"from":"!00000014",'
                                    '"error":"RATE_LIMIT_EXCEEDED"}' % pid)
        return {"id": pid, "res": 0, "to": to}

    d = Driver({"send": send})

    def host(station, **said):
        d.console_line(station, "fw: something\r")
        d.console_line(station, "mthost: " + json.dumps(said))

    async def go():
        await d.sendtext(st, "hello", "alpha.1", dest="bravo")
        assert d.lines[-1] == "send bravo 0 1 'hello #alpha.1'"
        host(st, event="routing", id=100, **{"from": "!00000014", "error": "NONE"})  # implicit
        host(st, event="routing", id=100, **{"from": "!00000015", "error": "NONE"})
        await d.sendtext(st, "lost", "alpha.2", dest="bravo")
        host(st, event="routing", id=101, **{"from": "!00000014", "error": "MAX_RETRANSMIT"})
        await d.sendtext(st, "all", "alpha.3", ch_index=1, want_ack=False)
        assert d.lines[-1] == "send ^all 1 0 'all #alpha.3'"
        host(st, event="routing", id=102, **{"from": "!00000014", "error": "MAX_RETRANSMIT"})
        await d.sendtext(st, "early", "alpha.4")
        await d.sendtext(st, "open", "alpha.5", dest="bravo")
        d.answers["send"] = {"error": "unknown node carol"}
        await d.sendtext(st, "who", "alpha.6", dest="carol")
        assert [(e["mid"], e["status"], e.get("why")) for e in st.events] == [
            ("alpha.1", "sent", None), ("alpha.1", "delivered", None),
            ("alpha.2", "sent", None), ("alpha.2", "failed", "MAX_RETRANSMIT"),
            ("alpha.3", "sent", None),
            ("alpha.4", "sent", None), ("alpha.4", "failed", "RATE_LIMIT_EXCEEDED"),
            ("alpha.5", "sent", None),
            ("alpha.6", "failed", "unknown node carol")]
        st.events.clear()
        # A restart: what was still open has no answer coming.
        assert await d.wait_up(st, 5) is True
        assert st.events == [{"event": "msg.status", "mid": "alpha.5", "status": "failed",
                              "why": "station restarted"}]

        host(bravo, event="recv", to="!00000015", ch=0, text="hello #alpha.1",
             **{"from": "!00000014"})
        host(bravo, event="recv", to="^all", ch=1, text="all #alpha.3", **{"from": "!00000014"})
        host(bravo, event="recv", to="^all", ch=0, text="no tag", **{"from": "!00000014"})
        assert bravo.events == [
            {"event": "msg.received", "mid": "alpha.1", "text": "hello", "sender": "!00000014"},
            {"event": "msg.received", "mid": "alpha.3", "text": "all", "chan": 1}]
    run(go())


def test_traceroute_waits_on_the_runs_clock(tmp_path):
    st = Station(tmp_path)
    d = Driver({"traceroute": {"id": 7}})

    async def go():
        async def answer():
            await asyncio.sleep(0.02)
            d.console_line(st, "mthost: " + json.dumps({
                "event": "traceroute", "id": 7, "route": ["bravo"], "snr_towards": [6.0, 5.0],
                "route_back": ["bravo"], "snr_back": [4.0, 3.0]}))

        async def slow(seconds):
            st.slept += seconds
            await asyncio.sleep(0.005)
        st.sleep = slow
        task = asyncio.ensure_future(answer())
        got = await d.traceroute(st, "charlie")
        await task
        assert got == {"route": ["bravo"], "snr_towards": [6.0, 5.0], "route_back": ["bravo"],
                       "snr_back": [4.0, 3.0]}
        assert d.lines[-1] == "traceroute charlie"
        st.slept = 0.0
        st.sleep = lambda s: asyncio.sleep(0, st.__dict__.update(slept=st.slept + s))
        with pytest.raises(CommandError, match="no traceroute answer from charlie"):
            await d.traceroute(st, "charlie")
        assert st.slept == pytest.approx(mt.TRACEROUTE_WAIT_S)
    run(go())


def test_role_nodes_and_the_rest(tmp_path):
    st = Station(tmp_path)
    d = Driver({"nodes": [{"num": 21, "id": "!00000015", "name": "bravo", "hops_away": 0,
                           "snr": 6.5, "last_heard": 1234}]})

    async def go():
        assert await d.current_role(st) == "client"
        d.answers["info"] = dict(INFO, role="ROUTER_LATE")
        assert await d.current_role(st) == "router"
        assert await d.nodes(st) == [("bravo", "!00000015", 0, 6.5, 1234)]
        await d.nodeinfo(st)
        assert d.lines[-1] == "nodeinfo"
        diag = await d.diagnostics(st)
        assert json.loads(diag["info"])["role"] == "ROUTER_LATE" and "bravo" in diag["nodes"]
        assert d.argv(st) == ["env", "SIM_MESH_NODE_ID=1000004", "MESHTASTIC_FIRMWARE_NODE_ID=4",
                              "python3", "/x/host.py"]
        assert d.sids(st) == (4, 1000004) and d.console_sid(st) == 1000004
        assert d.env(st) == {"SIM_MESH_IDLE": "threads"}
        d.answers["info"] = {"error": "no answer from the firmware"}
        with pytest.raises(CommandError, match="info: no answer"):
            await d.current_role(st)
    run(go())
