# Quickstart: a first run

One run in order, on the bootstrap appliance, from bare servers to MGMT on Proxmox VE. Each step says how to check it worked before going on. Plan the site first ([PLAN.md](PLAN.md)); what is built today is in [ROADMAP.md](ROADMAP.md).

- **A site whose Proxmox VE already runs** skips steps 4 to 6: it has its API token, fingerprint and node template from the start.
- **A Rocky Linux 10 host you provide** skips step 2: unpack the archive, run `./dciab` from it instead of `dciab`, and set `CFG` to your config and `WORK` to `spec.bootstrap.workDisk.path`. Edit the config where it is. The rest is the same.

## 1. Prepare the USB stick and the config

On any laptop, write the DCiaB USB image to a stick and open its `DCIAB` partition. Add:

- `dciab.yaml`, starting from [examples/proxmox-bare.yaml](../examples/proxmox-bare.yaml);
- every file it references, under `secrets/` (the appliance copies only `dciab.yaml` and `secrets/`);
- the pinned Proxmox VE installer ISO, under `isos/`.

Two values of `spec.platform.proxmox` can't exist yet on a bare site: the CAPMOX API token, which `dciab hypervisors` creates in step 5, and the API certificate's fingerprint, which the installed host makes. Every step checks the config, so give placeholders until step 5:

- `credentials.tokenSecret: {file: ./secrets/pve/token}`, with a file that holds any text. Use a file, not `{env: …}`: neither the appliance's boot-time check nor a `sudo` step sees your environment.
- `tlsFingerprint`: any well-formed SHA-256 fingerprint.

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
$ CFG=/var/lib/dciab/site/dciab.yaml
$ WORK=/var/lib/dciab/work
$ STATE=$WORK/state
$ ISOS=$WORK/isos
```

> [!NOTE]
> **Changing the config on the appliance.** Edit `dciab.yaml` and `secrets/` on the stick, which the appliance mounts at `/var/mnt/dciab`, then run `sudo systemctl restart dciab-bootstrap-setup.service` (or reboot) to copy them in. An edit made only in `/var/lib/dciab/site` is replaced at the next boot with the stick plugged in.

## 3. Check the config against the site, and copy the archive

```console
$ sudo dciab validate $CFG --live
$ sudo dciab media $CFG --key ~/cosign.pub
```

`--live` asks the BMCs, NTP and DNS servers whether the config fits them, and checks that the addresses DCiaB will use are free ([VALIDATE.md](VALIDATE.md)). On a bare site its Proxmox API checks fail until step 5, because nothing is installed yet; everything else should pass. `cosign.pub` is the release's public key, obtained separately from the archive ([RELEASING.md](RELEASING.md)). `media` copies the archive and the ISOs to the work disk, verifies them, and loads the Day 0 images ([MEDIA.md](MEDIA.md)).

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

**Check:** each host ends installed (`wait`) and post-installed (`post`); `$STATE/installs/<host>.json` records it. Then replace the placeholders, on the stick ([see the note](#2-boot-server-zero-and-log-in)), for the host `platform.proxmox.url` names:

1. `post` stops once for that host, printing its new API certificate's fingerprint. Set `tlsFingerprint` to it, copy the config in, and rerun this step.
2. Open that host's token into the token file, with the `age` from the archive's payload:
   ```console
   $ sudo ./payload/bin/age -d -i ~/alice.agekey $STATE/proxmox/<host>.token.age | sudo tee /var/mnt/dciab/secrets/pve/token >/dev/null
   $ sudo systemctl restart dciab-bootstrap-setup.service
   ```
3. Run `sudo dciab validate $CFG --live` again: the Proxmox API checks now pass, except the node template's.

## 6. The node template (bare servers only)

`dciab mgmt` clones its nodes from the template `templateID` on `templateNode`, and a freshly installed host has none. DCiaB doesn't publish the template onto a host yet (node images in the archive, [ROADMAP.md](ROADMAP.md)): build it from the DCiaB node image recipe and import it at that ID on that node, without a Proxmox cloud-init drive (CAPMOX attaches its own).

**Check:** `sudo dciab validate $CFG --live` passes with no Proxmox errors.

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
