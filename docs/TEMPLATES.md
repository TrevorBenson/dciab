# `dciab templates`

Publishes the Proxmox VE node template from the archive's node disk. The Proxmox ClusterClass clones every node of MGMT, INFRA and the workload clusters from the template `spec.platform.proxmox.templateID` on `templateNode`. `dciab mgmt` needs it there, and `dciab validate --live` reports it missing until then.

It runs as root on the bootstrap host, after `dciab hypervisors` on a site that starts from bare servers, and before `dciab seed`. It talks only to the Proxmox VE API, never over SSH.

```console
$ sudo ./dciab templates dciab.yaml --state <state> --print                  # the plan; changes nothing
$ sudo ./dciab templates dciab.yaml --state <state> --age-identity ~/alice.agekey
```

Options: `--state DIR` (the same folder the other steps use, [STATE.md](STATE.md)), `--payload DIR` (the archive's payload; default `DCIAB_PAYLOAD`, else the archive's own `payload/`), `--age-identity FILE` (opens `templateNode`'s generated root password, below), `--replace` (below), `--timeout SECONDS` (for the upload and each Proxmox VE task; default 3600), `--print`.

## What it does

1. **Checks the disk.** The payload's `nodes/nodes.yaml` lists the Proxmox VE disk (`nodes/proxmox/disk.qcow2`) with its sha256, as the archive build pulled and checked it. The disk's sha256 must match, or the step stops: copy the archive again (`dciab media`) and check it (`dciab verify-archive`).
2. **Looks at `templateID`.** A template there whose description holds this disk's sha256 is kept, and the step ends, once it passes the checks of step 5. One that fails them is left as it is and the step stops with what is wrong (`--replace` publishes it again). A VM that DCiaB made from this disk, with its `scsi0`, but that isn't a template yet (an earlier run stopped, or its conversion failed) is converted and checked: the step is finished. Such a VM with no disk that isn't running (an import that failed part-way) is DCiaB's own, so it is deleted and made again, without `--replace`; the delete waits for the checks before it and for step 3, as with `--replace`. Anything else at that ID is left alone, and the step stops; see [replacing a template](#replacing-a-template).
3. **Checks the storages and uploads the disk** to the storage `local` as the content type `import`, with its sha256 for Proxmox VE to check. `local` gains that content type when it lacks it. `spec.platform.proxmox.storage` must exist on `templateNode`, be enabled and active there, and hold VM disks; `local` must be enabled and active too.
4. **Creates the VM** `templateID` on `templateNode`, named `dciab-node-<the disk's sha256, first 12>`, its `scsi0` imported from the upload onto `spec.platform.proxmox.storage` (`import-from`), and converts it to a template. The upload is deleted afterwards, whether or not this worked.
5. **Checks the template:** a template, with `scsi0`, with the disk's sha256 in its description, and no cloud-init drive.

The template's settings are what the node image is built and tested for: SeaBIOS, a `virtio-scsi-single` controller with the disk as `scsi0` (`discard=on`, `ssd=1`), the boot order `scsi0`, CPU type `x86-64-v3`, 2 cores and 4 GiB (CAPMOX sets each clone's own), the QEMU guest agent on, a serial console, and one `virtio` NIC on `spec.platform.proxmox.bridge` (tagged `vlanTag` when set). It has **no cloud-init drive**: CAPMOX attaches its own, and a second one would win over it. The disk stays as thin as the image (about 2 GiB of root); each clone grows to its `diskGiB` at deploy time.

## Credentials

Never CAPMOX's token: that token has exactly the privileges CAPMOX uses, and importing templates isn't one ([PROXMOX.md](PROXMOX.md)).

| The site | How it logs in |
| --- | --- |
| `templateNode` is a host `dciab hypervisors` installed (its install record says so) | As `root@pam`, on that host's address, with the site's `spec.hypervisors.rootPassword` or its generated one, opened with `--age-identity`, as `post` does ([HYPERVISORS.md](HYPERVISORS.md#post-install)). Nothing to configure |
| A Proxmox VE that already ran, or any site that sets `spec.platform.proxmox.templateImport` | With the API token `templateImport.credentials` names, through `platform.proxmox.url`. It has exactly the privileges in [PROXMOX.md](PROXMOX.md#the-node-templates-token): one VM ID, the template's storage, `local` for the upload, the bridge. `dciab validate --live` checks them |
| A Proxmox VE that already ran, without `templateImport` | The step stops. Give the token, or publish the template [by hand](#by-hand) |

DCiaB never logs in as root to a host it didn't install. The API certificate is trusted by `spec.platform.proxmox.tlsFingerprint`, or by the pin in the host's install record ([trust on first use](HYPERVISORS.md#trust-on-first-use)).

## Replacing a template

A new archive with a new node disk doesn't change a template that is already there: the step stops at a template made from another disk, as it does at any VM it didn't make. Either:

- set `templateID` to a free ID and run the step again (the old template stays until nothing clones from it), or
- run it with `--replace`, which deletes what is at `templateID` and publishes there. It never deletes a running VM, nor anything on another node than `templateNode`. Clusters already running are untouched: CAPMOX makes full clones, which don't depend on the template.

With `--replace`, nothing is deleted until everything that can be checked first has passed, and a failed check leaves the old template as it was:

- the login can create a VM at `templateID` after the delete. Proxmox VE removes a VM's own ACLs when it destroys it, so a [template token](PROXMOX.md#the-node-templates-token) with its VM privileges on `/vms/<templateID>` only can't. The step then stops and says so. Delete the old template yourself (web interface or API), run the token's `pveum acl modify /vms/<templateID>` line again, and run the step without `--replace`; or set `templateID` to a free ID with that ACL; or grant the token the `DCiaBTemplateVM` role on `/vms`, propagated, for this replacement only. As `root@pam` (a host DCiaB installed) the privileges are always there;
- the storages exist, are enabled and active, and the new disk is uploaded and checked (step 3).

The old one is deleted right before the new VM is created. If creating the new VM fails then, `templateID` is left empty until the step is run again (no new clone can start meanwhile). If it was created but converting it to a template failed, that VM (with its `scsi0`) stays at `templateID`, and the next run converts it and checks it (step 2). The error says which task failed.

## By hand

A site can publish the template itself instead, through the Proxmox VE web interface or API, from the archive's `payload/nodes/proxmox/disk.qcow2` (check it against `nodes.yaml` first): import it as `scsi0` of a VM with the settings above and no cloud-init drive, at `templateID` on `templateNode`, and convert it to a template. `dciab validate --live` checks that `templateID` is a template on `templateNode`.

## Not done yet

- vSphere and Metal3 node templates, with their lanes ([ROADMAP.md](ROADMAP.md)).
- One template for several unclustered hosts: CAPMOX clones from `templateNode` only, so other hosts need clustered, shared storage first.
