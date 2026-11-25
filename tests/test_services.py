"""dciab services: rendered DNS zones, chrony and Caddy configs, the pod, and the CLI plan."""

import copy
import os
import struct
import subprocess

import pytest
import yaml

from conftest import ROOT, load_example

from dciab_services import cli, probe, render

IMAGES = {"coredns": "example/coredns@sha256:aa", "caddy": "example/caddy@sha256:bb",
          "chronyBase": "example/rocky@sha256:cc", "chrony": "example/day0-chrony:4.6-x@sha256:dd"}


def bare():
    return copy.deepcopy(load_example("proxmox-bare"))


def files_for(doc):
    return render.render(doc, IMAGES, "localhost/dciab/day0-chrony:x", "/state/services",
                         "/state/isos", "/state/host-isos")


def zone_lines(text):
    return [line for line in text.splitlines() if not line.startswith(("$", "@"))]


def test_only_sites_with_hypervisors_run_full_services():
    assert render.wanted(bare()) and not render.dns_only(bare())
    assert not render.wanted(load_example("proxmox"))
    assert render.dns_only(load_example("proxmox"))


def existing():
    """A site whose hypervisors exist already, with NTP set: still DNS only."""
    doc = copy.deepcopy(load_example("proxmox"))
    doc["spec"]["time"] = {"servedBy": "dciab", "upstream": [], "serveLocalClock": True}
    return doc


def test_dns_only_pod_is_coredns_alone():
    files = files_for(existing())
    assert sorted(files) == ["coredns/Corefile", "coredns/zones/db.110.30.10.in-addr.arpa",
                             "coredns/zones/db.dc3.example.com", "pod.yaml"]
    spec = yaml.safe_load(files["pod.yaml"])["spec"]
    assert [c["name"] for c in spec["containers"]] == ["coredns"]
    assert spec["containers"][0]["image"] == IMAGES["coredns"]
    assert [v["name"] for v in spec["volumes"]] == ["coredns"]
    assert "bind 10.30.110.53 10.30.110.54" in files["coredns/Corefile"]
    net = render.network(existing())
    assert net["dnsOnly"] and not net["ntp"]


def test_dns_only_zone_has_the_registry_name():
    z = render.zones(existing())["dc3.example.com"]
    assert zone_lines(z) == ["dns1 IN A 10.30.110.53", "dns2 IN A 10.30.110.54",
                             "bootstrap IN A 10.30.110.5", "registry IN A 10.30.110.5"]


def test_full_mode_needs_coredns_caddy_and_the_pinned_chrony_image(monkeypatch):
    monkeypatch.delenv("DCIAB_CHRONY_IMAGE", raising=False)
    assert cli.pod_images(bare(), IMAGES) == [IMAGES["coredns"], IMAGES["caddy"], IMAGES["chrony"]]
    no_ntp = bare()
    no_ntp["spec"]["time"] = {"servedBy": "site", "servers": ["10.30.0.123"]}
    assert cli.pod_images(no_ntp, IMAGES) == [IMAGES["coredns"], IMAGES["caddy"]]
    assert cli.pod_images(existing(), IMAGES) == [IMAGES["coredns"]]
    # A local image (building and testing DCiaB) is neither loaded nor pulled.
    monkeypatch.setenv("DCIAB_CHRONY_IMAGE", "localhost/dciab/day0-chrony:test")
    assert cli.pod_images(bare(), IMAGES) == [IMAGES["coredns"], IMAGES["caddy"]]


def test_layout_names_come_from_the_payload(tmp_path):
    d = "sha256:" + "a" * 64
    (tmp_path / "images").mkdir()
    (tmp_path / "images.yaml").write_text(yaml.safe_dump({"images": [
        {"image": f"docker.io/coredns/coredns:1.14.6@{d}", "name": "docker.io_coredns_coredns_1.14.6_aaaa"}]}))
    assert cli.layout_names(tmp_path, [f"docker.io/coredns/coredns:1.14.6@{d}"]) == \
        ["docker.io_coredns_coredns_1.14.6_aaaa"]
    from dciab_seed.plan import SeedError
    with pytest.raises(SeedError, match="not in the payload"):
        cli.layout_names(tmp_path, ["docker.io/library/caddy:2@" + d])


