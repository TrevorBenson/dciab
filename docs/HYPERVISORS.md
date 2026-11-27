# `dciab hypervisors`

Installs Proxmox VE on the servers in `spec.hypervisors.hosts` through their BMCs, as decided in [ADR 0001](adr/0001-day0-hypervisors-and-services.md). It runs as root on the bootstrap host after `dciab services`, whose Caddy serves each host's installer ISO to that host's BMC.

```console
$ sudo ./dciab hypervisors dciab.yaml --print          # the plan and answer files; changes nothing
$ sudo ./dciab hypervisors dciab.yaml                  # every host
$ sudo ./dciab hypervisors dciab.yaml --host pve02 --until media
```

Options: `--state DIR` (default `.dciab/state` next to the config, or `/var/lib/dciab/work/state` on the bootstrap appliance; the same folder `dciab services` uses), `--host NAME` (repeatable), `--reinstall NAME` (repeatable: wipe and reinstall that host even if it's installed), `--first-install NAME` (repeatable: install that host when nothing shows whether it holds an install; see [the reinstall guard](#the-reinstall-guard)), `--until STEP`, `--iso FILE` (the base installer ISO's file name in the ISOs folder; default the file name of the Proxmox VE ISO in `bundle.lock`), `--isos DIR` (the ISOs folder; default as for `dciab services`), `--timeout SECONDS` (per host, for `wait`; default 3600), `--attach-timeout SECONDS` (how long `media` waits for the CD slot to show the ISO; default 60; see [the attach check](#the-attach-check)), `--age-identity FILE` (an age identity for one of `spec.bootstrap.recipients`: `post` opens a generated root password with it, and the CAPMOX token's sealed copy to write it where `tokenSecret` points when that file is missing or stale).

## Steps

Each step runs for every selected host and is safe to re-run.

| Step | What it does |
| --- | --- |
| `answer` | Renders the host's `answer.toml` into `<state>/answers/<host>.toml` (folder 700, file 600, written atomically). For a host no longer in the config, its answer file and root password files (`.root-hash`, `.root-salt`, `.root-password.age`) are removed. If it has an install record, its password files and the record are kept and the run says so: the server may still use that password. Delete the password files by hand once the server is wiped or retired |
| `iso` | Builds `<state>/host-isos/<host>.iso` (folder 700, file 600): the Proxmox VE installer ISO with that host's answer file inside (`proxmox-auto-install-assistant prepare-iso --fetch-from iso`, run with no network from the image built from `images/pve-assistant`), and, with `managementVLAN: tagged`, its [first-boot script](#the-management-vlan) (`--on-first-boot`). The base ISO is checked against `bundle.lock` first. An ISO is rebuilt only when its answer file, its first-boot script or the base ISO changed. Caddy serves it at `http://<bootstrap.address>/host-isos/<host>.iso` to that host's BMC address only |
| `media` | Skipped when the host's Proxmox VE API already answers, and held when anything shows an existing install ([the reinstall guard](#the-reinstall-guard)). Otherwise, it first [prepares the server](#redfish-preparation) over Redfish (boot mode, Secure Boot, virtualization, the disk), and refuses the install on a clear disk mismatch. Then, over Redfish: finds the system's CD/DVD virtual media slot (under the System or its Manager), ejects other media, attaches the host's ISO and waits for the slot to show it ([the attach check](#the-attach-check)), sets a one-time UEFI boot from `Cd`, and resets the server (`ForceRestart` when on, `On` when off; a server in a transitional power state is left alone). Redfish tasks a BMC accepts with HTTP 202 are waited for. The installer boots its "Automated Installation" entry and reads the answer file from the ISO: no DHCP and no answer server |
| `wait` | Waits for SSH (22) and then the Proxmox VE API on the host's address (an answer from `/api2/json/version` on 8006, not just an open port), and records the install as done. Then, for a host whose ISO DCiaB attached, ejects that ISO and clears the boot override DCiaB set if it still points at `Cd` (Target `None` with Enabled `Disabled`, since some BMCs refuse Enabled alone; a BMC that answers that with a 4xx gets Enabled `Disabled` alone, and the run says which form it took), so a fallback to CD never boots the installer again. Other media and overrides are left alone. The result goes in the install record (`media`: `detached`, `detach-failed`, or `detach-unverified` when other media is in the slot). Only `detached` is final: otherwise the run says how to eject by hand, and the next run checks again. This never fails the run. Each new install (`media`, `--reinstall` too) starts the record's `media` over at `attached` |
| `post` | For each host DCiaB installed (its record names the ISO DCiaB attached) that is in `spec.platform.proxmox.nodes`: [post-install](#post-install) through the Proxmox VE API. Done once per install; the record says when (`post: done`) |

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

With `managementVLAN: tagged` the answer file also has `[first-boot]` with `source = "from-iso"` and `ordering = "before-network"` (see below).

## The management VLAN

`spec.hypervisors.managementVLAN` says how each host's management NIC reaches the node network (`network.nodes.vlan`). Both modes work from the first boot; nothing is moved later, and DCiaB never changes a switch.

| Mode | The host's switch port | What the host gets |
| --- | --- | --- |
| `untagged` | an access port for `network.nodes.vlan`, or a trunk with it as the native (untagged) VLAN | the installer's network as it writes it: the management address on `vmbr0`, which bridges the NIC untagged |
| `tagged` | a trunk that carries `network.nodes.vlan` tagged (a native VLAN, if any, isn't used) | `vmbr0` VLAN-aware (`bridge-vids` from `spec.hypervisors.bridgeVids`, default `2-4094`) with no address, and the management address and gateway on `vmbr0.<network.nodes.vlan>` |

The Proxmox VE answer file can't set a VLAN, but the install itself needs no network (the answer file is on the ISO). So for `tagged`, each host's ISO also carries a small first-boot script, run by the installed system before any network device is set up (Proxmox VE's first-boot hook, `ordering = "before-network"`). The host's first network traffic is already tagged. `--print` shows the script.

What the script changes in `/etc/network/interfaces`, editing the `vmbr0` stanza in place:
- `iface vmbr0 inet static` becomes `inet manual`;
- the stanza's `address`, `netmask` and `gateway` lines move to a new `auto vmbr0.<vlan>` / `iface vmbr0.<vlan> inet static` stanza (the config's address when the stanza has no `address` line, a lone `netmask` line then being dropped, and `network.nodes.gateway` when it has no `gateway` line);
- `bridge-vlan-aware yes` and `bridge-vids <bridgeVids>` are added, replacing any the stanza had (either spelling, `-` or `_`).

Every other key in the stanza (`bridge-ports` or `bridge_ports`, `bridge-stp`, `mtu`, `hwaddress`, comments) and every other line in the file stays as it was. The installer's file is kept once as `/etc/network/interfaces.dciab-installer`. A rerun changes nothing. If the `vmbr0` stanza has command lines (`up`, `down`, `pre-up`, `post-up`, `pre-down`, `post-down`, either spelling), the script refuses and changes nothing: they may use `vmbr0`'s address or name, and moving the address could cut the host off. The installer never writes them, so this only happens when the file was edited by hand; remove or move those lines, and the hook runs again at the next boot.

**When the hook runs, and when it fails.** Upstream runs it from the oneshot unit `proxmox-first-boot-network-pre.service` (ordered before `network-pre.target`, so before ifupdown2 brings up any interface), only while the flag `/var/lib/proxmox-first-boot/pending-first-boot-setup` exists; the flag is removed once the hook succeeds ([pve-devel patch](https://lists.proxmox.com/pipermail/pve-devel/2024-November/066653.html)). So it runs at every boot until it succeeds once, and never after that. If it fails, networking still starts with the installer's untagged `vmbr0`, and on a tagged-only port the host can't be reached: `wait` then times out, and its message says what to check on the host's console through its BMC: `journalctl -t dciab-first-boot` (the script's log), `systemctl status proxmox-first-boot-network-pre`, and that the switch port carries the node VLAN tagged.

**`bridgeVids`.** By default a VLAN-aware `vmbr0` carries VLANs 2-4094, Proxmox VE's own default, so VMs can use any VLAN. Some NICs have a smaller VLAN filter table than that (the Mellanox ConnectX-4 Lx, for example, takes 512 and drops the rest, [Proxmox forum](https://forum.proxmox.com/threads/proxmox-7-and-mellanox-connectx4-and-vlan-aware-bridge.104926/)); in `tagged` mode the management VLAN can be among the dropped ones, and the host can't be reached. Set `spec.hypervisors.bridgeVids` to the VLANs the site uses (IDs and ranges separated by spaces, such as `"110 200-210"`). It applies to the first-boot script and to `post`'s bridge change in `untagged` mode.

Rules `dciab validate` checks for `tagged`: `network.nodes.vlan` isn't 1 (VLAN 1 is the bridge's default PVID, so it isn't supported: use `untagged`); `spec.platform.proxmox.bridge` is `vmbr0`; `platform.proxmox.vlanTag` equals `network.nodes.vlan`, because `vmbr0` is then a trunk and node VMs' NICs must tag the node VLAN to reach the node network; and `bridgeVids`, when set, includes the node VLAN. In either mode `bridgeVids` must be well formed, within 2-4094, and include `vlanTag` when that is set. With `untagged`, `vlanTag` stays optional.

**Changing the mode or VLAN later.** Changing `managementVLAN` or `network.nodes.vlan` for hosts already installed rebuilds their ISOs but doesn't touch the hosts: `media` skips a host whose API answers, and `post` runs once per install. Each install record keeps the mode and node VLAN it was installed for, and `dciab hypervisors` prints a note for an installed host whose record differs from the config. Reinstall it (`--reinstall`) or change its network by hand through its console before moving its switch port.

## Access

- **People:** root over SSH with the `identity.initialAdmins` keys. The root password is for joining the host to a cluster or manager (a Proxmox VE cluster, vCenter, Proxmox Datacenter Manager), for post-install setup through the API, and for break-glass console access; SSO through Authentik as a Proxmox VE realm comes later ([ADR 0004](adr/0004-identity-and-sso.md)).
- **DCiaB and CAPMOX** at run time: the Proxmox VE API with a dedicated API token, never SSH. [PROXMOX.md](PROXMOX.md) lists its exact privileges.
- **Existing hypervisors** (a site that already runs Proxmox VE or vSphere): DCiaB never touches root; it uses only the API token or vCenter account in the config.


## The attach check

A BMC can accept the request to attach an ISO and then fail to mount it, for example when it can't reach the bootstrap host's URL. So after attaching, `media` reads the CD slot until it shows the host's ISO, for up to `--attach-timeout` seconds (default 60). BMCs echo the image back in different forms, so the slot counts as showing the ISO when it is inserted and:

- its `Image` is the same URL, or the same after normalising: scheme and host in any case, credentials and a default port (80, 443) dropped, a trailing slash ignored; or
- it names no usable URL, and its `ImageName` (or an `Image` that is only a file name, a path or a malformed URL) is the ISO's file name.

The file-name form counts only after DCiaB has emptied the slot and inserted the ISO itself (the attach check, an install recorded in progress, and ejecting after the install), because then the mounted media can only be its own. Before attaching, media already in the slot counts as the ISO only when its `Image` is the same URL (exactly or after normalising): media that only shares the file name may be stale (an earlier upload, another site's ISO) and would boot an old answer file, so it is ejected and the ISO inserted. A different URL (another host, port, scheme, path or query) never counts. `--attach-timeout` must be more than 0. When the slot never shows the ISO, or can't be read:

- the ISO is ejected anyway, because a slow mount can land after the last read (a BMC that refuses the eject, for example with HTTP 405 on an empty slot, is logged and ignored);
- a boot override is cleared only if this run set one; an override DCiaB didn't set this run is left as it is;
- the slot is read once more, and the run says whether the ISO is still attached (if it is, eject it through the BMC before the server's next power-on);
- the host fails with nothing reset.

The same undo runs when setting the one-time boot from `Cd` fails after a good attach: the ISO is ejected and the override, which may have half-applied, is cleared before the host fails.

## Redfish preparation

Before `media` attaches the ISO to a host it is going to install (never to a host the guard holds), it prepares the server over Redfish. Changes are staged to apply at the reset that boots the installer. `--print` lists what will be checked and set; it doesn't talk to the BMC.

| Check | How | When it can't be read |
| --- | --- | --- |
| UEFI boot mode | The BIOS boot mode attribute: `BootMode` (Dell, HPE), `BootModeSelect` (Supermicro) or `BootModes_SystemBootMode` (Lenovo). A value that allows legacy boot, legacy only or mixed (`Bios`, `LegacyBios`, `LEGACY`, `DUAL`, `UEFIandLegacy`), on a host with no install record holds it (see [the reinstall guard](#the-reinstall-guard)). With `--reinstall`, `BootMode` is staged to `Uefi`; the Supermicro and Lenovo attributes are only read, and a note says to set UEFI in the firmware setup or through the BMC | A note: the installer is booted in UEFI mode, which fails on a server set to legacy boot |
| Secure Boot | `ComputerSystem.SecureBoot`: `SecureBootEnable` set on for `bootMode: UEFISecureBoot`, off for `UEFI`. Never changed while the boot mode still reads legacy (firmware refuses Secure Boot in legacy mode): a note says to set it once the boot mode is UEFI | A note: set it in the firmware setup to match `bootMode` |
| Virtualization extensions | A known BIOS attribute (`ProcVirtualization`, `IntelVirtualizationTechnology`, `VirtualizationTechnology`, `IntelVTX`, `VMX`, `SVMMode`, `AmdVirtualization`, ...) set to `Disabled` (or the boolean `false`) is staged to `Enabled` (or `true`) | A note: VMs won't start on the host without them |
| The disk | `rootDeviceHints` against the drives and volumes under the system's `Storage` (every page). `serialNumber` must match exactly; `wwn` must match exactly or share at least 16 trailing hex digits; `model` and `vendor` match a part, ignoring case and spaces. Serial number and WWN alone decide: on the drive they match, a different `model` or `vendor` is a note | No drives listed, only `deviceName`, or only `model`/`vendor` that don't match: a note, and the installer decides |

The install is **refused** (the host fails the run, and nothing is written to the BMC, not even the BIOS or Secure Boot changes) when the BMC lists drives, none has the hinted `serialNumber` or `wwn`, and the storage has no RAID volumes. With RAID volumes the installer may see a virtual disk under other identifiers, so a mismatch there is a note. A `wwn` hint with no digits (`0x`, `naa.`) is refused too.

BIOS changes are written before Secure Boot. If a write fails partway, or attaching the ISO fails after preparation, the run names the changes already staged in the firmware: they apply at the next reset of any kind, so revert them in the BMC if the host shouldn't change.

BIOS changes go to the settings object the Bios resource names (`Bios/Settings`), with `@Redfish.SettingsApplyTime: OnReset` when the BMC offers it. Attribute names vary by vendor; a server whose firmware uses other names gets notes, not failures.

## Post-install

`post` logs in to the Proxmox VE API as `root@pam` with the host's root password (`spec.hypervisors.rootPassword`, or the generated one, opened from `<state>/answers/<host>.root-password.age` with `--age-identity`). It never uses SSH, and it never logs in as root to a host DCiaB didn't install.

| What | How |
| --- | --- |
| Trust | The API's certificate is pinned: `spec.platform.proxmox.tlsFingerprint` when `platform.proxmox.url` names this host and the config gives one (the run stops with the certificate's fingerprint when they differ), else the fingerprint seen on first contact, recorded in the install record (`api_fingerprint`) and checked on every later run. For the host the URL names, `validate` and the later steps then trust the API by that pin ([trust on first use](#trust-on-first-use)). `tlsFingerprint` may be SHA-256 or SHA-1 |
| Bridge | `spec.platform.proxmox.bridge` must exist (the installer makes `vmbr0` on the management NIC). With `managementVLAN: tagged` it is only checked: it must be VLAN-aware with the host's address on `vmbr0.<network.nodes.vlan>`, as the first-boot script left it, and nothing is changed; otherwise the run stops and points to the script's journal log. With `managementVLAN: untagged` and `platform.proxmox.vlanTag` it is made VLAN-aware (`bridge_vids` from `spec.hypervisors.bridgeVids`, default `2-4094`), keeping its address and gateway, and the network is reloaded. The API must answer again within about 30 seconds; otherwise the run stops, saying the reload left the API unreachable, and the host's console (through its BMC) is the way back |
| Storage | `spec.platform.proxmox.storage` must exist on the node, enabled and active, and hold VM disks; `local` must take ISOs (CAPMOX uploads each machine's bootstrap data there) and gains `iso` content when it doesn't. DCiaB doesn't create other storage |
| CAPMOX token | The roles, user, privilege-separated token and ACLs in [PROXMOX.md](PROXMOX.md), from `lib/dciab_validate/proxmox_privs.py`. Roles whose privileges differ are corrected. The token's secret, shown once, is encrypted at once to `spec.bootstrap.recipients` in `<state>/proxmox/<host>.token.age`. For the host `platform.proxmox.url` names, it is also written where `platform.proxmox.credentials.tokenSecret` points (mode 600), so the later steps find it with no copying by hand ([the token's home](#the-capmox-tokens-home)). A token that already exists is kept only when its sealed secret is in the state folder; otherwise the run says how to replace it. With `--age-identity`, a kept token's file is written again from the sealed copy when it is missing or stale |

### Trust on first use

A host DCiaB just installed has a new self-signed certificate that nothing could know beforehand. `post`'s first contact, right after `wait` sees the API answer, is when it is pinned; from then on, every connection checks it before sending anything. Copying the fingerprint into the config by hand adds nothing unless someone checks it on the host's console (`pvenode cert info`), so the config may leave `tlsFingerprint` out for the host `platform.proxmox.url` names, and DCiaB uses the install record's pin. The state folder is root-only, like the root passwords and tokens it holds. Set `tlsFingerprint` to pin a certificate checked out of band instead. Certificates signed by the site's CA are planned.

### The CAPMOX token's home

`validate --live`, `seed` and `mgmt` read the token's secret through `tokenSecret`. When `post` creates the token on the host `platform.proxmox.url` names, it writes the secret there itself:

- **On the bootstrap appliance** (the config is `/var/lib/dciab/site/dciab.yaml`), into the site copy and into the appliance-local secrets, `/var/lib/dciab/site-local/secrets/<the same path>` (root-only, on the appliance's disk). Each sync from the stick puts the appliance-local file over the stick's, so a reboot or a config change keeps the token, and the stick needs no token file. `tokenSecret` must name a file under `./secrets/`: `post` refuses any other path, an absolute one too, and `validate` reports it as an error.
- **On a host you provide,** into the file `tokenSecret` names, relative to the config.

It never writes the secret to removable media (an exFAT, FAT or NTFS file system, such as the stick's partition, which any laptop can read), over the config file itself, over a file another secret reference names, or for `{env: …}`; then the run says so, and `age -d -i <identity> <state>/proxmox/<host>.token.age` opens the sealed copy ([TROUBLESHOOTING.md](TROUBLESHOOTING.md)). `validate` refuses the same files for a token `post` writes. Folders it makes are mode 700, the file mode 600. The other hosts' tokens are only sealed: the config names one API.

The file is written after the token's ACLs are set, and a write that fails (a full disk, a permission error) never fails `post`: the token exists and is sealed, and the run says so. A later run of `dciab hypervisors` with `--age-identity` writes the file from the sealed copy when it is missing or holds something else, even once `post` is done (that needs no login); without an identity, the run says the file is missing.

A new install (`media`, `--reinstall` too) clears the record's post-install result and pinned certificate, so `post` runs again on the reinstalled host and pins its new certificate. Its old token no longer exists, so its sealed secret is moved aside to `<state>/proxmox/<host>.token.age.replaced-<time>` (mode 600, never deleted) and `post` creates a new one. A failed `post` records why in the install record (`post_log`).

The user must be in the Proxmox VE realm (`<user>@pve!<token>`). On hosts that aren't clustered, each host in `platform.proxmox.nodes` gets its own token (clustering, which makes this once per cluster, is a later step).

## The reinstall guard

`media` resets a server only when nothing suggests it already holds an install. It holds a host, does nothing to it, and exits 1 when:

| Evidence | To install anyway |
| --- | --- |
| The Proxmox VE API answers on the host's address: `GET /api2/json/version` over TLS returns Proxmox VE's version answer (JSON with `data.version`), or the API daemon's own 401 (`Server: pve-api-daemon`). Another service on port 8006 doesn't count, even one that sends the daemon's `Server` header with some other body | `--reinstall <host>` |
| An install is recorded in `<state>/installs/<host>.json` (any file there, even an unreadable one), powered off or not. A recorded install still in progress, powered on with its ISO attached, isn't reset again: the run goes on to `wait` | `--reinstall <host>` |
| No record, but SSH (22) answers | `--reinstall <host>` |
| No record, and port 8006 is open but doesn't answer as the Proxmox VE API (a timeout, a 403 from `pveproxy`'s access list, a server error, another service): something runs there | `--reinstall <host>` |
| No record, but the BMC lists a boot option that may load an installed OS: Proxmox VE (`\EFI\proxmox`), a known OS name, the generic "UEFI OS" entry firmware creates after an NVRAM reset or a board swap, a legacy BIOS disk entry ("Hard drive C:", `BBS(HD,...)`), or any entry whose device path points into a local disk: a partition (`HD(...)`), or a whole disk on a SATA, SCSI, SAS or NVMe bus with no partition node (a path ending in an optical drive, `CDROM(...)`, is a disc, not a disk). An entry with no device path counts when its name names a disk or a disk controller ("Integrated RAID Controller 1: PERC H730", "Embedded SATA Port Disk A": the whole words disk, drive, RAID, SATA, SAS, NVMe, SSD or HDD), unless it names an optical or floppy drive. OS evidence wins over other words in the name: "UEFI OS (Virtual Disk)" counts, and so does a USB-named entry on a partition, such as a bootable stick left in the server. Only network entries (PXE, HTTP boot) never count; CD/DVD, shell and setup entries count only with such evidence | `--reinstall <host>` |
| No record, and the BMC's boot options can't be read in full: a page fails, the pages don't end, or a page has no `Members` (only an explicit `Members: []` means the BMC lists none) | `--reinstall <host>` |
| No record, and the BIOS boot mode allows legacy boot, legacy only or mixed (checked by [the preparation](#redfish-preparation), before any write): a legacy-mode server may hold an OS its boot entries don't show | `--reinstall <host>` |
| No record, and the BMC lists no boot options at all (no boot options collection, or an empty one): a new server and one whose state folder was lost (or a different `--state`) look the same | `--first-install <host>`, or restore the host's record in `installs/` |

`--first-install` never overrides the other rows: it only answers the question the last row can't. A new server whose firmware lists only network or CD entries needs neither flag. `--reinstall` wipes the host whatever the evidence; the run says which evidence it overrode.

The installer ISO and the answer files travel over plain HTTP from the bootstrap host, as ADR 0001 decided.

## Not done yet

- **Joining a Proxmox VE cluster** and shared storage across hosts.
- **ESXi.**
