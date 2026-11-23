#!/usr/bin/env python3
"""Check what CAPI's topology controller generated for each e2e cluster.

    python3 hack/e2e/verify.py charts/capi-cluster/ci/*-values.yaml

For every values file it waits for the Cluster's TopologyReconciled condition, then
checks that the class patches landed with the values from that file:
  - the control plane: Kubernetes version, replicas, kube-vip (VIP and pinned image),
    platform CA, registry mirrors, NTP, the break-glass user, OIDC flags when set,
    the API server audit policy, flags and mounts, Pod Security defaults, Secret
    encryption, kubelet serving certificates and the certificate-expiry rollout;
  - the infrastructure cluster: control plane endpoint, credentials, provider settings;
  - the control plane and worker machine templates: placement, size, addressing,
    with each worker pool's overrides;
  - the worker bootstrap template: the same node files as the control plane.

Exits 1 and lists every mismatch when anything is off.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, List, Optional

import yaml

ROOT = Path(__file__).resolve().parents[2]
LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())
WAIT_SECONDS = 360
AUDIT_POLICY = "/etc/kubernetes/audit/policy.yaml"
AUDIT_DIR = "/var/log/kubernetes/audit"
ADMISSION_CONFIG = "/etc/kubernetes/admission/config.yaml"
ENCRYPTION_CONFIG = "/etc/kubernetes/encryption/config.yaml"
KUBELET_PATCH = "/etc/kubernetes/patches/kubeletconfiguration+merge.yaml"
AUDIT_ARGS = {
    "audit-policy-file": AUDIT_POLICY,
    "audit-log-path": f"{AUDIT_DIR}/audit.log",
    "audit-log-maxage": "30",
    "audit-log-maxbackup": "10",
    "audit-log-maxsize": "100",
}


def kubectl(*args: str) -> Any:
    out = subprocess.run(["kubectl", *args, "-o", "json"], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def get_ref(ns: str, ref: dict) -> dict:
    return kubectl("-n", ns, "get", f"{ref['kind'].lower()}.{ref['apiGroup']}", ref["name"])


def condition(obj: dict, ctype: str) -> Optional[dict]:
    return next((c for c in obj.get("status", {}).get("conditions", []) if c["type"] == ctype), None)


class Verifier:
    def __init__(self, values: dict):
        self.v = values
        self.name = values["name"]
        self.ns = f"cluster-{self.name}"
        self.failures: List[str] = []

    def expect(self, what: str, actual: Any, wanted: Any) -> None:
        if actual != wanted:
            self.failures.append(f"{self.name}: {what}: got {actual!r}, want {wanted!r}")

    def expect_true(self, what: str, ok: bool) -> None:
        if not ok:
            self.failures.append(f"{self.name}: {what}")

    # -------------------------------------------------------------------------------

    def wait_for_topology(self) -> Optional[dict]:
        deadline = time.time() + WAIT_SECONDS
        last = None
        while time.time() < deadline:
            cluster = kubectl("-n", self.ns, "get", "cluster", self.name)
            last = condition(cluster, "TopologyReconciled")
            generation = cluster["metadata"].get("generation", 0)
            # Only a condition observed for the current generation counts, so a re-run
            # never verifies objects generated from an older spec.
            current = last and last.get("observedGeneration", 0) >= generation
            if last and last["status"] == "True" and current:
                return cluster
            time.sleep(10)
        self.failures.append(f"{self.name}: TopologyReconciled never became True; last: {last}")
        return None

    def run(self) -> List[str]:
        cluster = self.wait_for_topology()
        if cluster is None:
            return self.failures
        spec = cluster["spec"]
        kcp = get_ref(self.ns, spec["controlPlaneRef"])
        infra_cluster = get_ref(self.ns, spec["infrastructureRef"])
        self.check_control_plane(kcp)
        self.check_infra_cluster(infra_cluster)
        cp_machine = get_ref(self.ns, kcp["spec"]["machineTemplate"]["spec"]["infrastructureRef"])
        self.check_machine("control plane", cp_machine, self.v["controlPlane"], "controlplane")
        self.check_workers()
        return self.failures

    def check_node_files(self, where: str, config: dict) -> None:
        files = {f["path"]: f.get("content", "") for f in config.get("files", [])}
        nodes = self.v["nodes"]
        ca = files.get("/etc/pki/ca-trust/source/anchors/dciab-platform-ca.crt", "")
        self.expect_true(f"{where}: platform CA file holds the trust bundle",
                         nodes["trustBundle"].strip() in ca)
        for upstream in nodes["mirroredRegistries"]:
            hosts = files.get(f"/etc/containerd/certs.d/{upstream}/hosts.toml", "")
            self.expect_true(f"{where}: {upstream} mirrors through {nodes['registryMirror']}",
                             f'https://{nodes["registryMirror"]}/v2/{upstream}' in hosts)
        registries = files.get("/etc/containers/registries.conf.d/50-dciab-mirror.conf", "")
        try:
            entries = tomllib.loads(registries).get("registry", [])
        except tomllib.TOMLDecodeError as e:
            entries = []
            self.expect_true(f"{where}: CRI-O mirror file is valid TOML ({e})", False)
        mirrors = {r.get("prefix"): [m.get("location") for m in r.get("mirror", [])] for r in entries}
        for upstream in nodes["mirroredRegistries"]:
            self.expect(f"{where}: CRI-O mirrors {upstream}", mirrors.get(upstream),
                        [f'{nodes["registryMirror"]}/{upstream}'])
        self.expect(f"{where}: kubelet serverTLSBootstrap patch",
                    yaml.safe_load(files.get(KUBELET_PATCH) or "{}"), {"serverTLSBootstrap": True})
        self.expect(f"{where}: NTP servers", config.get("ntp", {}).get("servers"), nodes["ntpServers"])
        users = {u["name"]: u for u in config.get("users", [])}
        self.expect(f"{where}: break-glass user keys", users.get("dciab", {}).get("sshAuthorizedKeys"),
                    nodes["sshAuthorizedKeys"])
        return files

    def check_control_plane(self, kcp: dict) -> None:
        spec = kcp["spec"]
        self.expect("control plane version", spec["version"], self.v["kubernetesVersion"])
        self.expect("control plane replicas", spec["replicas"], self.v["controlPlane"]["replicas"])
        cfg = spec["kubeadmConfigSpec"]
        # Cilium replaces kube-proxy, so kubeadm must not install it.
        self.expect_true("kubeadm skips addon/kube-proxy",
                         "addon/kube-proxy" in cfg.get("initConfiguration", {}).get("skipPhases", []))
        if self.v["class"] == "proxmox":
            # Cluster API matches Machines to Nodes by spec.providerID, which the kubelet sets.
            for part in ("initConfiguration", "joinConfiguration"):
                kargs = {a["name"]: a["value"] for a in
                         cfg.get(part, {}).get("nodeRegistration", {}).get("kubeletExtraArgs", [])}
                self.expect(f"control plane {part} kubelet provider-id", kargs.get("provider-id"),
                            "proxmox://{{ ds.meta_data.instance_id }}")
        files = self.check_node_files("control plane", cfg)
        kube_vip = files.get("/etc/kubernetes/manifests/kube-vip.yaml", "")
        self.expect_true("kube-vip manifest present", bool(kube_vip))
        pod = yaml.safe_load(kube_vip) if kube_vip else {}
        container = (pod.get("spec", {}).get("containers") or [{}])[0]
        env = {e["name"]: e.get("value") for e in container.get("env", [])}
        self.expect("kube-vip image", container.get("image"), LOCK["images"]["kubeVip"])
        self.expect("kube-vip address", env.get("address"), self.v["apiVIP"])
        self.expect("kube-vip interface", env.get("vip_interface"), self.v["nodes"].get("vipInterface"))
        args = {a["name"]: a["value"] for a in
                cfg.get("clusterConfiguration", {}).get("apiServer", {}).get("extraArgs", [])}
        self.check_audit(files, cfg.get("clusterConfiguration", {}).get("apiServer", {}), args)
        self.check_hardening(cfg, files, args)
        self.expect("certificate-expiry rollout", spec.get("rollout", {}).get("before", {}).get("certificatesExpiryDays"), 60)
        oidc = self.v["nodes"].get("oidc")
        if oidc:
            self.expect("API server oidc-issuer-url", args.get("oidc-issuer-url"), oidc["issuerURL"])
            self.expect("API server oidc-client-id", args.get("oidc-client-id"), oidc["clientID"])
            self.expect("API server oidc-username-claim", args.get("oidc-username-claim"),
                        oidc.get("usernameClaim", "preferred_username"))
            self.expect("API server oidc-groups-claim", args.get("oidc-groups-claim"), oidc.get("groupsClaim", "groups"))
        else:
            self.expect_true("no OIDC flags without oidc values", "oidc-issuer-url" not in args)

    def check_hardening(self, cfg: dict, files: dict, args: dict) -> None:
        """Pod Security defaults and Secret encryption: flags, files, mounts."""
        self.expect("API server admission-control-config-file", args.get("admission-control-config-file"), ADMISSION_CONFIG)
        self.expect("API server encryption-provider-config", args.get("encryption-provider-config"), ENCRYPTION_CONFIG)
        try:
            admission = yaml.safe_load(files.get(ADMISSION_CONFIG) or "{}") or {}
        except yaml.YAMLError as e:
            admission = {}
            self.expect_true(f"admission config is valid YAML ({e})", False)
        psa = next((p.get("configuration", {}) for p in admission.get("plugins", []) if p.get("name") == "PodSecurity"), {})
        self.expect("Pod Security enforce", psa.get("defaults", {}).get("enforce"), "baseline")
        self.expect("Pod Security warn", psa.get("defaults", {}).get("warn"), "restricted")
        self.expect("Pod Security audit", psa.get("defaults", {}).get("audit"), "restricted")
        self.expect("Pod Security exempt namespaces", psa.get("exemptions", {}).get("namespaces"),
                    self.v["podSecurity"]["exemptNamespaces"])
        enc = next((f for f in cfg.get("files", []) if f.get("path") == ENCRYPTION_CONFIG), {})
        self.expect("encryption config from the cluster's Secret", enc.get("contentFrom"),
                    {"secret": {"name": f"{self.name}-encryption-config", "key": "config.yaml"}})
        mounts = {v.get("mountPath"): v for v in cfg.get("clusterConfiguration", {}).get("apiServer", {}).get("extraVolumes", [])}
        for path in (ADMISSION_CONFIG, ENCRYPTION_CONFIG):
            self.expect(f"API server mount {path}", mounts.get(path, {}).get("hostPath"), path)

    def check_audit(self, files: dict, api_server: dict, args: dict) -> None:
        """The API server gets an audit policy, writes its log to the host and rotates it."""
        for name, value in AUDIT_ARGS.items():
            self.expect(f"API server {name}", args.get(name), value)
        policy_text = files.get(AUDIT_POLICY, "")
        self.expect_true(f"audit policy file {AUDIT_POLICY} present", bool(policy_text))
        try:
            policy = yaml.safe_load(policy_text) if policy_text else {}
        except yaml.YAMLError as e:
            policy = {}
            self.expect_true(f"audit policy is valid YAML ({e})", False)
        self.expect("audit policy kind", (policy or {}).get("kind"), "Policy")
        self.expect("audit policy omitStages", (policy or {}).get("omitStages"), ["RequestReceived"])
        self.expect("audit policy default rule", ((policy or {}).get("rules") or [None])[-1], {"level": "Metadata"})
        mounts = {v.get("mountPath"): v for v in api_server.get("extraVolumes", [])}
        for path, read_only in ((AUDIT_POLICY, True), (AUDIT_DIR, False)):
            vol = mounts.get(path, {})
            self.expect(f"API server mount {path} hostPath", vol.get("hostPath"), path)
            self.expect(f"API server mount {path} readOnly", bool(vol.get("readOnly")), read_only)

    def check_infra_cluster(self, obj: dict) -> None:
        spec = obj["spec"]
        self.expect("control plane endpoint host", spec.get("controlPlaneEndpoint", {}).get("host"), self.v["apiVIP"])
        self.expect("control plane endpoint port", spec.get("controlPlaneEndpoint", {}).get("port"), 6443)
        cls = self.v["class"]
        if cls == "vsphere":
            vs = self.v["vsphere"]
            self.expect("vSphere server", spec.get("server"), vs["server"])
            self.expect("vSphere thumbprint", spec.get("thumbprint"), vs["thumbprint"])
            self.expect("vSphere identityRef", spec.get("identityRef"),
                        {"kind": "Secret", "name": f"{self.name}-vsphere-credentials"})
        elif cls == "proxmox":
            px = self.v["proxmox"]
            net = self.v["network"]
            self.expect("Proxmox allowedNodes", spec.get("allowedNodes"), px["allowedNodes"])
            self.expect("Proxmox dnsServers", spec.get("dnsServers"), self.v["nodes"]["dnsServers"])
            ipv4 = spec.get("ipv4Config", {})
            # CAPMOX defaults fields such as metric; compare the ones the class sets.
            self.expect("Proxmox ipv4Config", {k: ipv4.get(k) for k in ("addresses", "prefix", "gateway")},
                        {"addresses": [net["nodeIPRange"]], "prefix": net["prefix"], "gateway": net["gateway"]})
            self.expect("Proxmox credentialsRef name", spec.get("credentialsRef", {}).get("name"),
                        f"{self.name}-proxmox-credentials")
        elif cls == "metal3":
            self.expect("Metal3 cloudProviderEnabled", spec.get("cloudProviderEnabled"), False)

    def check_machine(self, where: str, tmpl: dict, role: dict, role_name: str) -> None:
        spec = tmpl["spec"]["template"]["spec"]
        cls = self.v["class"]
        if cls in ("vsphere", "proxmox"):
            m = role["machine"]
            if cls == "vsphere":
                vs = self.v["vsphere"]
                for field in ("server", "thumbprint", "datacenter", "datastore", "folder", "resourcePool", "template"):
                    self.expect(f"{where}: vSphere {field}", spec.get(field), vs[field])
                self.expect(f"{where}: numCPUs", spec.get("numCPUs"), m["cpu"])
                self.expect(f"{where}: memoryMiB", spec.get("memoryMiB"), m["memoryGiB"] * 1024)
                self.expect(f"{where}: diskGiB", spec.get("diskGiB"), m["diskGiB"])
                dev = (spec.get("network", {}).get("devices") or [{}])[0]
                self.expect(f"{where}: port group", dev.get("networkName"), vs["network"])
                self.expect(f"{where}: node IP pool", dev.get("addressesFromPools"),
                            [{"apiGroup": "ipam.cluster.x-k8s.io", "kind": "InClusterIPPool", "name": f"{self.name}-nodes"}])
                self.expect(f"{where}: nameservers", dev.get("nameservers"), self.v["nodes"]["dnsServers"])
            else:
                px = self.v["proxmox"]
                self.expect(f"{where}: sourceNode", spec.get("sourceNode"), px["templateNode"])
                self.expect(f"{where}: templateID", spec.get("templateID"), px["templateID"])
                self.expect(f"{where}: storage", spec.get("storage"), px["storage"])
                self.expect(f"{where}: numCores", spec.get("numCores"), m["cpu"])
                self.expect(f"{where}: memoryMiB", spec.get("memoryMiB"), m["memoryGiB"] * 1024)
                self.expect(f"{where}: boot disk", spec.get("disks", {}).get("bootVolume", {}).get("sizeGb"), m["diskGiB"])
                dev = (spec.get("network", {}).get("networkDevices") or [{}])[0]
                self.expect(f"{where}: bridge", dev.get("bridge"), px["bridge"])
                self.expect(f"{where}: VLAN", dev.get("vlan"), px.get("vlanTag"))
                self.expect(f"{where}: provider ID injection", spec.get("metadataSettings", {}).get("providerIDInjection"), True)
        else:
            img = self.v["metal3"]["image"]
            self.expect(f"{where}: image", spec.get("image"), img)
            self.expect(f"{where}: hostSelector", spec.get("hostSelector", {}).get("matchLabels"), role["hostSelector"])
            data_template = spec.get("dataTemplate", {}).get("name")
            self.expect(f"{where}: dataTemplate", data_template, f"{self.name}-{role_name}")
            if data_template:
                self.check_metal3_addressing(where, data_template, role["network"])

    def check_metal3_addressing(self, where: str, name: str, network: dict) -> None:
        """The data template's IPPool hands out the role's range, prefix, gateway and DNS."""
        try:
            dt = kubectl("-n", self.ns, "get", "metal3datatemplate", name)
            pool = kubectl("-n", self.ns, "get", "ippools.ipam.metal3.io", name)
        except subprocess.CalledProcessError as exc:
            self.failures.append(f"{self.name}: {where}: {exc.stderr.strip() or exc}")
            return
        nd = dt["spec"].get("networkData", {})
        ipv4 = (nd.get("networks", {}).get("ipv4") or [{}])[0]
        self.expect(f"{where}: data template address pool", ipv4.get("fromPoolRef", {}).get("name"), name)
        route = (ipv4.get("routes") or [{}])[0]
        self.expect(f"{where}: default route gateway pool", route.get("gateway", {}).get("fromPoolRef", {}).get("name"), name)
        self.expect(f"{where}: data template DNS", nd.get("services", {}).get("dns"), self.v["nodes"]["dnsServers"])
        first, last = network["nodeIPRange"].split("-")
        spec = pool["spec"]
        self.expect(f"{where}: IPPool range", [(p.get("start"), p.get("end")) for p in spec.get("pools", [])], [(first, last)])
        self.expect(f"{where}: IPPool prefix", spec.get("prefix"), network["prefix"])
        self.expect(f"{where}: IPPool gateway", spec.get("gateway"), network["gateway"])
        self.expect(f"{where}: IPPool DNS", spec.get("dnsServers"), self.v["nodes"]["dnsServers"])

    def check_workers(self) -> None:
        mds = kubectl("-n", self.ns, "get", "machinedeployments",
                      "-l", f"cluster.x-k8s.io/cluster-name={self.name}")["items"]
        by_pool = {md["metadata"].get("labels", {}).get("topology.cluster.x-k8s.io/deployment-name"): md for md in mds}
        for pool in self.v["workerPools"]:
            md = by_pool.get(pool["name"])
            if md is None:
                self.failures.append(f"{self.name}: no MachineDeployment for pool {pool['name']} "
                                     f"(found {sorted(k for k in by_pool if k)})")
                continue
            where = f"pool {pool['name']}"
            self.expect(f"{where}: replicas", md["spec"].get("replicas"), pool["replicas"])
            tmpl = md["spec"]["template"]["spec"]
            self.check_machine(where, get_ref(self.ns, tmpl["infrastructureRef"]), pool, pool["name"])
            bootstrap = get_ref(self.ns, tmpl["bootstrap"]["configRef"])
            self.check_node_files(where, bootstrap["spec"]["template"]["spec"])


def main(paths: List[str]) -> int:
    failures: List[str] = []
    for p in paths:
        values = yaml.safe_load(Path(p).read_text())
        found = Verifier(values).run()
        status = "ok" if not found else f"{len(found)} problem(s)"
        print(f"{values['name']} ({values['class']}): {status}")
        failures += found
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
