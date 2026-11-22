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
          "chronyBase": "example/rocky@sha256:cc"}


def bare():
    return copy.deepcopy(load_example("proxmox-bare"))


def files_for(doc):
    return render.render(doc, IMAGES, "localhost/dciab/day0-chrony:x", "/state/services",
                         "/state/isos", "/state/answers")


def zone_lines(text):
    return [line for line in text.splitlines() if not line.startswith(("$", "@"))]


def test_only_sites_with_hypervisors_run_full_services():
    assert render.wanted(bare()) and not render.dns_only(bare())
    assert not render.wanted(load_example("proxmox"))
    assert render.dns_only(load_example("proxmox"))


def existing():
    """A site whose hypervisors exist already, with NTP set: still DNS only."""
    doc = copy.deepcopy(load_example("proxmox"))
    doc["spec"]["bootstrap"]["ntp"] = {"upstream": [], "serveLocalClock": True}
    return doc


def test_dns_only_pod_is_coredns_alone():
    files = files_for(existing())
    assert sorted(files) == ["coredns/Corefile", "coredns/zones/db.110.30.10.in-addr.arpa",
                             "coredns/zones/db.lab1.example.com", "pod.yaml"]
    spec = yaml.safe_load(files["pod.yaml"])["spec"]
    assert [c["name"] for c in spec["containers"]] == ["coredns"]
    assert spec["containers"][0]["image"] == IMAGES["coredns"]
    assert [v["name"] for v in spec["volumes"]] == ["coredns"]
    assert "bind 10.30.110.53 10.30.110.54" in files["coredns/Corefile"]
    net = render.network(existing())
    assert net["dnsOnly"] and not net["ntp"]


def test_dns_only_zone_has_the_registry_name():
    z = render.zones(existing())["lab1.example.com"]
    assert zone_lines(z) == ["dns1 IN A 10.30.110.53", "dns2 IN A 10.30.110.54",
                             "bootstrap IN A 10.30.110.5", "registry IN A 10.30.110.5"]


def test_full_mode_needs_coredns_caddy_and_chrony_base():
    assert cli.pod_images(bare(), IMAGES) == [IMAGES["coredns"], IMAGES["caddy"], IMAGES["chronyBase"]]
    no_ntp = bare()
    del no_ntp["spec"]["bootstrap"]["ntp"]
    assert cli.pod_images(no_ntp, IMAGES) == [IMAGES["coredns"], IMAGES["caddy"]]
    assert cli.pod_images(existing(), IMAGES) == [IMAGES["coredns"]]


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
        answers[(ip, "bootstrap.lab1.example.com", "A")] = ["10.30.110.5"]
        answers[(ip, "5.110.30.10.in-addr.arpa", "PTR")] = ["bootstrap.lab1.example.com"]
    monkeypatch.setattr(probe, "dns", lambda ip, name, qtype: answers.get((ip, name, qtype), []))
    monkeypatch.setattr(probe, "http", lambda url: pytest.fail("no HTTP in DNS-only mode"))
    failed = cli.check(existing(), timeout=0)
    assert failed == [f"DNS on {ip}: registry.lab1.example.com doesn't resolve to 10.30.110.5"
                      for ip in ("10.30.110.53", "10.30.110.54")]
    answers[("10.30.110.53", "registry.lab1.example.com", "A")] = ["10.30.110.5"]
    assert len(cli.check(existing(), timeout=0)) == 1
    answers[("10.30.110.54", "registry.lab1.example.com", "A")] = ["10.30.110.5"]
    assert cli.check(existing(), timeout=0) == []


def test_forward_zone_has_every_host():
    z = render.zones(bare())["lab1.example.com"]
    assert zone_lines(z) == [
        "dns1 IN A 10.30.110.53", "dns2 IN A 10.30.110.54",
        "bootstrap IN A 10.30.110.5", "registry IN A 10.30.110.5",
        "pve01 IN A 10.30.110.6", "pve02 IN A 10.30.110.7", "pve03 IN A 10.30.110.8",
    ]
    assert "@ IN NS dns1.lab1.example.com." in z


