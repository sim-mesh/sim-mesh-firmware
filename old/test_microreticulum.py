"""The microreticulum kind: its intents, its lines as edits to rnoded.conf,
the file a new station starts from, when a flush restarts the station, and
how its log says it is up."""

import asyncio
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import boards  # noqa: E402
import kinds  # noqa: E402
import nodeset  # noqa: E402
import stations  # noqa: E402
from kinds import microreticulum  # noqa: E402

DEVICE = {"ref": "mr", "kind_type": "microreticulum", "elf": "/z", "virtual_hardware": "ESP32-S3"}
RADIO = {"freq_mhz": 869.525, "sf": 8, "bw_khz": 125.0, "cr": 5, "tx_dbm": 14.0,
         "sync": 0x12, "preamble": 18}


def kind(**device):
    return microreticulum.Microreticulum(dict(DEVICE, **device))


def station(tmp_path, status=stations.UP):
    st = types.SimpleNamespace(name="n1", node_id=3, dir=str(tmp_path), addr="127.16.0.3",
                               ether_addr="127.0.0.1:7000", clock=None, board=boards.environment(27),
                               status=status, starts=1,
                               log_path=str(tmp_path / "log"), restarts=0)

    async def restart():
        st.restarts += 1
        st.starts += 1
        with open(st.log_path, "ab") as f:
            f.write(b"--- station n1 starting ---\nRNS Transport is READY!\n")
    st.restart = restart
    return st


def conf(tmp_path):
    return (tmp_path / "rnoded.conf").read_text()


def test_intents_are_rnoded_conf_edits_or_refused():
    mr = kinds.make_kinds({"mr": DEVICE})["mr"]
    assert isinstance(mr, microreticulum.Microreticulum)
    assert mr.lines("radio", **RADIO) == [
        "set lora_freq_hz 869525000", "set lora_bw_hz 125000", "set lora_sf 8",
        "set lora_cr 5", "txp 14"]
    assert mr.lines("role", role="transport") == []
    assert mr.lines("tx_power", dbm=10.4) == ["txp 10"]
    assert mr.lines("name") == []
    with pytest.raises(kinds.CommandError, match="always a transport"):
        mr.lines("role", role="client")
    for verb in ("announce", "message", "path", "peer_tcp"):
        with pytest.raises(kinds.CommandError, match="microreticulum station has no way"):
            mr.lines(verb)
    assert mr.label == "mr (virtual ESP32-S3)"


def test_a_new_station_starts_from_the_fixed_file(tmp_path):
    env = kind(env={"MR_LORA_INTERFACE_MODE": "full"}).env(station(tmp_path))
    assert env["MR_CONFIG"] == str(tmp_path / "rnoded.conf")
    assert env["MR_DATA_DIR"] == str(tmp_path / "state")
    assert (env["SIMRADIO_PIN_NSS"], env["SIMRADIO_PIN_DIO1"]) == ("1", "4")
    text = conf(tmp_path)
    assert "pin_cs = 1\n" in text and "pin_dio = 4\n" in text and "pin_txen = -1\n" in text
    assert "kiss_tcp_port = 0\n" in text and "reboot_mode = exit\n" in text
    assert "device_id = sim-mesh-3\n" in text
    assert text.endswith("lora_interface_mode = full\n")
    (tmp_path / "plain").mkdir()
    kind().env(station(tmp_path / "plain"))
    assert "lora_interface_mode" not in conf(tmp_path / "plain")
    (tmp_path / "rnoded.conf").write_text("lora_sf = 9\n")
    kind().env(station(tmp_path))
    assert conf(tmp_path) == "lora_sf = 9\n"


def test_lines_edit_the_file_in_place(tmp_path):
    mr, st = kind(), station(tmp_path)
    (tmp_path / "rnoded.conf").write_text("# mine\nlora_sf = 7  # was\nmodem = SX1262\n")

    async def go():
        await mr.run(st, "set lora_sf 9")
        await mr.run(st, "set lora_interface_mode full")
        await mr.run(st, "unset modem")
        await mr.run(st, "txp 24")
        with pytest.raises(kinds.CommandError, match="set <key> <value>"):
            await mr.run(st, "announce now")
    asyncio.run(go())
    assert conf(tmp_path) == "# mine\nlora_sf = 9\nlora_interface_mode = full\nlora_txp = 13\n"
    assert mr.dirty == {str(tmp_path): True}


def test_a_flush_restarts_only_a_station_that_is_up_and_changed(tmp_path):
    mr, st = kind(), station(tmp_path, status=stations.SETUP)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "eeprom").write_text("seeded")

    async def go():
        await mr.flush(st)                      # nothing changed
        await mr.run(st, "set lora_sf 9")
        await mr.flush(st)                      # still being set up
        assert st.restarts == 0 and (tmp_path / "state" / "eeprom").exists()
        st.status = stations.UP
        await mr.flush(st)
        assert st.restarts == 1 and not (tmp_path / "state" / "eeprom").exists()
        await mr.flush(st)
        assert st.restarts == 1
        (tmp_path / "state" / "eeprom").write_text("seeded")
        await mr.run(st, "set lora_interface_mode full")
        await mr.flush(st)
        assert st.restarts == 2 and (tmp_path / "state" / "eeprom").exists()
    asyncio.run(go())


def test_up_is_the_ready_line_since_the_latest_start(tmp_path):
    mr, st = kind(), station(tmp_path)
    log = tmp_path / "log"
    log.write_bytes(b"--- station n1 starting ---\nRNS Transport is READY!\n"
                    b"--- station n1 starting ---\nbooting\n")

    async def go():
        assert await mr.wait_up(st, 0.0) is False
        assert await mr.role(st) is None
        with open(log, "ab") as f:
            f.write(b"00:00:00.724 [---] RNS Transport is READY!\n")
        assert await mr.wait_up(st, 1.0) is True
        assert await mr.role(st) == "transport"
        st.starts += 1
        with open(log, "ab") as f:
            f.write(b"--- station n1 starting ---\nRNS is inoperable because hardware is not ready!\n")
        assert await mr.wait_up(st, 5.0) is False
    asyncio.run(go())


def test_configured_once_the_file_and_state_exist(tmp_path):
    mr, st = kind(), station(tmp_path)
    assert not mr.configured(st)
    mr.env(st)
    (tmp_path / "state").mkdir()
    assert not mr.configured(st)
    (tmp_path / "state" / "eeprom").write_text("x")
    assert mr.configured(st)


def test_connector_power_through_the_front_end():
    heltec = boards.environment(27)
    # The GC1109's points land on themselves, straight lines between, flat outside.
    assert [boards.connector_dbm(heltec, c) for c in (-9, 1, 6, 8, 10, 20, 22)] == \
        [7, 7, 14, 17, 20, 28, 27]
    # The lowest setting that reaches the request; the peak for one out of reach.
    assert boards.chip_dbm(heltec, 24) == 13 and boards.chip_dbm(heltec, 28) == 19
    assert boards.chip_dbm(heltec, 40) == 19 and boards.chip_dbm(heltec, 0) == -9
    plain = boards.environment(None)
    assert boards.chip_dbm(plain, 14) == 14 and boards.chip_dbm(plain, 30) == 22
    assert boards.chip_dbm(None, 14) == 14