def test_readiness_checks_the_registry_name_and_skips_http_in_dns_only(monkeypatch):
    answers = {}
    for ip in ("10.30.110.53", "10.30.110.54"):
        answers[(ip, "bootstrap.dc3.example.com", "A")] = ["10.30.110.5"]
        answers[(ip, "5.110.30.10.in-addr.arpa", "PTR")] = ["bootstrap.dc3.example.com"]
    queries = []

    def fake_dns(ip, name, qtype):
        queries.append((ip, name, qtype))
        return answers.get((ip, name, qtype), [])
    monkeypatch.setattr(probe, "dns", fake_dns)
    monkeypatch.setattr(probe, "http", lambda url: pytest.fail("no HTTP in DNS-only mode"))
    failed = cli.check(existing(), timeout=0)
    assert failed == [f"DNS on {ip}: registry.dc3.example.com doesn't resolve to 10.30.110.5"
                      for ip in ("10.30.110.53", "10.30.110.54")]
    assert queries == [q for ip in ("10.30.110.53", "10.30.110.54") for q in (
        (ip, "bootstrap.dc3.example.com", "A"), (ip, "5.110.30.10.in-addr.arpa", "PTR"),
        (ip, "registry.dc3.example.com", "A"))]
    answers[("10.30.110.53", "registry.dc3.example.com", "A")] = ["10.30.110.5"]
    assert len(cli.check(existing(), timeout=0)) == 1
    answers[("10.30.110.54", "registry.dc3.example.com", "A")] = ["10.30.110.5"]
    assert cli.check(existing(), timeout=0) == []


def test_readiness_retries_with_backoff_and_gives_up_at_the_deadline(monkeypatch):
    clock = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock[0] += s
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr(probe, "dns", lambda ip, name, qtype: None)   # no answer at all
    failed = cli.check(existing(), timeout=10)
    assert len(failed) == 6
    assert sleeps == [0.5, 1.0, 2.0, 4.0, 2.5]   # the last sleep stops at the deadline
    assert clock[0] == 10


def test_forward_zone_has_every_host():
    z = render.zones(bare())["dc3.example.com"]
    assert zone_lines(z) == [
        "dns1 IN A 10.30.110.53", "dns2 IN A 10.30.110.54",
        "bootstrap IN A 10.30.110.5", "registry IN A 10.30.110.5",
        "pve01 IN A 10.30.110.6", "pve02 IN A 10.30.110.7", "pve03 IN A 10.30.110.8",
    ]
    assert "@ IN NS dns1.dc3.example.com." in z


def test_reverse_zone_matches_forward_and_registry_has_no_ptr():
    z = render.zones(bare())["110.30.10.in-addr.arpa"]
    ptrs = zone_lines(z)
    assert "5 IN PTR bootstrap.dc3.example.com." in ptrs
    assert "6 IN PTR pve01.dc3.example.com." in ptrs
    assert not any("registry" in p for p in ptrs)
    assert len(ptrs) == 6


@pytest.mark.parametrize("cidr,zones", [
    ("10.30.110.0/24", ["110.30.10.in-addr.arpa"]),
    ("10.30.110.128/25", ["110.30.10.in-addr.arpa"]),
    ("10.30.108.0/22", [f"{i}.30.10.in-addr.arpa" for i in (108, 109, 110, 111)]),
    ("10.30.0.0/16", ["30.10.in-addr.arpa"]),
])
def test_reverse_zones_on_octet_boundaries(cidr, zones):
    assert [render.zone_name(n) for n in render.reverse_zones(cidr)] == zones


