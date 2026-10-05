# The device file

A device file is one station build, ready to run on a Linux host of one
architecture: an executable, whatever else it needs, and a `node.yaml` that
says what they are.

```
catalogue/index.html ── <a href="<slug>_hw-sim-mesh-<arch>_<stamp>.zip"> ──► reader
reader ── GET <slug>_hw-sim-mesh-<arch>_<stamp>.zip ──► catalogue
reader: unzip, read node.yaml, check arch and stamp, run <elf> as its kind says
```

## The file

A zip archive. In a catalogue it is named in the catalogue image format:

```
<slug>_<entry>_<stamp>.zip
```

- `<slug>` is the project's name, lower case, with every run of characters
  other than `a`–`z` and `0`–`9` made one `-` and a leading or trailing `-`
  dropped (`builds` when nothing is left). It holds no `_`.
- `<entry>` is `hw-sim-mesh-<arch>`, where `<arch>` is the architecture as
  `uname -m` names it on Linux: `aarch64` or `x86_64`.
- `<stamp>` is the build stamp: the UTC time of the build as
  `YYYYMMDDhhmmss`, digits only. A later stamp is a later build, compared as
  strings.

The filename is split from the right: the stamp after the last `_`, the slug
before the first, the entry between.

Handed over outside a catalogue, the file may be called anything. A reader
names it from its `node.yaml` as a catalogue would: the slug made from
`project`, or from `kind` when there is no `project`, the entry from `arch`,
and the stamp from `stamp`.

## The archive

Members sit at the archive's root, with forward-slash paths that stay inside
it (no absolute path, no `..` component):

| Member | |
|---|---|
| `node.yaml` | the description below |
| the executable | the file `elf` names |
| the data tree | the directory `fixed` names, when there is one |
| each tool | the file each entry of `tools` names |

A member's Unix permission bits, where the archive records them, are the
file's. The executable and the tools are executable whatever the archive says.

## node.yaml

A YAML mapping.

| Key | Required | Value |
|---|---|---|
| `kind` | yes | the station kind that runs it (`reticulous`, `sergeyculum`, …) |
| `arch` | yes | the architecture it runs on, as in the filename |
| `stamp` | yes | the build stamp, as in the filename; a string of digits (an integer is read as its digits) |
| `elf` | yes | the executable's path in the archive |
| `fixed` | no | the path in the archive of a read-only data tree the kind hands the station (a `reticulous` station reads it as its `/fixed`); absent, the station has none |
| `tools` | no | a mapping of tool name to its path in the archive: the programs the kind talks to the station with (`rncfg` for `sergeyculum`) |
| `env` | no | a mapping of environment variable to value, given to the station; a value starting `./` or `../` is a path in the archive |
| `name` | no | what a reader calls the device; absent, its project, catalogue and build time (`Reticulous dev 2026-09-25 03:50`) |
| `virtual_hardware` | no | the hardware the station plays (`ESP32-S3`); a reader shows it as a virtual one of that |
| `virtual_radio` | no | the radio chip the station drives (`SX1262`); a reader shows it as a virtual one of that |
| `project` | no | the project's name, as its catalogue gives it |
| `catalogue` | no | the catalogue it was built for (`stable`, `dev`, …) |
| `entry` | no | the catalogue entry it was built for, as in the filename |
| `invocation` | no | the build invocation that produced it |
| `version` | no | the version of the firmware it was built from |
| `libc` | no | the version of the C library it was linked against |

An optional key whose value is null is as if absent. Keys not listed here
are carried and ignored.

```yaml
kind: reticulous
arch: aarch64
stamp: "20260925035045"
elf: reticulous.elf
fixed: fixed
virtual_hardware: ESP32-S3
virtual_radio: SX1262
project: Reticulous
catalogue: dev
entry: hw-sim-mesh-aarch64
libc: "2.39"
```

## Validity

A device file is valid when:

- `node.yaml` is a mapping holding every required key;
- `elf` and every tool name a file in the archive, and `fixed`, when given,
  a directory;
- in a catalogue, `stamp` equals the filename's stamp and `arch` the
  filename's architecture.

A reader runs a device only on a machine of its `arch`: the executable is
native code, dynamically linked against the C library, C++ runtime, zlib and
libbsd of the system it was built on, and needs those on the machine that
runs it.

## In a catalogue

A catalogue lists its images in `index.html`, one `<a href>` per image, the
href relative to the listing. A device file is listed like any other image,
its anchor carrying `data-target="linux"` where a chip image's carries its
chip, so a reader that flashes chips leaves it out without fetching it. A
reader of device files finds them by the `hw-sim-mesh-` prefix of the entry
and takes the highest stamps per entry.

## Expanded

Expanded, a device file is a directory holding the archive's members as they
were, named as the archive less `.zip`. The expanded directory may also hold
`origin.yaml`, a mapping written by whoever expanded it:

| Key | Value |
|---|---|
| `catalogue` | the catalogue's name |
| `url` | where the archive was read from: a URL or a file path |
| `fetched` | when, as UTC `YYYY-MM-DDThh:mm:ssZ` |

## Outside an archive

The same mapping may also stand in a YAML file of its own, describing a
build that lives elsewhere: `kind` and `elf` are required, `arch` and `stamp`
are not (the machine's own, and the executable's modification time), and
`elf`, `fixed` and every tool are paths relative to that file, free to leave
its directory. Every other key means what it means in an archive.
