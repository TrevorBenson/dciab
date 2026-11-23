# `dciab hypervisors`

Installs Proxmox VE on the servers in `spec.hypervisors.hosts` through their BMCs, as decided in [ADR 0001](adr/0001-day0-hypervisors-and-services.md). It runs as root on the bootstrap host after `dciab services`, whose Caddy serves each host's installer ISO to that host's BMC.

```console
$ sudo ./dciab hypervisors dciab.yaml --print          # the plan and answer files; changes nothing
$ sudo ./dciab hypervisors dciab.yaml                  # every host
$ sudo ./dciab hypervisors dciab.yaml --host pve02 --until media
```

Options: `--state DIR` (default `.dciab/state` next to the config, or `/var/lib/dciab/work/state` on the bootstrap appliance; the same folder `dciab services` uses), `--host NAME` (repeatable), `--reinstall NAME` (repeatable: wipe and reinstall that host even if it's installed), `--until STEP`, `--iso FILE` (the base installer ISO's file name in the ISOs folder; default the file name of the Proxmox VE ISO in `bundle.lock`), `--isos DIR` (the ISOs folder; default as for `dciab services`), `--timeout SECONDS` (per host, for `wait`; default 3600).

## Steps

Each step runs for every selected host and is safe to re-run.

| Step | What it does |
| --- | --- |
| `answer` | Renders the host's `answer.toml` into `<state>/answers/<host>.toml` (folder 700, file 600, written atomically). Files of hosts no longer in the config are removed |
| `iso` | Builds `<state>/host-isos/<host>.iso` (folder 700, file 600): the Proxmox VE installer ISO with that host's answer file inside (`proxmox-auto-install-assistant prepare-iso --fetch-from iso`, run with no network from the image built from `images/pve-assistant`). The base ISO is checked against `bundle.lock` first. An ISO is rebuilt only when its answer file or the base ISO changed. Caddy serves it at `http://<bootstrap.address>/host-isos/<host>.iso` to that host's BMC address only |
| `media` | Skipped when the host already answers on port 8006. Otherwise, over Redfish: finds the system's CD/DVD virtual media slot (under the System or its Manager), ejects other media, attaches the host's ISO, sets a one-time UEFI boot from `Cd`, and resets the server (`ForceRestart` when on, `On` when off; a server in a transitional power state is left alone). Redfish tasks a BMC accepts with HTTP 202 are waited for. The installer boots its "Automated Installation" entry and reads the answer file from the ISO: no DHCP and no answer server |
| `wait` | Waits for SSH (22) and then the Proxmox VE API (8006) on the host's address |

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

## Not done yet

- **A tagged management VLAN** (`managementVLAN: tagged`): the answer file can't set a VLAN, so the installer's network only works on an untagged port until post-install configuration exists.
- **Redfish preparation**: checking or setting UEFI boot mode, Secure Boot per `bootMode`, virtualization extensions, and that a disk matches `rootDeviceHints`.
- **Post-install**: the CAPMOX API token (`platform.proxmox.credentials.tokenID`), the bridge and its VLAN-aware setting, and the storage, through the Proxmox VE API.
- **Stronger install checks**: `media` never resets a host that answers on port 8006 or has an install recorded in `<state>/installs/<host>.json` (any file there, even an unreadable one), powered off or not, unless `--reinstall <host>` (repeatable) names it. A host with no record is also left alone when it answers on port 22, when its BMC lists a UEFI boot option for `proxmox`, or when its boot options can't be read. A recorded install still in progress, powered on with its ISO attached, isn't reset again; the run goes on to `wait`. Still missing: the record is the main guard. A powered-off host is reinstalled when the state folder (or its `installs` folder) is lost, or another one is used, and its BMC doesn't list a `proxmox` boot option. That includes a BMC that lists no boot options, and firmware that shows only a generic entry such as "UEFI OS" (after an NVRAM reset or a board swap, for example): vendors name those entries too differently to count them as an installed OS. `wait` still treats an open TCP port 8006 (and 22) as an installed host; checking the Proxmox VE API answer is next. The ISO and the answer files travel over plain HTTP from the bootstrap host, as ADR 0001 decided.
- **ESXi.**