def test_wide_network_ptr_names_are_relative_to_the_zone():
    doc = bare()
    doc["spec"]["network"]["nodes"]["cidr"] = "10.30.0.0/16"
    z = render.zones(doc)["30.10.in-addr.arpa"]
    assert "6.110 IN PTR pve01.dc3.example.com." in z


def test_hypervisor_name_may_not_be_reserved():
    doc = bare()
    doc["spec"]["hypervisors"]["hosts"][0]["name"] = "registry"
    with pytest.raises(render.ServicesError, match="reserved"):
        render.records(doc)


def test_empty_upstream_means_authoritative_only():
    cf = render.corefile(bare())
    assert "forward" not in cf
    assert "\n. {" not in cf
    assert cf.count("bind 10.30.110.53 10.30.110.54") == 2


def test_upstream_resolvers_are_forwarded():
    doc = bare()
    doc["spec"]["network"]["upstreamResolvers"] = ["192.0.2.1", "192.0.2.2"]
    cf = render.corefile(doc)
    assert "forward . 192.0.2.1 192.0.2.2" in cf
    assert cf.count("bind 10.30.110.53 10.30.110.54") == 3


def test_chrony_one_server_per_service_ip_with_local_clock():
    confs = render.chrony_confs(bare())
    assert list(confs) == ["10.30.110.53", "10.30.110.54"]
    c = confs["10.30.110.53"]
    assert "bindaddress 10.30.110.53" in c
    assert "allow 10.30.110.0/24" in c
    assert "local stratum 10" in c
    assert "server" not in c


def test_chrony_follows_bootstrap_upstream():
    doc = bare()
    doc["spec"]["time"] = {"servedBy": "dciab", "upstream": ["ntp1.example.org"], "serveLocalClock": False}
    c = render.chrony_confs(doc)["10.30.110.54"]
    assert "server ntp1.example.org iburst" in c
    assert "local" not in c


def test_no_chrony_when_the_site_has_ntp():
    doc = bare()
    doc["spec"]["time"] = {"servedBy": "site", "servers": ["10.30.0.123"]}
    files = files_for(doc)
    assert not any(f.startswith("chrony") for f in files)
    pod = yaml.safe_load(files["pod.yaml"])
    assert [c["name"] for c in pod["spec"]["containers"]] == ["coredns", "caddy"]


def test_caddy_listens_on_the_bootstrap_address_only():
    c = render.caddyfile(bare())
    assert "http://10.30.110.5:80 {" in c
    assert "bind 10.30.110.5" in c
    assert "admin off" in c


def test_host_isos_only_for_each_hosts_bmc():
    c = render.caddyfile(bare())
    assert "path /host-isos /host-isos/*" in c
    assert "not remote_ip 10.30.0.21 10.30.0.22 10.30.0.23" in c          # BMCs, not the hosts
    assert "respond @hostisos_denied 403" in c
    # each BMC reads only its own host's ISO (answer file and root hash inside)
    assert "\t@hostiso_pve01 {\n\t\tpath /host-isos/pve01.iso\n\t\tnot remote_ip 10.30.0.21\n\t}" in c
    assert "respond @hostiso_pve03 403" in c
    assert "/answers" not in c


def test_invalid_upstream_resolver_is_rejected():
    doc = bare()
    doc["spec"]["network"]["upstreamResolvers"] = ["999.1.1.1"]
    with pytest.raises(render.ServicesError, match="not an IPv4 address"):
        render.corefile(doc)


