# The station contract

What sim-mesh gives every station process, and what it expects back. A firmware
that keeps this contract, and links `radio/` below its radio driver, runs on
the testbed; what differs between firmwares beyond it is a **kind**
(`testbed/kinds/`).

## The environment

| Variable | Meaning |
|---|---|
| `SIM_MESH_NODE_ID` | a small integer, unique on the host; the last byte of any MAC (media access control) address the station makes, and the `sid` it gives the ether |
| `SIM_MESH_NODE_DIR` | the station's directory; its working directory; its state lives under `state/` |
| `SIM_MESH_BIND_ADDR` | its own loopback address, fixed by its id in the testbed's network (`simd --net`, a /22 holding 1000 stations by default); every socket it opens binds here |
| `SIM_MESH_ETHER` | `host:port` of the ether |
| `SIM_MESH_BOARD` | the board its node is, as one flat JSON object: `chip` (`sx1262`), `max_dbm` (the node's maximum power at the antenna connector) and, when that is above 22 dBm and the node has a GC1109 front-end module, `fem_part`, `fem_tx_cal` (chip register → connector dBm, in the `LORAn_TX_CAL` form), `fem_gain_db` (its flat gain, when there is no curve) and `fem_rx_gain_db`, from `testbed/boards.py`. The chip model reads it too (below, The radio); a firmware that drives a front end takes its figures from here |
| `SIM_MESH_TIME` | `virtual` in a virtual-time run, absent in a real-time one |
| `SIM_MESH_EPOCH_US` | virtual time: the wall-clock microseconds T 0 stands for |
| `SIM_MESH_SEED` | virtual time: the ether's seed, the one its `welcome` carries; the shim keys the station's `getentropy`/`getrandom` by it and `SIM_MESH_NODE_ID` |
| `LD_PRELOAD` | virtual time: `radio/build/libsimclock.so`, the time shim |
| `SIM_MESH_IDLE` | virtual time, set by a kind whose firmware does not call `simradio_idle()` itself: `threads`, and the shim says the station is idle when every thread is blocked (below) |
| `SIM_MESH_CLOCK_PROFILE` | optional, from a kind's `env:`: node time as a function of T, `T:node,T:node,…` in microseconds, both columns increasing, slope 1 outside the points. Absent, node time is T |
| the kind's `env:` | anything the binary needs beyond that |

Nothing else is promised. A station reads its identity from these and from
nowhere else, so two stations on one host never collide.

## The process

- **stdin and stdout are the console**: a pty (pseudo-terminal), text, shown
  to a person in the map's console window and appended to `log` in its
  directory. The one binary thing that may cross it is a framed RPC (remote
  procedure call) frame
  ([`spangap-core/docs/framed-rpc.md`](../spangap-core/docs/framed-rpc.md)),
  which the supervisor takes out of the stream before anything else sees it;
  a kind that speaks it says so, and the rest print text only.
- **Exit to reboot.** The supervisor starts the binary again, on the same
  directory and address, half a second later (of T, in a virtual-time run). A
  station that wants to reboot exits.
- **State is its directory.** A new simulation starts it with an empty
  `state/`, or with the one a snapshot kept; a factory reset empties it; a
  reset leaves it. Whatever marks "this directory has been
  set up" is the kind's to name.
- **Ports are its own**, on its own address. Which ones, and what answers on
  them, is the kind's to say.
- **It may be several processes.** A process it starts that waits on time
  joins a virtual-time run as a station of its own, under the id its kind
  names for it (`sids`), with the same address, the time shim and
  `SIM_MESH_IDLE=threads`: it opens its link (`simradio_station_open`) and,
  with no radio, no chip. Whichever of them reads the console is the kind's
  to name (`console_sid`); `SIM_MESH_NODE_ID` is that process's id, and the
  radio's is the node's own. A `standard_reticulum` station is two: its
  RNode, and the Python behind it that reads the console.

## The radio

The station links `radio/` (`simradio.h`) and calls
`simradio_station_open(SIM_MESH_NODE_ID, SIM_MESH_BIND_ADDR, SIM_MESH_ETHER)` once,
then `simradio_open(slot, …)` per radio. Its driver talks to the chip model
frame by frame, exactly as to an SX1262 on a bus. A firmware built on
Portduino links [`radio/portduino/`](radio/portduino/README.md) instead,
which does that for it and binds the chip's lines at the pin numbers it is
given in `SIMRADIO_PIN_NSS`, `SIMRADIO_PIN_RESET`, `SIMRADIO_PIN_BUSY` and
`SIMRADIO_PIN_DIO1` (1 to 4 when unset); a kind of such a firmware sets them
to the pins it configures the firmware with.

**One whole frame per NSS cycle.** A bus adapter hands the model everything
the driver put on the bus between NSS (the chip-select line) going low and
going high as one frame: writes inside a frame are appended, never passed on
one by one; a frame that contains a read is complete at the read, and nothing
is sent again at NSS high; the adapter never adds a NOP of its own, because
the driver sends it. A reassembly one byte off makes `GetIrqStatus` read the
status byte as the IRQ word's high byte, so every flag looks set and every
transmission "succeeds" in no time.

**A station never learns where it stands.** Its position is the ether's and
the loss table's alone; nothing in the environment or in setup tells it, so a
firmware that reads a position behaves here as it would with none.

**The model applies the board's front end** from `SIM_MESH_BOARD`: what the
chip radiates goes through `fem_tx_cal`'s curve for `fem_part` (or the flat
`fem_gain_db`) before the ether is told its power, and every level the chip
reads, instant or per packet, is `fem_rx_gain_db` above the connector's. A
firmware that drives a front end converts with the same figures, so it
radiates what it asked for; one that does not sends at the curve's reading
of whatever it set.

