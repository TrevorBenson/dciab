# `dciab hypervisors`

Installs Proxmox VE on the servers in `spec.hypervisors.hosts` through their BMCs, as decided in [ADR 0001](adr/0001-day0-hypervisors-and-services.md). It runs as root on the bootstrap host after `dciab services`, whose Caddy serves each host's installer ISO to that host's BMC.

```console
$ sudo ./dciab hypervisors dciab.yaml --print          # the plan and answer files; changes nothing
$ sudo ./dciab hypervisors dciab.yaml                  # every host
$ sudo ./dciab hypervisors dciab.yaml --host pve02 --until media
```

Options: `--state DIR` (default `.dciab/state` next to the config, or `/var/lib/dciab/work/state` on the bootstrap appliance; the same folder `dciab services` uses), `--host NAME` (repeatable), `--reinstall NAME` (repeatable: wipe and reinstall that host even if it's installed), `--first-install NAME` (repeatable: install that host when nothing shows whether it holds an install; see [the reinstall guard](#the-reinstall-guard)), `--until STEP`, `--iso FILE` (the base installer ISO's file name in the ISOs folder; default the file name of the Proxmox VE ISO in `bundle.lock`), `--isos DIR` (the ISOs folder; default as for `dciab services`), `--timeout SECONDS` (per host, for `wait`; default 3600).

## Steps

Each step runs for every selected host and is safe to re-run.

| Step | What it does |
| --- | --- |
| `answer` | Renders the host's `answer.toml` into `<state>/answers/<host>.toml` (folder 700, file 600, written atomically). For a host no longer in the config, its answer file and root password files (`.root-hash`, `.root-salt`, `.root-password.age`) are removed. If it has an install record, its password files and the record are kept and the run says so: the server may still use that password. Delete the password files by hand once the server is wiped or retired |
| `iso` | Builds `<state>/host-isos/<host>.iso` (folder 700, file 600): the Proxmox VE installer ISO with that host's answer file inside (`proxmox-auto-install-assistant prepare-iso --fetch-from iso`, run with no network from the image built from `images/pve-assistant`). The base ISO is checked against `bundle.lock` first. An ISO is rebuilt only when its answer file or the base ISO changed. Caddy serves it at `http://<bootstrap.address>/host-isos/<host>.iso` to that host's BMC address only |
| `media` | Skipped when the host's Proxmox VE API already answers, and held when anything shows an existing install ([the reinstall guard](#the-reinstall-guard)). Otherwise, over Redfish: finds the system's CD/DVD virtual media slot (under the System or its Manager), ejects other media, attaches the host's ISO, sets a one-time UEFI boot from `Cd`, and resets the server (`ForceRestart` when on, `On` when off; a server in a transitional power state is left alone). Redfish tasks a BMC accepts with HTTP 202 are waited for. The installer boots its "Automated Installation" entry and reads the answer file from the ISO: no DHCP and no answer server |
| `wait` | Waits for SSH (22) and then the Proxmox VE API on the host's address (an answer from `/api2/json/version` on 8006, not just an open port), and records the install as done. Then, for a host whose ISO DCiaB attached, ejects that ISO and clears the boot override DCiaB set if it still points at `Cd`, so a fallback to CD never boots the installer again. Other media and overrides are left alone. The result goes in the install record (`media`: `detached`, `detach-failed`, or `detach-unverified` when other media is in the slot). Only `detached` is final: otherwise the run says how to eject by hand, and the next run checks again. This never fails the run. Each new install (`media`, `--reinstall` too) starts the record's `media` over at `attached` |

BMC trust comes from each host's `bmc.tlsFingerprint` or `bmc.caBundle`, as for `dciab validate --live`; the certificate is checked before credentials are sent.

## The answer file

