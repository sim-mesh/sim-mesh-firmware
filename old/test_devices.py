"""Devices: names, expansion, the survey of catalogues from a directory and
over HTTP, fetching a latest build when used, saving, importing, workspace
builds, and what a device name resolves to."""

import asyncio
import os
import sys
import zipfile

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import devices  # noqa: E402

ARCH = "aarch64"
OTHER = "x86_64"


def package_zip(path, arch=ARCH, stamp="20260925035045", kind="reticulous",
                extra=None, node=None):
    doc = node if node is not None else {
        "kind": kind, "arch": arch, "stamp": stamp, "elf": "reticulous.elf",
        "fixed": "fixed", "project": "Reticulous", "catalogue": "dev",
        "entry": "hw-sim-mesh-%s" % arch}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("node.yaml", yaml.safe_dump(doc))
        info = zipfile.ZipInfo("reticulous.elf")
        info.external_attr = 0o644 << 16
        zf.writestr(info, "#!/bin/sh\necho station\n")
        zf.writestr("fixed/webroot/index.html", "<p>hi</p>")
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return path


def catalogue(directory, images, attrs=""):
    """A catalogue directory: the given zips and an index.html listing them,
    plus a board image that is not a device file."""
    os.makedirs(directory, exist_ok=True)
    rows = []
    for name, arch, stamp in images:
        package_zip(os.path.join(directory, name), arch=arch, stamp=stamp)
        rows.append('<li><a href="%s"%s>%s</a> <small>(1 KB)</small></li>'
                    % (name, attrs, name))
    board = "reticulous_hw-heltecv4_20260925035045.zip"
    with open(os.path.join(directory, board), "wb") as f:
        f.write(b"not a device")
    rows.append('<li><a href="%s" data-onboarding="device">%s</a></li>' % (board, board))
    with open(os.path.join(directory, "index.html"), "w") as f:
        f.write("<!doctype html>\n<title>x</title>\n<ul>\n%s\n</ul>\n" % "\n".join(rows))
    return directory


def image(stamp, arch=ARCH, slug="reticulous"):
    return ("%s_hw-sim-mesh-%s_%s.zip" % (slug, arch, stamp), arch, stamp)


def quiet(lines):
    return lines.append


def survey(sources, dd, said=None):
    return asyncio.run(devices.survey(sources, dd, ARCH, quiet(said if said is not None else [])))


# ---- names ---------------------------------------------------------------------

def test_an_image_name_splits_from_the_right():
    assert devices.split_image_name("reticulous_hw-sim-mesh-aarch64_20260925035045.zip") == \
        ("reticulous", "hw-sim-mesh-aarch64", "20260925035045")
    assert devices.split_image_name("reticulous_odd_entry_name_1.zip") == \
        ("reticulous", "odd_entry_name", "1")
    assert devices.split_image_name("reticulous_generic_abc.zip") is None
    assert devices.split_image_name("index.html") is None
    assert devices.entry_arch("hw-sim-mesh-x86_64") == "x86_64"
    assert devices.entry_arch("hw-heltecv4") is None


def test_a_device_name_is_project_catalogue_and_stamp_or_latest():
    assert devices.split_name("reticulous_dev_20260927140352") == \
        ("reticulous", "dev", "20260927140352")
    assert devices.split_name("reticulous_dev_latest") == ("reticulous", "dev", "latest")
    assert devices.split_name("reticulous_my_cat_latest") == ("reticulous", "my_cat", "latest")
    assert devices.split_name("reticulous_latest") is None
    assert devices.split_name("dev") is None
    assert devices.split_name("reticulous_dev_newest") is None


def test_the_newest_package_per_project_for_this_arch_is_picked():
    links = devices.parse_listing(
        '<a href="r_hw-sim-mesh-aarch64_2.zip" data-target="linux">a</a>'
        '<a href="r_hw-sim-mesh-aarch64_4.zip">b</a>'
        '<a href="r_hw-sim-mesh-aarch64_1.zip">b</a>'
        '<a href="s_hw-sim-mesh-aarch64_3.zip">b</a>'
        '<a href="r_hw-sim-mesh-x86_64_9.zip">c</a>'
        '<a href="r_hw-heltecv4_9.zip">d</a><a name="x">no href</a>')
    newest = devices.newest_packages(links, ARCH)
    assert {k: v["stamp"] for k, v in newest.items()} == {"r": "4", "s": "3"}
    assert devices.newest_packages(links, OTHER)["r"]["stamp"] == "9"


