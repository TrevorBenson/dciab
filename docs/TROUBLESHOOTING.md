# Troubleshooting

What to do when a step stops, by symptom. Every step is safe to rerun once the cause is fixed, and each error message says what it found and what to do; this page links to the full explanation. Run `sudo ./dciab status <config> --state <state>` to see where the bootstrap stands.

## Any step

| Symptom | Cause | Fix |
| --- | --- | --- |
| A step refuses the config | `dciab validate` finds errors | Run `sudo ./dciab validate <config>`; each error names the field, what it found and the fix ([VALIDATE.md](VALIDATE.md)) |
| A secret given as `{env: VAR}` is missing under `sudo` | `sudo` drops environment variables | `sudo --preserve-env=VAR …`, or use a `{file: …}` reference ([PLAN.md](PLAN.md#5-secrets)) |
| On a bare site, every step refuses the config before the hypervisors exist | The CAPMOX token or the API fingerprint can't exist until `dciab hypervisors` runs | Give placeholders until then ([QUICKSTART.md](QUICKSTART.md#1-prepare-the-usb-stick-and-the-config)) |
| A config edit on the appliance was lost | `/var/lib/dciab/site` is replaced from the stick at each boot | Edit the stick's copy ([QUICKSTART.md](QUICKSTART.md#2-boot-server-zero-and-log-in)) |
| A step doesn't see what an earlier one did (no kubeconfig, no CA, "run seed first") | The steps ran with different state folders | Pass the same `--state` to every step; after `media`, `<work>/state` ([STATE.md](STATE.md)) |
| `dciab status` fails with a permission error, or reports checks as missing or unknown | It ran without root, so it can't read the root-only state folder or see root's containers | `sudo ./dciab status …` |
| Nodes can't pull images or resolve names; `kubeadm init` stalls | `network.upstreamResolvers` is empty, so nodes resolve only `network.domain`, and the image isn't in the seed registry | Add resolvers, or keep the site isolated and make sure the archive's payload holds every image ([SERVICES.md](SERVICES.md)) |

## `dciab validate --live`

| Symptom | Cause | Fix |
| --- | --- | --- |
| An address "answers on the node network" | Something already uses the bootstrap address, a DNS service IP, an API VIP or a node range | Pick free addresses, or free them. Run `--live` before `seed`: once clusters exist, their own addresses answer ([VALIDATE.md](VALIDATE.md#live-checks)) |
| A TLS fingerprint doesn't match | The certificate changed (a reinstalled host, a new BMC certificate) or the config is wrong | Check the certificate out of band, then update `tlsFingerprint` / `thumbprint` |
| The Proxmox token lacks privileges | Its roles or ACLs differ from [PROXMOX.md](PROXMOX.md) | Recreate them as listed there |

## `dciab media`

| Symptom | Cause | Fix |
| --- | --- | --- |
| Not enough free space | The work disk can't hold the copy, the state and the Day 0 images | Free space or use a bigger disk ([MEDIA.md](MEDIA.md#where-it-copies-to)) |
| It stops while containers run | Moving container storage or state under running containers isn't safe | Stop them and rerun |
| Both folders hold state | State exists next to the config and on the work disk | Keep the right one, move the other aside, rerun; the message says which looks partial ([MEDIA.md](MEDIA.md#what-it-does)) |
| `verify` finds extra files | An earlier build of the same version left files in the copy | Remove the old copy and rerun |

## `dciab services`

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ready` fails on DNS or NTP | Another server already binds those ports on every address (a local resolver, libvirt's dnsmasq, a chronyd that serves NTP), or firewalld blocks them | Stop or rebind the other server; check the firewalld zone of `spec.bootstrap.interface` ([SERVICES.md](SERVICES.md)) |
| DNS stopped answering while the host runs | NetworkManager reapplied the interface and dropped the DNS service IPs | `sudo systemctl restart dciab-services.service` ([SERVICES.md](SERVICES.md#after-a-reboot)) |
| `--down` fails on a change it can't confirm | A podman or firewalld error, or a unit that won't stop | Fix the error and rerun `--down`; for a change you know is gone, `--down --forget '<line>'` ([SERVICES.md](SERVICES.md#what-it-adds-to-the-host-and-taking-it-away)) |

## `dciab hypervisors`

| Symptom | Cause | Fix |
| --- | --- | --- |
| A host is held | Something suggests it already holds an install (its API, SSH, an install record, a boot entry on a disk, legacy boot mode) | Check the host. Only if it should be wiped: `--reinstall <host>`. A new server with no boot options at all: `--first-install <host>` ([the reinstall guard](HYPERVISORS.md#the-reinstall-guard)) |
| The install is refused | `rootDeviceHints` match no drive the BMC lists | Fix the serial number or WWN ([Redfish preparation](HYPERVISORS.md#redfish-preparation)) |
| The CD slot never shows the ISO | The BMC can't fetch it: no route back to the bootstrap host, HTTP blocked, or the ISO isn't served | Check the route and firewall from the BMC network to `spec.bootstrap.address:80`, and that `dciab services` runs ([the attach check](HYPERVISORS.md#the-attach-check)) |
| `wait` times out on a tagged port | The first-boot script that moves the address onto the VLAN failed | On the host's console: `journalctl -t dciab-first-boot`, and check the switch port carries the node VLAN tagged ([the management VLAN](HYPERVISORS.md#the-management-vlan)) |
| `post` stops with a certificate fingerprint | `platform.proxmox.url` names this host, and its fresh install has a new certificate | Set `platform.proxmox.tlsFingerprint` to the fingerprint it prints, and rerun ([post-install](HYPERVISORS.md#post-install)) |
| `post` can't open the root password | No identity to open the sealed password | Pass `--age-identity` with one recipient's identity |
| The media wasn't ejected after the install | The BMC refused or didn't confirm the eject | Eject it through the BMC; the next run checks again |

## `dciab seed`

| Symptom | Cause | Fix |
| --- | --- | --- |
| It won't create a new root CA | The state shows a CA was issued before, but the CA is incomplete | Restore the state from a backup, or pass `--force-new-ca` knowing that nothing trusting the old CA will trust the new one ([SEED.md](SEED.md#the-platform-ca)) |
| It can't reissue the registry certificate | The CA key is sealed | Pass `--age-identity` with one recipient's identity |
| The bootstrap cluster is down after a reboot | kind doesn't restart its node containers under podman | Rerun `dciab seed`, which tries to start it. If that fails, what to do depends on whether `dciab mgmt` has started: don't delete it once MGMT's objects are in it ([SEED.md](SEED.md#after-a-reboot)) |

## `dciab mgmt`

| Symptom | Cause | Fix |
| --- | --- | --- |
| `move` doesn't start | The export to the backup site target failed (credentials, endpoint, or an NFS export the squashed root user can't write) | Fix the target ([BACKUP.md](BACKUP.md)) and rerun; nothing was moved |
| It stops because a Cluster is on both clusters, or paused | A move stopped part-way, or an earlier run left a copy | Follow the table for your case; don't unpause or delete either copy by guesswork ([MGMT.md](MGMT.md#a-cluster-on-both-clusters)) |
| Nodes never become Ready | Images or names unreachable, or (vSphere) the cloud provider hasn't initialized them | Check the seed registry and DNS from a node; on vSphere, the `cpi` step's output |