| Answer file key | From |
| --- | --- |
| `global.keyboard`, `global.country`, `global.timezone` | `spec.hypervisors.installer` |
| `global.fqdn` | host `name` + `network.domain` |
| `global.mailto` | the first `identity.initialAdmins` email |
| `global.root-password-hashed` | `spec.hypervisors.rootPassword` when set (one for every host), else a random 256-bit password per host, encrypted at once to `spec.bootstrap.recipients` (`<state>/answers/<host>.root-password.age`; open it with `age -d -i <identity>`) and never written in plain text. Only its SHA-512 crypt hash is kept (`<state>/answers/<host>.root-hash`, mode 600). Either way the hash uses a random salt made once per host and kept in `<state>/answers/<host>.root-salt` (mode 600), so reruns write the same file |
| `global.root-ssh-keys` | every `identity.initialAdmins` SSH key |
| `network` | `source = "from-answer"`: the host `address` with the node network's prefix, `network.nodes.gateway`, the first DNS service IP, and the NIC matched by `bootMACAddress` |
| `disk-setup` | `rootDeviceHints`: `deviceName` becomes `disk-list`; `serialNumber`, `wwn`, `model` and `vendor` become udev filters (`ID_SERIAL_SHORT`, `ID_WWN`, `ID_MODEL`, `ID_VENDOR`; spaces become underscores, as udev writes them) that must all match. `hctl`, `minSizeGigabytes` and `rotational` can't be expressed and are refused |

Fixed value: `filesystem = "ext4"`, which gives the `local` and `local-lvm` storages CAPMOX expects from a default install.

## Access

- **People:** root over SSH with the `identity.initialAdmins` keys. The root password is for joining the host to a cluster or manager (a Proxmox VE cluster, vCenter, Proxmox Datacenter Manager), for post-install setup through the API, and for break-glass console access; SSO through Authentik as a Proxmox VE realm comes later ([ADR 0004](adr/0004-identity-and-sso.md)).
- **DCiaB and CAPMOX** at run time: the Proxmox VE API with a dedicated API token, never SSH. [PROXMOX.md](PROXMOX.md) lists its exact privileges.
- **Existing hypervisors** (a site that already runs Proxmox VE or vSphere): DCiaB never touches root; it uses only the API token or vCenter account in the config.

## The reinstall guard

`media` resets a server only when nothing suggests it already holds an install. It holds a host, does nothing to it, and exits 1 when:

| Evidence | To install anyway |
| --- | --- |
| The Proxmox VE API answers on the host's address: `GET /api2/json/version` over TLS returns Proxmox VE's version answer (JSON with `data.version`), or the API daemon's own 401 (`Server: pve-api-daemon`). Another service on port 8006 doesn't count, even one that sends the daemon's `Server` header with some other body | `--reinstall <host>` |
| An install is recorded in `<state>/installs/<host>.json` (any file there, even an unreadable one), powered off or not. A recorded install still in progress, powered on with its ISO attached, isn't reset again: the run goes on to `wait` | `--reinstall <host>` |
| No record, but SSH (22) answers | `--reinstall <host>` |
| No record, and port 8006 is open but doesn't answer as the Proxmox VE API (a timeout, a 403 from `pveproxy`'s access list, a server error, another service): something runs there | `--reinstall <host>` |
| No record, but the BMC lists a boot option that may load an installed OS: Proxmox VE (`\EFI\proxmox`), a known OS name, the generic "UEFI OS" entry firmware creates after an NVRAM reset or a board swap, a legacy BIOS disk entry ("Hard drive C:", `BBS(HD,...)`), or any entry whose device path points into a local disk: a partition (`HD(...)`), or a whole disk on a SATA, SCSI, SAS or NVMe bus with no partition node. OS evidence wins over other words in the name: "UEFI OS (Virtual Disk)" counts, and so does a USB-named entry on a partition, such as a bootable stick left in the server. Only network entries (PXE, HTTP boot) never count; CD/DVD, shell and setup entries count only with such evidence | `--reinstall <host>` |
| No record, and the BMC's boot options can't be read in full | `--reinstall <host>` |
| No record, and the BMC lists no boot options at all (no boot options collection, or an empty one): a new server and one whose state folder was lost (or a different `--state`) look the same | `--first-install <host>`, or restore the host's record in `installs/` |

`--first-install` never overrides the other rows: it only answers the question the last row can't. A new server whose firmware lists only network or CD entries needs neither flag. `--reinstall` wipes the host whatever the evidence; the run says which evidence it overrode.

The installer ISO and the answer files travel over plain HTTP from the bootstrap host, as ADR 0001 decided.

## Not done yet

- **A tagged management VLAN** (`managementVLAN: tagged`): the answer file can't set a VLAN, so the installer's network only works on an untagged port until post-install configuration exists.
- **Redfish preparation**: checking or setting UEFI boot mode, Secure Boot per `bootMode`, virtualization extensions, and that a disk matches `rootDeviceHints`.
- **Post-install**: the CAPMOX API token (`platform.proxmox.credentials.tokenID`), the bridge and its VLAN-aware setting, and the storage, through the Proxmox VE API.
- **ESXi.**