def test_pod_uses_pinned_images_and_host_network():
    pod = yaml.safe_load(files_for(bare())["pod.yaml"])
    spec = pod["spec"]
    assert spec["hostNetwork"] is True
    images = {c["name"]: c["image"] for c in spec["containers"]}
    assert images == {"coredns": IMAGES["coredns"], "chrony-1": "localhost/dciab/day0-chrony:x",
                      "chrony-2": "localhost/dciab/day0-chrony:x", "caddy": IMAGES["caddy"]}
    paths = {v["name"]: v["hostPath"]["path"] for v in spec["volumes"]}
    assert paths["isos"] == "/state/isos" and paths["host-isos"] == "/state/host-isos"
    assert "&id" not in files_for(bare())["pod.yaml"]


def test_chrony_image_is_the_pinned_one_and_never_built(monkeypatch):
    monkeypatch.delenv("DCIAB_CHRONY_IMAGE", raising=False)
    assert cli.chrony_image(bare(), IMAGES) == IMAGES["chrony"]
    assert cli.chrony_image(existing(), IMAGES) is None
    for unpinned in ("", "example/day0-chrony:4.6-x"):
        with pytest.raises(render.ServicesError, match="day0.images.chrony isn't pinned by digest"):
            cli.chrony_image(bare(), {**IMAGES, "chrony": unpinned})
    monkeypatch.setenv("DCIAB_CHRONY_IMAGE", "localhost/dciab/day0-chrony:test")
    assert cli.chrony_image(bare(), {**IMAGES, "chrony": ""}) == "localhost/dciab/day0-chrony:test"


def test_services_step_never_builds_or_pulls_chrony_from_the_internet():
    """The chrony image is built at archive build time (images/day0-chrony, CI); the step only
    loads the pinned image like the others (issue #67)."""
    step = (ROOT / "lib" / "steps" / "services.sh").read_text()
    code = "\n".join(line for line in step.splitlines() if not line.lstrip().startswith("#"))
    for banned in ("podman build", "buildah", "microdnf", "dnf ", "images/day0-chrony", "CHRONY_BASE"):
        assert banned not in code, banned
    # The only pull is the generic one for the pinned images, when there is no payload.
    assert code.count("podman pull") == 2
    assert 'podman pull -q "oci:$payload/images:' in code
    assert "dciab services doesn't build it" in code
    helper = (ROOT / "lib" / "dciab_services" / "cli.py").read_text()
    assert "chronyBase" not in helper and "Containerfile" not in helper


def test_rendering_is_idempotent_and_changes_follow_config(tmp_path):
    files = files_for(bare())
    assert files == files_for(bare())
    conf = tmp_path / "services"
    assert cli.write(files, conf)
    assert cli.write(files, conf) == []
    for p in conf.rglob("*"):
        assert (p.stat().st_mode & 0o777) == (0o700 if p.is_dir() else 0o600)
    doc = bare()
    doc["spec"]["hypervisors"]["hosts"].pop()
    changed = files_for(doc)
    assert render.fingerprint(changed) != render.fingerprint(files)
    assert cli.write(changed, conf) == ["Caddyfile", "coredns/zones/db.110.30.10.in-addr.arpa",
                                        "coredns/zones/db.dc3.example.com"]


def test_relative_state_and_isos_become_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = cli.Paths(bare(), cli.Path("st"), "isos")
    assert paths.conf == tmp_path / "st" / "services"
    assert paths.isos == tmp_path / "isos"


def test_dropped_ntp_removes_chrony_configs(tmp_path):
    conf = tmp_path / "services"
    cli.write(files_for(bare()), conf)
    doc = bare()
    doc["spec"]["time"] = {"servedBy": "site", "servers": ["10.30.0.123"]}
    changed = cli.write(files_for(doc), conf)
    assert "chrony-1.conf (removed)" in changed
    assert not (conf / "chrony-1.conf").exists()