def test_a_sites_index_lists_its_catalogues():
    links = devices.parse_listing('<a href="stable/">stable</a><a href="dev/">dev</a>'
                                  '<a href="../">up</a><a href="x.zip">x</a>'
                                  '<a href="https://elsewhere/y/">y</a>')
    assert devices.catalogue_names(links) == ["stable", "dev"]


def test_a_source_is_a_name_a_url_or_a_directory(tmp_path):
    s = devices.Source("stable", "https://example.net/builds/")
    assert (s.name, s.location, s.local) == ("stable", "https://example.net/builds/stable/", False)
    s = devices.Source("https://github.com/o/r/releases/download/catalogue-dev")
    assert s.name == "dev" and s.location.endswith("catalogue-dev/")
    d = catalogue(str(tmp_path / "rop"), [])
    s = devices.Source(d)
    assert (s.name, s.local) == ("rop", True)
    with pytest.raises(devices.DeviceError):
        devices.Source(str(tmp_path / "nowhere" / "x"))
    with pytest.raises(devices.DeviceError, match="cannot be called"):
        devices.Source("imported")
    assert devices.Source(catalogue(str(tmp_path / "local"), [])).name == "local"
    assert devices.Source(catalogue(str(tmp_path / "imported"), [])).name == "builds-imported"


def test_the_builds_beside_sim_mesh_are_sources(tmp_path):
    catalogue(str(tmp_path / "builds" / "dev"), [])
    os.makedirs(str(tmp_path / "builds" / "elf"))
    assert devices.builds_sources(str(tmp_path / "builds")) == [str(tmp_path / "builds" / "dev")]
    assert devices.builds_sources(str(tmp_path / "nowhere")) == []


def test_a_device_is_called_by_its_name_or_its_project_catalogue_and_time():
    assert devices.display_name({"name": "Mine", "stamp": "1"}) == "Mine"
    assert devices.display_name({"project": "Reticulous", "stamp": "20260925035045"}, "dev") == \
        "Reticulous dev 2026-09-25 03:50"


# ---- expanding ---------------------------------------------------------------

def test_expanding_checks_node_yaml_and_makes_the_elf_and_tools_executable(tmp_path):
    node = {"kind": "sergeyculum", "arch": ARCH, "stamp": "20260925035045",
            "elf": "reticulous.elf", "tools": {"rncfg": "bin/rncfg"}, "env": {"A": "./x", "B": "y"},
            "virtual_hardware": "nRF52840"}
    z = package_zip(str(tmp_path / "p.zip"), node=node, extra={"bin/rncfg": "tool"})
    dest = str(tmp_path / "devices" / "latest" / "sergeyculum_dev_20260925035045")
    devices.expand(z, dest, {"catalogue": "dev", "url": z}, ARCH)
    assert os.access(os.path.join(dest, "reticulous.elf"), os.X_OK)
    assert os.access(os.path.join(dest, "bin", "rncfg"), os.X_OK)
    assert devices.read_origin(dest)["catalogue"] == "dev"
    got = devices.package_result(dest)
    assert got["tools"] == {"rncfg": os.path.join(dest, "bin", "rncfg")}
    assert got["env"] == {"A": os.path.join(dest, "x"), "B": "y"}
    assert got["virtual_hardware"] == "nRF52840" and got["kind_type"] == "sergeyculum"
    assert (got["ref"], got["project"], got["catalogue"]) == \
        ("sergeyculum_dev_20260925035045", "sergeyculum", "dev")
    assert not [n for n in os.listdir(os.path.dirname(dest)) if n.startswith(".part-")]


