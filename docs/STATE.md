# The state folder

Every step keeps what it made in one folder, the state folder: the platform CA, sealed passwords and tokens, kubeconfigs, rendered configs and the records that let a step be rerun or undone. Give every step the same one with `--state DIR`.

> [!WARNING]
> Losing the state folder loses the platform CA's key, the hypervisors' generated root passwords and API tokens, and the kubeconfigs. Copy it somewhere safe before the bootstrap host is wiped or retired, and never delete it to "start again": `dciab seed --remove` and `dciab services --down` undo what was done to the host and keep the state.

## Where it is

| Bootstrap host | Default state folder |
| --- | --- |
| The bootstrap appliance (server zero) | `/var/lib/dciab/work/state`, on the work disk, outside the site copy the appliance replaces at each boot |
| A host you provide | `.dciab/state` next to the config |

After `dciab media`, the state lives at `<work>/state` on the work disk ([MEDIA.md](MEDIA.md)). Media moves any state already next to the config there and leaves a link in its place, and the commands it prints pass `--state <work>/state`. A step run with a different `--state` doesn't see the earlier steps' work.

The folder is mode 700 and owned by root once a root step has run. Under `sudo`, run every step as root, including `dciab status`.

## What's in it

**Secret** means the file must not leave the bootstrap host unencrypted. A `.age` file is encrypted to `spec.bootstrap.recipients`; open it with `age -d -i <identity> <file>`.

| Path | Written by | Secret | What it is |
| --- | --- | --- | --- |
| `pki/ca.crt` | `seed` | no | The platform CA (generated root, or the site's intermediate) |
| `pki/ca.key.age` | `seed` | sealed | The generated root CA's key (`pki.mode: generate-root` only). Without it, the registry certificate can't be reissued under the CA nodes already trust, and `seed` refuses to make a new CA unless given `--force-new-ca` ([SEED.md](SEED.md#the-platform-ca)) |
| `pki/trust-bundle.pem` | `seed` | no | What nodes and the bootstrap cluster trust |
| `pki/registry.crt`, `pki/registry.key` | `seed` | the key | The seed registry's TLS certificate and key, renewed by `seed` |
| `registry/` | `seed` | no | The seed registry's storage: every image from the payload |
| `kind/` | `seed` | no | The bootstrap cluster's registry mirror settings |
| `bootstrap.kubeconfig` | `seed` | yes | Admin access to the bootstrap (kind) cluster |
| `seed-created.json` | `seed` | no | What seed added to the host, for `dciab seed --remove` |
| `services/` | `services` (and `seed`, DNS-only) | no | The rendered Day 0 configs, `pod.yaml`, and `created`: what the services added to the host, for `--down` ([SERVICES.md](SERVICES.md)) |
| `isos/` | you, or `media` | no | Installer ISOs, when `--isos` isn't given and `workDisk` has no `path:` |
| `answers/<host>.toml` | `hypervisors` | no | Each host's Proxmox VE answer file |
| `answers/<host>.root-password.age` | `hypervisors` | sealed | A host's generated root password (when `spec.hypervisors.rootPassword` isn't set) |
| `answers/<host>.root-hash`, `.root-salt` | `hypervisors` | yes | The password's hash and salt, so reruns write the same answer file |
| `host-isos/<host>.iso` | `hypervisors` | yes | Each host's installer ISO, with its answer file (and root password hash) inside |
| `installs/<host>.json` | `hypervisors` | no | The install record: what was installed, its media and post-install state, the pinned API certificate. The reinstall guard reads it ([HYPERVISORS.md](HYPERVISORS.md#the-reinstall-guard)) |
| `proxmox/<host>.token.age` | `hypervisors` (`post`) | sealed | The CAPMOX API token's secret for each host DCiaB installed. A replaced token's file is kept as `.replaced-<time>` |
| `mgmt-values.yaml` | `mgmt` | no | MGMT's `capi-cluster` values |
| `mgmt.kubeconfig` | `mgmt` | yes | Admin access to MGMT (the certificate kubeconfig: break-glass) |
| `capi-backup/` | `mgmt` | encrypted | Cluster API exports from before the move and after adoption; `LAST` records the latest. The same exports are on the backup site target ([BACKUP.md](BACKUP.md)) |
| `backup/backup-key.age`, `backup-key.pub` | `mgmt` | sealed | The backup key, when the config gives none: its private half sealed to the recipients, and its public half |
| `.lock`, `dciab-seed.lock`, `hypervisors.lock`, `host-isos.base-checked` | the steps | no | Locks, and a cache of the base ISO's checked sha256 |

## Before retiring the bootstrap host

Keep a copy of at least `pki/`, `answers/*.age`, `proxmox/`, `backup/`, `installs/` and the kubeconfigs, encrypted or on media the site controls. With them, a new bootstrap host can renew the registry certificate under the same CA, reopen hosts' root passwords and tokens, and know which hosts DCiaB installed. The age identities of `spec.bootstrap.recipients` are kept separately, never in the state folder ([PLAN.md](PLAN.md#recipients-and-their-keys)).