def test_dns_answer_parsing():
    name = probe._qname("pve01.dc3.example.com")
    header = struct.pack("!6H", 1, 0x8180, 1, 2, 0, 0)
    question = name + struct.pack("!HH", 1, 1)
    a = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 300, 4) + bytes([10, 30, 110, 6])
    ptr_target = probe._qname("pve01.dc3.example.com")
    ptr = b"\xc0\x0c" + struct.pack("!HHIH", 12, 1, 300, len(ptr_target)) + ptr_target
    msg = header + question + a + ptr
    assert probe.parse_answers(msg, 1) == ["10.30.110.6"]
    assert probe.parse_answers(msg, 12) == ["pve01.dc3.example.com"]
    refused = struct.pack("!6H", 1, 0x8185, 1, 0, 0, 0) + question
    assert probe.parse_answers(refused, 1) == []
    assert probe.reverse_name("10.30.110.6") == "6.110.30.10.in-addr.arpa"


# CLI


def bare_site(tmp_path):
    from conftest import SECRET_FILES, intermediate_pem
    for rel in SECRET_FILES:
        p = tmp_path / "secrets" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(intermediate_pem() if rel == "pki/intermediate.crt" else "x\n")
        p.chmod(0o600)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text((ROOT / "examples" / "proxmox-bare.yaml").read_text())
    return cfg


CHRONY_LOCAL = "localhost/dciab/day0-chrony:test"


def run(*args, env_extra=None):
    env = {**os.environ, "DCIAB_PVE_TOKEN": "x", "DCIAB_PVE_CSI_TOKEN": "y", "DCIAB_CHRONY_IMAGE": CHRONY_LOCAL, **(env_extra or {})}
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run([str(ROOT / "dciab"), "services", *map(str, args)],
                          capture_output=True, text=True, env=env)


def test_print_shows_the_plan_and_changes_nothing(tmp_path):
    cfg = bare_site(tmp_path)
    r = run(cfg, "--print")
    assert r.returncode == 0, r.stderr
    assert "address:     10.30.110.53/24 (DNS, NTP)" in r.stdout
    assert "http://10.30.110.5:80" in r.stdout
    assert "--- " + str(tmp_path / ".dciab/state/services/coredns/Corefile") in r.stdout
    assert "6 IN PTR pve01.dc3.example.com." in r.stdout
    assert f"chrony:      {CHRONY_LOCAL} (DCIAB_CHRONY_IMAGE: a local image, not the pinned one)" in r.stdout
    assert "warning: DCIAB_CHRONY_IMAGE is set" in r.stderr
    assert not (tmp_path / ".dciab").exists()


def test_print_uses_the_pinned_chrony_image_or_refuses_an_unpinned_one(tmp_path):
    cfg = bare_site(tmp_path)
    r = run(cfg, "--print", env_extra={"DCIAB_CHRONY_IMAGE": None})
    pinned = yaml.safe_load((ROOT / "bundle.lock").read_text())["day0"]["images"].get("chrony") or ""
    assert "DCIAB_CHRONY_IMAGE" not in r.stderr
    if "@sha256:" in pinned:
        assert r.returncode == 0, r.stderr
        assert f"chrony:      {pinned}" in r.stdout
        assert pinned in r.stdout.split("images:", 1)[1].splitlines()[0]
    else:
        assert r.returncode == 1
        assert "day0.images.chrony isn't pinned by digest yet" in r.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="runs as root")
def test_services_needs_root(tmp_path):
    r = run(bare_site(tmp_path))
    assert r.returncode == 1
    assert "must run as root" in r.stderr


def test_site_without_hypervisors_gets_dns_only(tmp_path):
    bare_site(tmp_path)
    cfg = tmp_path / "proxmox.yaml"
    cfg.write_text((ROOT / "examples" / "proxmox.yaml").read_text())
    r = run(cfg, "--print")
    assert r.returncode == 0, r.stderr
    assert "mode:        DNS only" in r.stdout
    assert "address:     10.30.110.53/24 (DNS)" in r.stdout
    assert "HTTP:" not in r.stdout and "Caddyfile" not in r.stdout
    assert "registry IN A 10.30.110.5" in r.stdout
    assert not (tmp_path / ".dciab").exists()


