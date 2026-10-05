"""The standard_reticulum kind and its station program: the intents, the two
ether ids of a station, the environment, txp at the connector, when a flush
restarts, and station.py's console, Reticulum config and rnoded.conf."""

import asyncio
import importlib.util
import json
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import boards  # noqa: E402
import kinds  # noqa: E402
import rpc  # noqa: E402
import stations  # noqa: E402
from kinds import standard_reticulum  # noqa: E402

DEVICE = {"ref": "sr", "kind_type": "standard_reticulum", "elf": "/station.py",
          "tools": {"rnode": "/rnode"}}
RADIO = {"freq_mhz": 869.475, "sf": 7, "bw_khz": 125.0, "cr": 5, "tx_dbm": 27.0,
         "sync": 0x12, "preamble": 18}
STATION_PY = os.path.join(os.path.dirname(HERE), "stations", "standard_reticulum", "station.py")


def kind():
    return kinds.make_kinds({"sr": DEVICE})["sr"]


class Client:
    """A station's framed-RPC client: the lines it was asked, and its answers."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    async def query(self, line, timeout=None):
        self.asked.append(line)
        return self.answers.get(line, "")

    async def wait_ready(self, timeout, marker_wait, pause):
        return True


def station(tmp_path, answers=None, status=stations.UP, clock=None):
    st = types.SimpleNamespace(name="n1", node_id=3, dir=str(tmp_path), addr="127.16.0.7",
                               ether_addr="127.0.0.1:7000", clock=clock,
                               board=boards.environment(27), status=status, starts=1,
                               rpc=Client(answers or {}), restarts=0)

    async def restart():
        st.restarts += 1
        st.starts += 1
        st.rpc.answers["status"] = "state: up\npending: no"
    st.restart = restart
    return st


def test_intents_are_station_commands():
    sr = kind()
    assert isinstance(sr, standard_reticulum.StandardReticulum)
    assert sr.lines("name") == ["set name {name}"]
    assert sr.lines("radio", **RADIO) == [
        "set freq_hz 869475000", "set bw_hz 125000", "set sf 7", "set cr 5", "txp 27"]
    assert sr.lines("radio_up") == []
    assert sr.lines("tx_power", dbm=10.4) == ["txp 10"]
    assert sr.lines("role", role="transport") == ["set transport 1"]
    assert sr.lines("role", role="client") == ["set transport 0"]
    assert sr.lines("announce") == ["announce"]
    assert sr.lines("message", dest="ab" * 16, text="G0001-mesh") == ["send %s G0001-mesh" % ("ab" * 16)]
    assert sr.lines("path", dest="ab" * 16) == ["path %s" % ("ab" * 16)]
    with pytest.raises(kinds.CommandError, match="no way to peer tcp"):
        sr.lines("peer_tcp", addr="127.16.0.5")


def test_a_station_is_two_processes_of_the_run_and_the_second_reads_the_console(tmp_path):
    sr = kind()
    st = station(tmp_path, clock=types.SimpleNamespace(epoch=1, seed=2))
    assert sr.sids(st) == (3, 3 + standard_reticulum.COMPANION)
    assert sr.console_sid(st) == 3 + standard_reticulum.COMPANION
    env = sr.env(st)
    assert env["SIM_MESH_NODE_ID"] == str(3 + standard_reticulum.COMPANION)
    assert env["SR_RNODE_ID"] == "3"
    assert env["SR_RNODE"] == "/rnode"
    assert env["SR_SIMRADIO"].endswith(os.path.join("radio", "build", "libsimradio.so"))
    assert env["SIM_MESH_IDLE"] == "threads"


def test_the_ether_expects_and_forgets_both_processes(tmp_path):
    seen = []
    clock = types.SimpleNamespace(expect=lambda sid: seen.append(("expect", sid)),
                                  leave=lambda sid: seen.append(("leave", sid)))
    st = stations.Station("n1", 3, str(tmp_path), kind(), "127.0.0.1:7000", clock=clock)
    st.expect()
    st.leave()
    companion = 3 + standard_reticulum.COMPANION
    assert seen == [("expect", 3), ("expect", companion), ("leave", 3), ("leave", companion)]


def test_txp_is_the_power_at_the_connector(tmp_path):
    st = station(tmp_path)
    asyncio.run(kind().run(st, "txp 27"))
    assert st.rpc.asked == ["set txpower %d" % boards.chip_dbm(st.board, 27)]


def test_a_flush_restarts_only_a_station_that_is_up_with_a_setting_pending(tmp_path):
    sr = kind()
    pending = {"status": "state: up\npending: yes"}
    st = station(tmp_path, dict(pending), status=stations.SETUP)
    asyncio.run(sr.flush(st))
    assert st.restarts == 0                 # still being set up
    st = station(tmp_path, {"status": "state: up\npending: no"})
    asyncio.run(sr.flush(st))
    assert st.restarts == 0                 # nothing to apply
    st = station(tmp_path, dict(pending))
    asyncio.run(sr.flush(st))
    assert st.restarts == 1


def test_role_and_address_are_read_from_the_station(tmp_path):
    addr = "7ef64a0ea298889606aa22b9bd9f6c59"
    st = station(tmp_path, {"transport": "transport: on", "addr": "lxmf.delivery : " + addr})
    assert asyncio.run(kind().role(st)) == "transport"
    assert asyncio.run(kind().address(st)) == addr
    st.rpc.answers["transport"] = "transport: off"
    assert asyncio.run(kind().role(st)) == "client"


def test_a_station_is_configured_once_it_has_settings(tmp_path):
    sr = kind()
    st = station(tmp_path)
    assert not sr.configured(st)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.json").write_text("{}")
    assert sr.configured(st)


# ---- station.py -------------------------------------------------------------------

@pytest.fixture
def program(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location("sr_station", STATION_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = []
    monkeypatch.setattr(module, "emit", out.append)
    module.out = out
    return module


def test_the_console_answers_frames_and_lines(program):
    node = types.SimpleNamespace(up=False, settings=dict(program.DEFAULTS))
    console = program.Console(node, dict(program.DEFAULTS))
    probe = rpc.frame(0x41, b"show s.net.hostname")
    # A frame split across reads, between a person's lines.
    console.feed(b"status\n" + probe[:3])
    console.feed(probe[3:] + b"set sf 7\r")
    assert program.out[0] == b"state: starting\npending: no\n"
    assert program.out[1] == rpc.frame(0x41, b"s.net.hostname = ")
    assert program.out[2] == b"sf = 7 (applies at restart)\n"
    assert json.loads(open(program.SETTINGS).read())["sf"] == 7
    console.feed(rpc.frame(0x42, b"lxmf create alpha"))
    assert program.out[3] == rpc.frame(0x42, b"name = alpha (applies at restart)")
    console.feed(rpc.frame(0x43, b"announce"))
    assert program.out[4] == rpc.frame(0x43, b"not up yet")


def test_reticulum_gets_the_rnode_only_once_the_radio_is_set(program):
    settings = dict(program.DEFAULTS, transport=1)
    text, radio = program.rns_config(settings, "127.16.0.7")
    assert not radio and "RNodeInterface" not in text
    assert "enable_transport = Yes" in text
    settings.update(freq_hz=869475000, bw_hz=125000, sf=7, cr=5, txpower=22)
    text, radio = program.rns_config(settings, "127.16.0.7")
    assert radio
    assert "port = tcp://127.16.0.7" in text
    assert "frequency = 869475000" in text and "txpower = 22" in text


def test_the_rnode_is_driven_by_its_host_on_the_station_address(program):
    conf = dict(line.split(" = ") for line in program.rnode_conf("127.16.0.7", "3").splitlines())
    assert conf["kiss_tcp_bind"] == "127.16.0.7"
    assert conf["kiss_tcp_port"] == str(program.KISS_PORT)
    assert conf["reboot_mode"] == "exit"
    assert not any(key.startswith("lora_") for key in conf)
