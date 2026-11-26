# Quickstart: a first run

One run in order, on the bootstrap appliance, from bare servers to MGMT on Proxmox VE. Each step says how to check it worked before going on. Plan the site first ([PLAN.md](PLAN.md)); what is built today is in [ROADMAP.md](ROADMAP.md).

On a Rocky Linux 10 host you provide, the commands are the same: use your config's path for `CFG`, and `spec.bootstrap.workDisk.path` for `WORK`. On a site whose hypervisors already exist, skip steps 4 and 5.

## 1. Prepare the USB stick and the config

On any laptop, write the DCiaB USB image to a stick and open its `DCIAB` partition. Add `dciab.yaml` (start from [examples/proxmox-bare.yaml](../examples/proxmox-bare.yaml)), every file it references under `secrets/`, and the pinned installer ISO under `isos/`. A laptop with the archive unpacked and Python 3.12 (with `jsonschema` and `PyYAML`) can run `./dciab validate dciab.yaml` now: the offline checks need no network.

## 2. Boot server zero and log in

Boot one server on the node network from the stick, choose "install the bootstrap appliance" (it wipes `spec.bootstrap.workDisk.disk`), and boot it from its disk. At each boot it copies the config from the stick, prints `dciab validate`'s report on the console, configures `spec.bootstrap.interface` and creates a login for each `identity.initialAdmins` user. Then, from the laptop:

```console
$ ssh <you>@<spec.bootstrap.address>
```

Set the paths once. Every command below uses them, so every step shares one state folder ([STATE.md](STATE.md)):

```console
$ CFG=/var/lib/dciab/site/dciab.yaml
$ WORK=/var/lib/dciab/work
$ STATE=$WORK/state
$ ISOS=$WORK/isos
```

## 3. Check the config against the site, and copy the archive

```console
$ dciab validate $CFG --live
$ sudo dciab media $CFG --key cosign.pub
```

`--live` asks the BMCs, NTP and DNS servers whether the config fits them, and checks that the addresses DCiaB will use are free ([VALIDATE.md](VALIDATE.md)). `cosign.pub` is the release's public key, obtained separately from the archive ([RELEASING.md](RELEASING.md)). `media` copies the archive and ISOs to the work disk, verifies them, and loads the Day 0 images ([MEDIA.md](MEDIA.md)).

**Check:** `media` ends by printing the next commands. From here on, run the copy it made:

```console
$ cd $WORK/dciab-<VERSION>
```

## 4. Day 0 services (bare servers only)

```console
$ sudo ./dciab services $CFG --state $STATE --isos $ISOS
```

DNS (and NTP with `spec.time.servedBy: dciab`) on the DNS service IPs, and HTTP for the installers ([SERVICES.md](SERVICES.md)).

**Check:** the last step, `ready`, queries DNS both ways, NTP and HTTP from the network's side. `sudo ./dciab status $CFG --state $STATE` shows the pod and its DNS answers.

## 5. Hypervisors (bare servers only)

Look at the plan first: it shows each host's answer file and what it will do, without touching a BMC.

```console
$ sudo ./dciab hypervisors $CFG --state $STATE --isos $ISOS --print
$ sudo ./dciab hypervisors $CFG --state $STATE --isos $ISOS --age-identity ~/alice.agekey
```

Each host is installed through its BMC, then set up for CAPMOX through the Proxmox VE API ([HYPERVISORS.md](HYPERVISORS.md)). `--age-identity` (one recipient's identity, copied to the bootstrap host for this run only) lets post-install open the generated root passwords.

> [!WARNING]
> A host that shows any sign of an existing install is held, not wiped. `--reinstall <host>` overrides that and wipes it. Read [the reinstall guard](HYPERVISORS.md#the-reinstall-guard) before using it.

**Check:** the run ends with each host installed (`wait`) and post-installed (`post`); `$STATE/installs/<host>.json` records it. When `platform.proxmox.url` names a freshly installed host, `post` stops once with the new API certificate's fingerprint: set `platform.proxmox.tlsFingerprint` to it and rerun. Put each host's CAPMOX token where `platform.proxmox.credentials.tokenSecret` points ([HYPERVISORS.md](HYPERVISORS.md#post-install)), then run `dciab validate $CFG --live` again to check it.

## 6. Seed registry and bootstrap cluster

```console
$ sudo ./dciab seed $CFG --state $STATE
```

The platform CA, the read-only seed registry, and the kind bootstrap cluster with Cluster API ([SEED.md](SEED.md)).

**Check:** `sudo ./dciab status $CFG --state $STATE` reports the CA, the registry certificate, the seed registry and the bootstrap cluster as ok.

## 7. MGMT

```console
$ sudo ./dciab mgmt $CFG --state $STATE
```

MGMT is built, Cluster API moves into it, and MGMT manages itself ([MGMT.md](MGMT.md)). It takes the longest; each step prints `== mgmt: <step> done in Ns`.

**Check:**

```console
$ sudo ./dciab status $CFG --state $STATE        # MGMT's Cluster now lives on MGMT
$ sudo kubectl --kubeconfig $STATE/mgmt.kubeconfig get nodes
$ sudo kubectl --kubeconfig $STATE/mgmt.kubeconfig get clusters.cluster.x-k8s.io -A
```

## Afterwards

- **Keep the bootstrap host running.** MGMT's nodes still pull images from the seed registry and resolve through the Day 0 DNS until Day 1 moves those services ([MGMT.md](MGMT.md#what-mgmt-needs-from-infra-nothing)).
- **Keep the state folder safe** ([STATE.md](STATE.md)).
- `infra`, `platform` and `finalize` are Day 1 (`r1beta`) and not built yet.
- When something stops: [TROUBLESHOOTING.md](TROUBLESHOOTING.md). Every step is safe to rerun once the cause is fixed.