def test_payload_must_hold_the_pinned_image(tmp_path):
    bare_site(tmp_path)
    cfg = tmp_path / "proxmox.yaml"
    cfg.write_text((ROOT / "examples" / "proxmox.yaml").read_text())
    payload = tmp_path / "payload"
    (payload / "images").mkdir(parents=True)
    (payload / "images.yaml").write_text(yaml.safe_dump({"images": [
        {"image": "docker.io/library/other:1@sha256:" + "b" * 64, "name": "other"}]}))
    r = run(cfg, "--print", "--payload", payload)
    assert r.returncode == 1
    assert "is not in the payload" in r.stderr
    r = run(cfg, "--print", env_extra={"DCIAB_PAYLOAD": str(tmp_path / "nope")})
    assert r.returncode == 1 and "not an archive payload" in r.stderr


def test_invalid_config_stops_before_anything(tmp_path):
    cfg = bare_site(tmp_path)
    doc = yaml.safe_load(cfg.read_text())
    doc["spec"]["network"]["dnsServiceIPs"] = ["10.30.110.53"]
    cfg.write_text(yaml.safe_dump(doc))
    r = run(cfg, "--print")
    assert r.returncode == 1
    assert "is not valid" in r.stderr


def test_unknown_step_is_rejected(tmp_path):
    r = run(bare_site(tmp_path), "--until", "nope")
    assert r.returncode == 1
    assert "--until must be one of" in r.stderr


def test_chrony_image_is_built_by_ci_from_the_pinned_base():
    """images/day0-chrony/build.sh: a recipe id from the pinned base and the Containerfile;
    CI publishes the image once per version, checking while logged in (issue #67)."""
    build = ROOT / "images" / "day0-chrony" / "build.sh"
    r = subprocess.run([str(build), "--recipe-id"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    import hashlib
    base = yaml.safe_load((ROOT / "bundle.lock").read_text())["day0"]["images"]["chronyBase"]
    containerfile = (build.parent / "Containerfile").read_text()
    assert r.stdout.strip() == hashlib.sha256(f"{base}\n{containerfile}".encode()).hexdigest()[:12]
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    job = ci[ci.index("\n  day0-chrony:"):]
    job = job[:job.index("\n  frozen-classes:")]
    assert "images/day0-chrony/build.sh" in job and "packages: write" in job
    assert "github.ref == 'refs/heads/main'" in job
    assert job.index("podman login") < job.index("skopeo inspect") < job.index("podman push")


def test_pinned_chrony_image_is_built_from_this_recipe():
    """A recipe or base change needs the image CI publishes on main pinned again."""
    pinned = yaml.safe_load((ROOT / "bundle.lock").read_text())["day0"]["images"]["chrony"]
    assert "@sha256:" in pinned
    r = subprocess.run([str(ROOT / "images" / "day0-chrony" / "build.sh"), "--recipe-id"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert pinned.split("@", 1)[0].endswith("-" + r.stdout.strip()), \
        "images/day0-chrony changed: pin the image CI publishes on main as day0.images.chrony"


def test_services_records_what_it_adds_to_the_host(tmp_path):
    """dciab seed --remove undoes only what <state>/services/created lists."""
    step = (ROOT / "lib" / "steps" / "services.sh").read_text()
    record = step[step.index("record() {"):]
    record = record[:record.index("\n}\n") + 3]
    script = (f'set -euo pipefail\nCONF_DIR="{tmp_path}"\n{record}'
              'record address eth0 10.0.0.53/24\nrecord port public 53/udp\nrecord address eth0 10.0.0.53/24\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    created = tmp_path / "created"
    assert created.read_text() == "address eth0 10.0.0.53/24\nport public 53/udp\n"
    assert created.stat().st_mode & 0o777 == 0o600
    for call in ('record address "$nic" "$want"', 'record port "$zone" "$p"', 'record pod "$POD"'):
        assert call in step