@pytest.mark.parametrize("zip_kwargs, why", [
    ({"arch": OTHER}, "this machine is"),
    ({"stamp": "20260101000000"}, "the filename"),
    ({"extra": {"../escape": "x"}}, "leaves the package"),
    ({"node": {"kind": "reticulous", "arch": ARCH, "stamp": "20260925035045"}}, "no `elf`"),
    ({"node": {"kind": "reticulous", "arch": ARCH, "stamp": "20260925035045",
               "elf": "missing.elf"}}, "not in the package"),
    ({"node": {"kind": "reticulous", "arch": ARCH, "stamp": "20260925035045",
               "elf": "reticulous.elf", "tools": {"t": "gone"}}}, "tool gone"),
])
def test_a_bad_package_leaves_nothing_behind(tmp_path, zip_kwargs, why):
    z = package_zip(str(tmp_path / "p.zip"), **zip_kwargs)
    dest = str(tmp_path / "devices" / "latest" / "reticulous_dev_20260925035045")
    with pytest.raises(devices.DeviceError, match=why):
        devices.expand(z, dest, {}, ARCH)
    assert os.listdir(os.path.dirname(dest)) == []


# ---- the survey and latest -------------------------------------------------------

def test_a_survey_notes_the_newest_per_catalogue_and_fetches_nothing(tmp_path):
    dev = catalogue(str(tmp_path / "builds" / "dev"),
                    [image("20260901000000"), image("20260902000000"),
                     image("20260903000000", OTHER), image("20260801000000", slug="sergeyculum")],
                    attrs=' data-target="linux"')
    rop = catalogue(str(tmp_path / "builds" / "rop"), [image("20260905000000")])
    dd = str(tmp_path / "devices")
    index, changed = survey([dev, rop], dd)
    assert changed
    assert {k: v["stamp"] for k, v in index.items()} == {
        "reticulous_dev": "20260902000000", "reticulous_rop": "20260905000000",
        "sergeyculum_dev": "20260801000000"}
    assert devices.fetched("reticulous_dev", dd) == []
    rows = {r["ref"]: r for r in devices.listing(dd, ARCH)["latest"]}
    assert rows["reticulous_dev_latest"]["fetched"] is False
    assert rows["reticulous_dev_latest"]["source"] == "builds"
    # What it plays and its kind, read from the zip's node.yaml, before any fetch.
    assert rows["reticulous_dev_latest"]["kind"] == "reticulous"
    assert rows["reticulous_dev_latest"]["name"] == "Reticulous dev 2026-09-02 00:00"
    assert survey([dev, rop], dd)[1] is False
    with pytest.raises(devices.DeviceError, match="not fetched yet"):
        devices.resolve("reticulous_dev_latest", devices_dir=dd, arch=ARCH)


def test_a_latest_build_is_fetched_when_used_and_removed_when_a_newer_is_seen(tmp_path):
    src = str(tmp_path / "builds" / "dev")
    dd = str(tmp_path / "devices")
    catalogue(src, [image("20260901000000")])
    survey([src], dd)
    got = asyncio.run(devices.ensure("reticulous_dev_latest", devices_dir=dd, arch=ARCH,
                                     say=quiet([]), look=False))
    assert got["stamp"] == "20260901000000" and got["ref"] == "reticulous_dev_20260901000000"
    assert got["elf"].startswith(os.path.join(dd, "latest"))
    assert devices.resolve("reticulous_dev_latest", devices_dir=dd, arch=ARCH)["dir"] == got["dir"]
    rows = {r["ref"]: r for r in devices.listing(dd, ARCH)["latest"]}
    assert rows["reticulous_dev_latest"]["fetched"] and rows["reticulous_dev_latest"]["kind"] == \
        "reticulous"

    # make-builds leaves one image per entry, so a new round replaces it.
    catalogue(src, [image("20260902000000")])
    said = []
    survey([src], dd, said)
    assert devices.fetched("reticulous_dev", dd) == []
    assert any("removed reticulous_dev_20260901000000" in line for line in said)
    assert not devices.listing(dd, ARCH)["latest"][0]["fetched"]


def test_a_local_catalogue_joins_the_one_of_its_name_the_newest_winning(tmp_path):
    web = catalogue(str(tmp_path / "web" / "dev"), [image("20260901000000")])
    mine = catalogue(str(tmp_path / "builds" / "dev"), [image("20260902000000")])
    dd = str(tmp_path / "devices")
    index, _ = survey([web, mine], dd)
    assert index["reticulous_dev"]["where"].startswith(mine)
    index, changed = survey([web], dd)
    assert not changed and index["reticulous_dev"]["stamp"] == "20260902000000"


