# sim-mesh-firmware

Everything that was specific to one firmware project, out of sim-mesh: each
project's sim-mesh driver and the script that makes its firmware zip.

The aim is that every firmware project builds and publishes its own sim-mesh
zip, as one more build target beside its boards: its driver, its
platform or variant for sim-mesh's virtual radio, and its packaging script
in its own repository (or a fork of it, until upstream takes them), built
and published with the project's releases. What is here waits until it
moves there. Four have:

| Firmware | Where | Zip from |
|---|---|---|
| Reticulous | the reticulous repository: driver `sim-mesh/driver.py` | `spangap make-builds hw-sim-mesh-<arch>`, and in `builds/dev` its `post:` `sim-mesh/rns-zip`, the same image under `standard-reticulum/` |
| Sergeyculum | `sergey/reticulum`, branch `feat/sim-mesh-firmware-zip`: `fw/sim-mesh/` | `fw/sim-mesh/make_zip.py`, which also makes the zip under `standard-reticulum/` |
| MeshCore (companion, repeater, room server) | the fork `sim-mesh/MeshCore`, branch `sim-mesh`: a Linux (Portduino) platform under `src/helpers/portduino/` and the variant `variants/sim_mesh_sx1262/`, its driver and companion host in `sim/` | `variants/sim_mesh_sx1262/sim/make-zips` |
| Meshtastic (meshtasticd) | the fork `sim-mesh/meshtastic` of `meshtastic/firmware`, branch `sim-mesh` on release 2.7.26: the source's `SIM_MESH` changes and the variant `variants/native/sim-mesh/`, its driver and API host in `sim/` | `variants/native/sim-mesh/sim/make-zips` |

Each fork's branch holds what the project would need to build sim-mesh zips
itself, and its variant's README says how to build them.

Still here:

| Directory | Firmware | Driver | Zip from |
|---|---|---|---|
| `standard-reticulum/` | Reticulum's Python reference with an LXMF router over an RNode (`station.py`): the RNode daemon, or Reticulous or Sergeyculum with their own stack running | `station.py`'s framed RPC; a flush restarts it | `make_zip.py RNODE`, RNODE the daemon built with `[env:sim-mesh-rnode]` (`rns-<version>-rnode-sx1262`) or a Reticulous or Sergeyculum firmware zip (`rns-<version>-<its base>`), with PyPI's latest `rns`, `lxmf` and `pyserial` |
| `microreticulum/` | attermann's microReticulum_Firmware daemon, `[env:sim-mesh]` | edits to `rnoded.conf` and a restart | the daemon (`make_zip.py DAEMON`; `--base`/`--mode` for a variant) |
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
- **Meshtastic**: sim-mesh's `radio/portduino/` too, in its fork's
  `[env:sim-mesh]`: `pio run -e sim-mesh`, which `make-zips` runs before it
  packs the program with the libraries meshtasticd loads and the protobuf
  bindings.
- **MeshCore**: its fork's Portduino platform with RadioLib over a HAL
  that hands the radio whole SPI frames: `pio run -e
  sim_mesh_sx1262_<role>` for `companion`, `repeater` and `room`, which
  `make-zips` runs before it packs them.

Both forks build as their projects build: each architecture on a machine of
its own, which their workflows (`build_sim_mesh.yml` in Meshtastic's,
`build-sim-mesh-firmwares.yml` in MeshCore's) do on an x86_64 and an arm64
runner, keeping the zips as artifacts. `make-zips` builds for the machine's
own architecture; `--arch` builds another on the same machine by its cross
g++.
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