def test_reverse_zone_matches_forward_and_registry_has_no_ptr():
    z = render.zones(bare())["110.30.10.in-addr.arpa"]
    ptrs = zone_lines(z)
    assert "5 IN PTR bootstrap.lab1.example.com." in ptrs
    assert "6 IN PTR pve01.lab1.example.com." in ptrs
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
    assert "6.110 IN PTR pve01.lab1.example.com." in z


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
    doc["spec"]["bootstrap"]["ntp"] = {"upstream": ["ntp1.example.org"], "serveLocalClock": False}
    c = render.chrony_confs(doc)["10.30.110.54"]
    assert "server ntp1.example.org iburst" in c
    assert "local" not in c


def test_no_chrony_when_the_site_has_ntp():
    doc = bare()
    del doc["spec"]["bootstrap"]["ntp"]
    files = files_for(doc)
    assert not any(f.startswith("chrony") for f in files)
    pod = yaml.safe_load(files["pod.yaml"])
    assert [c["name"] for c in pod["spec"]["containers"]] == ["coredns", "caddy"]


def test_caddy_listens_on_the_bootstrap_address_only():
    c = render.caddyfile(bare())
    assert "http://10.30.110.5:80 {" in c
    assert "bind 10.30.110.5" in c
    assert "admin off" in c


def test_answer_files_only_for_hypervisor_hosts():
    c = render.caddyfile(bare())
    assert "path /answers /answers/*" in c
    assert "not remote_ip 10.30.110.6 10.30.110.7 10.30.110.8" in c
    assert "respond @answers_denied 403" in c


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
    assert paths["isos"] == "/state/isos" and paths["answers"] == "/state/answers"
    assert "&id" not in files_for(bare())["pod.yaml"]


def test_chrony_image_tag_follows_recipe_and_base():
    a = render.chrony_image("FROM x", "base@sha256:1")
    assert a.startswith("localhost/dciab/day0-chrony:")
    assert a == render.chrony_image("FROM x", "base@sha256:1")
    assert a != render.chrony_image("FROM y", "base@sha256:1")
    assert a != render.chrony_image("FROM x", "base@sha256:2")


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
                                        "coredns/zones/db.lab1.example.com"]


def test_relative_state_and_isos_become_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = cli.Paths(bare(), cli.Path("st"), "isos")
    assert paths.conf == tmp_path / "st" / "services"
    assert paths.isos == tmp_path / "isos"


def test_dropped_ntp_removes_chrony_configs(tmp_path):
    conf = tmp_path / "services"
    cli.write(files_for(bare()), conf)
    doc = bare()
    del doc["spec"]["bootstrap"]["ntp"]
    changed = cli.write(files_for(doc), conf)
    assert "chrony-1.conf (removed)" in changed
    assert not (conf / "chrony-1.conf").exists()


def test_dns_answer_parsing():
    name = probe._qname("pve01.lab1.example.com")
    header = struct.pack("!6H", 1, 0x8180, 1, 2, 0, 0)
    question = name + struct.pack("!HH", 1, 1)
    a = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 300, 4) + bytes([10, 30, 110, 6])
    ptr_target = probe._qname("pve01.lab1.example.com")
    ptr = b"\xc0\x0c" + struct.pack("!HHIH", 12, 1, 300, len(ptr_target)) + ptr_target
    msg = header + question + a + ptr
    assert probe.parse_answers(msg, 1) == ["10.30.110.6"]
    assert probe.parse_answers(msg, 12) == ["pve01.lab1.example.com"]
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


def run(*args, env_extra=None):
    env = {**os.environ, "DCIAB_PVE_TOKEN": "x", **(env_extra or {})}
    return subprocess.run([str(ROOT / "dciab"), "services", *map(str, args)],
                          capture_output=True, text=True, env=env)


def test_print_shows_the_plan_and_changes_nothing(tmp_path):
    cfg = bare_site(tmp_path)
    r = run(cfg, "--print")
    assert r.returncode == 0, r.stderr
    assert "address:     10.30.110.53/24 (DNS, NTP)" in r.stdout
    assert "http://10.30.110.5:80" in r.stdout
    assert "--- " + str(tmp_path / ".dciab/state/services/coredns/Corefile") in r.stdout
    assert "6 IN PTR pve01.lab1.example.com." in r.stdout
    assert not (tmp_path / ".dciab").exists()


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