The medium matches receivers on carrier, bandwidth, spreading factor and sync
word, and **does not model preamble length**: two radios whose preambles
differ hear each other here and may not on a bench. Set them equal in a
nodeset that means to say anything about hardware.

**Sync words differ by default between kinds**: `reticulous` uses 0x42,
`sergeyculum`, `microreticulum` and `standard_reticulum` (as RNode) 0x12,
and theirs cannot be changed. The startup script sets every radio to `globals.py`'s `SYNC`,
0x12, RNode's, so a `reticulous` station hears them; when nothing crosses
between kinds, the `state` lines in `record.tsv` (sync, carrier, bandwidth,
spreading factor) are the first thing to read.

## Time

In a real-time run a station keeps the host's time. In a virtual-time run
([INTERNALS.md](INTERNALS.md#time)) the ether owns time, and a station keeps
four promises:

- **It reads time only through the C library or `radio/`.** The shim answers
  `clock_gettime`, `gettimeofday`, `time`, the sleeps, `setitimer` and the
  timeouts of `poll`, `select`, `epoll_wait`, `pthread_cond_timedwait`
  (on either clock a condition keeps) and `sem_timedwait`/`sem_clockwait`
  (what CPython's locks wait in) in node time; the model's own timers are on
  T. A raw `rdtsc`, a `clock_gettime`
  made by system call, or a wait that none of those is does not move with the
  run.
- **It opens its link early**, before anything in it waits on time:
  `simradio_station_open` is where the station's clock starts and the shim
  attaches, and until the ether's welcome the monotonic clocks read 0 and the
  wall clocks the run's epoch. A monotonic clock that jumped backwards at
  attach would saturate Rust's `Instant` and hold a station's uptime at 0 for
  the whole run. The mode itself comes in the environment (`SIM_MESH_TIME`)
  as well as in the welcome, because a FreeRTOS station calls `setitimer`
  before it can reach the ether; the two disagreeing is an error.
- **It says when it is idle, and until when.** Idle is every thread
  blocked; the `until` it reports is the earliest wake anything in it holds
  (`simradio_wake_at`), which is the instant it next needs to run, and
  nothing sooner. Either the host calls `simradio_idle()` itself — a FreeRTOS
  station from its tickless idle, with a wake at the tick its first task is
  due at and another at esp_timer's next expiry, so a station whose tasks
  sleep for a second is woken once in that second — or the kind sets
  `SIM_MESH_IDLE=threads` and the shim keeps a census of the process's threads
  and says so for it, each sleeping thread's deadline a wake. A station that
  says neither is reported idle by the library's watchdog, 20 ms of wall
  time after every message once none of its threads is on the CPU, which
  runs but crawls. A host whose own clock is
  counted from node time, as a kernel tick is, learns of every move of it
  from `simradio_on_advance()`. The link is UDP, and a datagram lost either
  way would leave the ether and the station each waiting on the other, so
  `radio/` applies the ether's messages strictly in their numbers' order,
  never twice, and says its idle again every 250 ms of wall time until it
  hears back; the ether answers an idle said twice for an older number by
  sending what came after it again.
- **It reads its console and talks TCP (Transmission Control Protocol) to
  other stations through the C
  library**: `read` on descriptor 0, and `read`/`recv`/`send`/`write` and
  their vector forms on its sockets. The shim counts those bytes for the
  ether, which holds T until a station has read what it was sent and lets a
  TCP write go only when its reader is in step. Input taken some other way
  (through `stdio` from `stdin`, say) is not seen as read, and T waits a
  second of wall time for it before going on. A socket it listens on is
  reported as its own once its link is open, so a connection to it is known
  to be this station's before it has read anything, even from another
  process of the run on the same address.

A kind waits between two questions to a station with `pause()`, which is on
T in a virtual run, so a poll costs the station the same time in either mode.

**A kind whose door is a pty holds a slave descriptor open for the station's
whole life.** Reading a pty master returns `EIO` once every slave descriptor
is closed, and a tool that opens and closes the slave per command (`rncfg`)
would otherwise close the door between commands.

## A kind

A node's device names its kind (`kind` in its `node.yaml`, [NODE.md](NODE.md));
a kind is a class in `testbed/kinds/` that says, for one firmware:

| | |
|---|---|
| `env` | the environment above, plus what that firmware and its device read |
| `sids`, `console_sid` | the ether ids of its processes that join a run, and of the one that reads the console: the node's own, unless it is several processes |
| `wait_up` | when a started station counts as up: answering, and done booting |
| `pause` | a wait on the run's clock, for a kind's polls |
| `run` | how one line, from setup, a script or **Run command**, is put to it |
| `setup_line` | one setup line, and whatever waiting it takes for it to land |
| `setup` | a station's setup lines, on its first boot, then a flush |
| `flush` | how to make what it was told durable, before a stop or a snapshot |
| `lines` | an intent (`name`, `role`, `radio`, `radio_up`, `tx_power`, `announce`, `message`, `path`, `peer_tcp`) in its own lines, or refused |
| `address` | its LXMF delivery address |
| `role` | what it does for the mesh, read live: `transport`, `router`, `repeater` or `client`, or unknown |
| `web_port` | the port of its web UI, or none |
| `configured` | whether its directory has been set up already |