def test_one_unreadable_source_does_not_stop_the_others(tmp_path):
    src = catalogue(str(tmp_path / "dev"), [image("5")])
    said = []
    index, _ = survey(["./not/a/catalogue", src], str(tmp_path / "devices"), said)
    assert list(index) == ["reticulous_dev"]
    assert any(line.startswith("./not/a/catalogue") for line in said)


def test_the_survey_and_the_fetch_over_http(tmp_path):
    from aiohttp import web

    catalogue(str(tmp_path / "site" / "dev"), [image("7")])
    with open(str(tmp_path / "site" / "index.html"), "w") as f:
        f.write('<a href="dev/">dev</a><a href="stable/">stable</a>')
    dd = str(tmp_path / "devices")

    async def go():
        app = web.Application()
        app.router.add_static("/builds/", str(tmp_path / "site"))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = "http://127.0.0.1:%d/builds/" % port
        said = []
        try:
            index, _ = await devices.survey(None, dd, ARCH, said.append, base=base)
            got = await devices.ensure("reticulous_dev_latest", devices_dir=dd, arch=ARCH,
                                       say=said.append, look=False)
            return index, got, said
        finally:
            await runner.cleanup()

    index, got, said = asyncio.run(go())
    assert list(index) == ["reticulous_dev"]
    assert any(line.startswith("stable:") for line in said)
    assert got["source"].endswith("/builds/dev/reticulous_hw-sim-mesh-aarch64_7.zip")
    # Its node.yaml was read by range requests, before the fetch.
    assert index["reticulous_dev"]["node"] == {"kind": "reticulous", "project": "Reticulous",
                                               "arch": ARCH}


# ---- saving and importing ------------------------------------------------------

def test_a_latest_build_is_saved_as_a_copy_that_stays(tmp_path):
    src = str(tmp_path / "builds" / "dev")
    dd = str(tmp_path / "devices")
    catalogue(src, [image("20260901000000")])
    survey([src], dd)
    got = asyncio.run(devices.save("reticulous_dev_latest", dd, ARCH, quiet([])))
    assert got["ref"] == "reticulous_dev_20260901000000"
    assert got["dir"] == os.path.join(dd, "saved", "reticulous_dev_20260901000000")
    with pytest.raises(devices.DeviceError, match="saved already"):
        asyncio.run(devices.save("reticulous_dev_latest", dd, ARCH, quiet([])))
    catalogue(src, [image("20260902000000")])
    survey([src], dd)
    assert devices.resolve("reticulous_dev_20260901000000", devices_dir=dd,
                           arch=ARCH)["dir"] == got["dir"]
    assert [r["ref"] for r in devices.listing(dd, ARCH)["saved"]] == \
        ["reticulous_dev_20260901000000"]
    devices.delete_saved("reticulous_dev_20260901000000", dd)
    assert devices.listing(dd, ARCH)["saved"] == []
    with pytest.raises(devices.DeviceError, match="no saved build"):
        devices.delete_saved("reticulous_dev_20260901000000", dd)


def test_an_imported_zip_of_any_name_is_saved_named_from_its_node_yaml(tmp_path):
    dd = str(tmp_path / "devices")
    z = package_zip(str(tmp_path / "upload.zip"))
    got = devices.import_zip(z, "whatever.zip", dd, ARCH)
    assert got["ref"] == "reticulous_imported_20260925035045"
    assert devices.resolve(got["ref"], devices_dir=dd, arch=ARCH)["catalogue"] == "imported"
    with pytest.raises(devices.DeviceError, match="already"):
        devices.import_zip(z, "again.zip", dd, ARCH)
    other = package_zip(str(tmp_path / "other.zip"), arch=OTHER)
    with pytest.raises(devices.DeviceError, match="built for x86_64"):
        devices.import_zip(other, "other.zip", dd, ARCH)
    mesh = package_zip(str(tmp_path / "m.zip"), node={
        "kind": "sergeyculum", "arch": ARCH, "stamp": "7", "elf": "reticulous.elf"})
    assert devices.import_zip(mesh, "m.zip", dd, ARCH)["ref"] == "sergeyculum_imported_7"
    bare = str(tmp_path / "bare.zip")
    with zipfile.ZipFile(bare, "w") as zf:
        zf.writestr("x", "y")
    with pytest.raises(devices.DeviceError, match="no node.yaml"):
        devices.import_zip(bare, "bare.zip", dd, ARCH)


