# sim-mesh-firmware

Everything that was specific to one firmware project, out of sim-mesh: each
project's sim-mesh driver and the script that makes its firmware zip. Each
belongs in its own project's repository; it waits here until it moves there.
Two have: Reticulous's driver is `sim-mesh/driver.py` in the reticulous
repository, and `spangap make-builds` makes its zip; Sergeyculum's driver and
`make_zip.py` are in its `fw/sim-mesh/`, on the `feat/sim-mesh-firmware-zip`
branch of `sergey/reticulum`.

| Directory | Firmware | Driver | Zip from |
|---|---|---|---|
| `standard-reticulum/` | Reticulum's Python reference with an LXMF router over an RNode (`station.py`) | `station.py`'s framed RPC; a flush restarts it | the RNode daemon built with `[env:sim-mesh-rnode]`, plus `rns` and `lxmf` from PyPI (`make_zip.py RNODE`) |
| `microreticulum/` | attermann's microReticulum_Firmware daemon, `[env:sim-mesh]` | edits to `rnoded.conf` and a restart | the daemon (`make_zip.py DAEMON`; `--base`/`--mode` for a variant) |
| `meshtastic/` | Meshtastic's Linux daemon, meshtasticd 2.7.26, `[env:sim-mesh]` from `firmware.patch`, with its API client (`host.py`) as a second, radio-less process of the station | `host.py`'s commands over framed RPC; settings in one transaction and the restart it brings | meshtasticd and the protobuf bindings generated from its own `protobufs` (`make_zip.py PROGRAM`) |
| `old/` | what sim-mesh held before: its station kinds, compiled-build files, `station.py`, `devices.py`, the ESP-IDF radio glue and its link test, `NODE.md`, `STATION.md`, and their tests | | |

Every station links sim-mesh's virtual radio by name (`-lsimradio-sx1262`,
built when `sim` starts), and sim-mesh provides it when it starts the
station:

- **Reticulous**: iface-lora's `src/host/simradio_glue.cpp` hands the radio
  its ESP-IDF services; `make-builds hw-sim-mesh-<arch>` leaves the finished
  zip (`sim-mesh/README.md` in the reticulous repository).
- **standard Reticulum, microReticulum**: sim-mesh's `radio/portduino/`, in
  the `competition/attermann_microReticulum_Firmware` clone:
  `pio run -e sim-mesh` (`-e sim-mesh-rnode` for standard Reticulum's RNode,
  `-e sim-mesh-jrl290` for the jrl290 stand-in). Portduino needs the
  headers of libuv, i2c-tools, libgpiod, yaml-cpp and libbsd
  (`libuv1-dev libi2c-dev libgpiod-dev libyaml-cpp-dev libbsd-dev`).
- **Meshtastic**: sim-mesh's `radio/portduino/` too, in the
  `competition/meshtastic_firmware` clone at tag `v2.7.26.54e0d8d` with
  `meshtastic/firmware.patch` applied (`git apply`; it adds
  `variants/native/sim-mesh/platformio.ini`): `pio run -e sim-mesh`, then
  `meshtastic/make_zip.py .pio/build/sim-mesh/meshtasticd`. Besides
  Portduino's headers it needs `libssl-dev`; `make_zip.py` fetches
  `grpcio-tools` and the pure-Python `protobuf` from PyPI. `test_host.py`
  and `test_driver.py` run with sim-mesh's Python environment; the first
  builds the bindings into `meshtastic/.pylib` once.
- **Sergeyculum**: its `sim-mesh-radio-sys` crate links it by name;
  `cargo build --profile sim` in `fw/sim-mesh`, and
  `cargo build --release -p rncfg` at the top, then its
  `fw/sim-mesh/make_zip.py`.

microReticulum is a transport node with no destination of its own, so a
traffic run of microReticulum stations alone has nobody to send between; it
belongs among stations that have one.

Sergeyculum's station logs every answer it gives a send as
`[lxmf] msg <id> <word>` (and `unproved` for one given up on after its last
attempt), which is what its `fw/sim-mesh/driver.py` reports a message's status
from; a message held while a path is asked for has no id until it is sent,
and the driver attributes what becomes of it by the station holding one at
a time.

Publishing a zip as pre-built: `sim firmware publish ZIP`.
