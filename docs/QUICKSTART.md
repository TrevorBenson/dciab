# Quickstart: a first run

One run in order, on the bootstrap appliance, from bare servers to MGMT on Proxmox VE. Each step says how to check it worked before going on. Plan the site first ([PLAN.md](PLAN.md)); what is built today is in [ROADMAP.md](ROADMAP.md).

- **A site whose Proxmox VE already runs** skips steps 4 and 5: its API token and fingerprint exist from the start, and the config gives them. It needs step 6 only if the node template isn't on `templateNode` yet, and then a token for it in the config (`platform.proxmox.templateImport`, [TEMPLATES.md](TEMPLATES.md#credentials)).
- **A Rocky Linux 10 host you provide** skips step 2's appliance install: unpack the archive there, use its `./dciab` wherever this page says `dciab`, and set the paths below for that host. Its config isn't copied from a stick, so edit the config and its secrets where they are, wherever this page says to edit the stick's copy.

## 1. Prepare the USB stick and the config

On any laptop, write the DCiaB USB image to a stick and open its `DCIAB` partition. Add:

- `dciab.yaml`, starting from [examples/proxmox-bare.yaml](../examples/proxmox-bare.yaml);
- every file it references, under `secrets/` (the appliance copies only `dciab.yaml` and `secrets/`), except the CAPMOX token's;
- the pinned Proxmox VE installer ISO, under `isos/`.

Two values of `spec.platform.proxmox` can't exist yet on a bare site, and the config needs neither: the CAPMOX API token and the API certificate's fingerprint. Keep `platform.proxmox.url` naming the host DCiaB installs, `credentials.tokenSecret: {file: ./secrets/pve/token}` with no such file, and no `tlsFingerprint`, as the example does. `dciab hypervisors` creates the token and writes it there on the bootstrap host, never on the stick, and pins the certificate it sees on first contact (step 5). Until then every check reports them as pending, not as errors.

A laptop with the archive unpacked and Python 3.12 or later (with `jsonschema` and `PyYAML`) can run `./dciab validate dciab.yaml` now: the offline checks need no network.

## 2. Boot server zero and log in

Boot one server on the node network from the stick. With Secure Boot on, the first boot on each machine asks you to enroll Ventoy's key in MokManager at the console. Choose "install the bootstrap appliance" (it wipes `spec.bootstrap.workDisk.disk`), and boot the server from its disk.

At each boot the appliance copies `dciab.yaml` and `secrets/` from the stick to `/var/lib/dciab/site` (root-only), prints `dciab validate`'s report on the console, configures `spec.bootstrap.interface`, and creates a login for each `identity.initialAdmins` user. Then, from the laptop, copy the release's public key over and log in:

```console
$ scp cosign.pub <you>@<spec.bootstrap.address>:
$ ssh <you>@<spec.bootstrap.address>
```

Set the paths once. Every command below uses them, so every step shares one state folder ([STATE.md](STATE.md)):

```console
$ CFG=/var/lib/dciab/site/dciab.yaml     # a host you provide: your config's path
$ WORK=/var/lib/dciab/work               # a host you provide: spec.bootstrap.workDisk.path
$ STATE=$WORK/state
$ ISOS=$WORK/isos
```

> [!NOTE]
> **Changing the config on the appliance.** Edit `dciab.yaml` and `secrets/` on the stick, which the appliance mounts root-only at `/var/mnt/dciab` (`sudoedit /var/mnt/dciab/dciab.yaml`), then run `sudo systemctl restart dciab-bootstrap-setup.service` (or reboot) to copy them in. An edit made only in `/var/lib/dciab/site` is replaced at the next boot or restart of that service with the stick plugged in; without the stick, the copy is kept. Secrets DCiaB generates on the appliance, such as the CAPMOX token, live in `/var/lib/dciab/site-local/secrets/` and survive every copy ([STATE.md](STATE.md#outside-the-state-folder)). Before taking the stick out after an edit, `sudo umount /var/mnt/dciab`.

## 3. Check the config against the site, and copy the archive

```console
$ sudo dciab validate $CFG --live
$ sudo dciab media $CFG --key ~/cosign.pub
```

`--live` asks the BMCs, NTP and DNS servers whether the config fits them, and checks that the addresses DCiaB will use are free ([VALIDATE.md](VALIDATE.md)). On a bare site it reports the Proxmox API checks as pending until step 5, because nothing is installed yet; everything else must pass. `cosign.pub` is the release's public key, obtained separately from the archive ([RELEASING.md](RELEASING.md)). `media` copies the archive and the ISOs to the work disk, verifies them, and loads the Day 0 images ([MEDIA.md](MEDIA.md)).

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

Each host is installed through its BMC, then set up for CAPMOX through the Proxmox VE API ([HYPERVISORS.md](HYPERVISORS.md)). `--age-identity` (one recipient's identity, copied to the bootstrap host for steps 5 and 6 only) lets post-install open the generated root passwords.

> [!WARNING]
> A host that shows any sign of an existing install is held, not wiped. `--reinstall <host>` overrides that and wipes it. Read [the reinstall guard](HYPERVISORS.md#the-reinstall-guard) before using it.

**Check:** each host ends installed (`wait`) and post-installed (`post`); `$STATE/installs/<host>.json` records it, with the API certificate `post` pinned. For the host `platform.proxmox.url` names, `post` wrote the CAPMOX token where `tokenSecret` points: on the appliance, into the site copy and the appliance-local secrets that every boot keeps ([the token's home](HYPERVISORS.md#the-capmox-tokens-home)). There is nothing to copy. Run `sudo dciab validate $CFG --live` again: the Proxmox API checks now pass, except the node template's.

## 6. The node template

`dciab mgmt` clones its nodes from the template `templateID` on `templateNode`. A freshly installed host has none, and an existing Proxmox VE may not have it yet. Publish it from the archive's node disk, through the Proxmox VE API ([TEMPLATES.md](TEMPLATES.md)):

```console
$ sudo ./dciab templates $CFG --state $STATE --print
$ sudo ./dciab templates $CFG --state $STATE --age-identity ~/alice.agekey
```

On a host step 5 installed, it logs in as root with the host's root password (`--age-identity` opens a generated one); an existing Proxmox VE needs `platform.proxmox.templateImport` instead. A template already there from the same disk is kept, and anything else at `templateID` is left alone.

**Check:** the step ends with `template <templateID> on <templateNode>: published` (or `kept`), and `sudo ./dciab validate $CFG --live` passes with no Proxmox errors. Then delete the identity you copied over, with `rm ~/alice.agekey`: anyone who can read it can open everything sealed to that recipient. Copy it over again for a later step that needs it, and keep the identities off the bootstrap host otherwise ([PLAN.md](PLAN.md#4-recipients-and-their-keys)).

## 7. Seed registry and bootstrap cluster

```console
$ sudo ./dciab seed $CFG --state $STATE
```

The platform CA, the read-only seed registry, and the kind bootstrap cluster with Cluster API ([SEED.md](SEED.md)).

**Check:** `sudo ./dciab status $CFG --state $STATE` reports the CA, the registry certificate, the seed registry and the bootstrap cluster as ok.

## 8. MGMT

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
