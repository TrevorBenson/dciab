# `dciab hypervisors`

Installs Proxmox VE on the servers in `spec.hypervisors.hosts` through their BMCs, as decided in [ADR 0001](adr/0001-day0-hypervisors-and-services.md). It runs as root on the bootstrap host after `dciab services`, whose Caddy serves the installer ISO and the answer files.

```console
$ sudo ./dciab hypervisors dciab.yaml --print          # the plan and answer files; changes nothing
$ sudo ./dciab hypervisors dciab.yaml                  # every host
$ sudo ./dciab hypervisors dciab.yaml --host pve02 --until media
```

Options: `--state DIR` (default `.dciab/state` next to the config, the same folder `dciab services` uses), `--host NAME` (repeatable), `--until STEP`, `--iso FILE` (the file name under `/isos/`; default the file name of the Proxmox VE ISO in `bundle.lock`), `--timeout SECONDS` (per host, for `wait`; default 3600).

## Steps

Each step runs for every selected host and is safe to re-run.

| Step | What it does |
| --- | --- |
| `answer` | Renders the host's `answer.toml` into `<state>/answers/<host>.toml` (folder 700, file 600, written atomically). Caddy serves it at `http://<bootstrap.address>/answers/<host>.toml` to that host's address only. Files of hosts no longer in the config are removed |
| `media` | Skipped when the host already answers on port 8006. Otherwise, over Redfish: finds the system's CD/DVD virtual media slot (under the System or its Manager), ejects other media, attaches `http://<bootstrap.address>/isos/<iso>`, sets a one-time UEFI boot from `Cd`, and resets the server (`ForceRestart` when on, `On` when off; a server in a transitional power state is left alone). Redfish tasks a BMC accepts with HTTP 202 are waited for |
| `wait` | Waits for SSH (22) and then the Proxmox VE API (8006) on the host's address |

BMC trust comes from each host's `bmc.tlsFingerprint` or `bmc.caBundle`, as for `dciab validate --live`; the certificate is checked before credentials are sent.

## The answer file

| Answer file key | From |
| --- | --- |
| `global.fqdn` | host `name` + `network.domain` |
| `global.mailto` | the first `identity.initialAdmins` email |
| `global.root-password-hashed` | `spec.hypervisors.rootPassword`, as a SHA-512 crypt hash (`openssl passwd -6`, password on stdin; the salt is derived from the host and password, so a rerun writes the same file) |
| `global.root-ssh-keys` | every `identity.initialAdmins` SSH key |
| `network` | `source = "from-answer"`: the host `address` with the node network's prefix, `network.nodes.gateway`, the first DNS service IP, and the NIC matched by `bootMACAddress` |
| `disk-setup` | `rootDeviceHints`: `deviceName` becomes `disk-list`; `serialNumber`, `wwn`, `model` and `vendor` become udev filters (`ID_SERIAL_SHORT`, `ID_WWN`, `ID_MODEL`, `ID_VENDOR`; spaces become underscores, as udev writes them) that must all match. `hctl`, `minSizeGigabytes` and `rotational` can't be expressed and are refused |

Fixed values, because the installer needs them and they don't affect the platform: `keyboard = "en-us"`, `country = "us"`, `timezone = "UTC"`, and `filesystem = "ext4"`, which gives the `local` and `local-lvm` storages of a default install.

Keys use the installer's kebab-case format.

## Not done yet

- **Getting the answer file to the installer.** The ISO must be prepared with `proxmox-auto-install-assistant`, and its HTTP fetch mode POSTs to one URL using DHCP for its own address; `dciab services` has no DHCP and Caddy's file server answers GET. Embedding each host's answer file in its own ISO (`--fetch-from iso`) avoids both but puts the password hash in a file under `/isos/`. Which way to go is still open.
- **A tagged management VLAN** (`managementVLAN: tagged`): the answer file can't set a VLAN, so the installer's network only works on an untagged port until post-install configuration exists.
- **Redfish preparation**: checking or setting UEFI boot mode, Secure Boot per `bootMode`, virtualization extensions, and that a disk matches `rootDeviceHints`.
- **Post-install**: the CAPMOX API token (`platform.proxmox.credentials.tokenID`), the bridge and its VLAN-aware setting, and the storage, through the Proxmox VE API.
- **Stronger install checks**: `media` and `wait` treat an open TCP port 8006 (and 22) as an installed host; checking the Proxmox VE API answer is next. The ISO and the answer files travel over plain HTTP from the bootstrap host, as ADR 0001 decided.
- **ESXi.**