# ---- compiled builds and paths ----------------------------------------------------

def compiled(tmp_path):
    dd = tmp_path / "devices"
    build = tmp_path / "ws" / "fw" / "target"
    build.mkdir(parents=True)
    (build / "sim-mesh").write_text("elf")
    (build / "rncfg").write_text("tool")
    (dd / "local").mkdir(parents=True)
    (dd / "local" / "sergeyculum_local.yaml").write_text(
        "kind: sergeyculum\nproject: Sergeyculum\nvirtual_hardware: nRF52840\n"
        "elf: ../../ws/fw/target/sim-mesh\ntools: { rncfg: ../../ws/fw/target/rncfg }\n")
    (dd / "local" / "broken_local.yaml").write_text("kind: sergeyculum\nelf: ../../nowhere\n")
    (dd / "local" / "oddly-named.yaml").write_text("kind: sergeyculum\nelf: x\n")
    return str(dd), build


def test_a_compiled_build_is_the_latest_of_its_catalogue_run_in_place(tmp_path):
    dd, build = compiled(tmp_path)
    got = devices.resolve("sergeyculum_local_latest", devices_dir=dd, arch=ARCH)
    assert got["elf"] == str(build / "sim-mesh")
    assert got["tools"] == {"rncfg": str(build / "rncfg")}
    assert (got["project"], got["catalogue"], got["virtual_hardware"]) == \
        ("sergeyculum", "local", "nRF52840")
    assert got["name"].startswith("Sergeyculum local ")
    with pytest.raises(devices.DeviceError, match=devices.NOT_BUILT):
        devices.resolve("broken_local_latest", devices_dir=dd, arch=ARCH)
    rows = {r["ref"]: r for r in devices.listing(dd, ARCH)["latest"]}
    assert set(rows) == {"sergeyculum_local_latest", "broken_local_latest"}
    assert rows["sergeyculum_local_latest"]["source"] == "compiled"
    assert "error" in rows["broken_local_latest"]


def test_a_compiled_build_saved_gathers_its_pieces_into_a_package(tmp_path):
    dd, _ = compiled(tmp_path)
    got = asyncio.run(devices.save("sergeyculum_local_latest", dd, ARCH, quiet([])))
    assert got["ref"].startswith("sergeyculum_local_")
    assert os.path.isfile(got["tools"]["rncfg"]) and got["kind_type"] == "sergeyculum"
    assert got["virtual_hardware"] == "nRF52840"


def test_a_path_resolves_to_a_package_or_a_workspace_build(tmp_path):
    z = package_zip(str(tmp_path / "p.zip"), stamp="20260920000000")
    pkg = devices.expand(z, str(tmp_path / "d" / "reticulous_dev_20260920000000"), {}, ARCH)
    assert devices.resolve(pkg, arch=ARCH)["stamp"] == "20260920000000"
    with pytest.raises(devices.DeviceError, match="built for aarch64"):
        devices.resolve(pkg, arch=OTHER)

    build = tmp_path / "ws" / "reticulous" / "esp-idf" / "build.linux"
    (build / "data_merged").mkdir(parents=True)
    (build / "reticulous.elf").write_text("elf")
    base = tmp_path / "ws" / "sim-mesh" / "testbed"
    base.mkdir(parents=True)
    got = devices.resolve("../../reticulous/esp-idf/build.linux", base_dir=str(base), arch=ARCH)
    assert got["elf"] == str(build / "reticulous.elf")
    assert got["fixed"] == str(build / "data_merged")
    assert got["kind_type"] == "reticulous" and got["node"] is None
    assert len(got["stamp"]) == 14 and got["stamp"].isdigit()
    with pytest.raises(devices.DeviceError, match="neither"):
        devices.resolve(str(base), arch=ARCH)
    with pytest.raises(devices.DeviceError, match="a device is"):
        devices.resolve("stable", arch=ARCH)


def test_the_cli_resolves_as_json(tmp_path, capsys):
    build = tmp_path / "build.linux"
    build.mkdir()
    (build / "reticulous.elf").write_text("elf")
    assert devices.main(["resolve", str(build)]) == 0
    assert '"kind_type": "reticulous"' in capsys.readouterr().out
    assert devices.main(["resolve", str(tmp_path / "missing")]) == 1
