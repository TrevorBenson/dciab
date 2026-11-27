# Commands

Every `dciab` command, with its options. Each step's own page explains what it does; this page is the index. `dciab help` prints a short form.

Steps that change the host run as root. `services`, `hypervisors`, `templates`, `seed`, `mgmt` and `status` take `--state DIR` (and `validate` reads it) ([STATE.md](STATE.md)): give them all the same one. `media` takes none: it moves the state to `<work>/state` and prints the later commands with it. `--print` shows a step's plan and changes nothing; `--until STEP` stops after one of its stages.

## The steps, in run order

| Command | Page |
| --- | --- |
| `dciab validate <config> [--output text\|json] [--state DIR] [--live [--timeout SECONDS]]` | [VALIDATE.md](VALIDATE.md). On the appliance, run it with `sudo`: the site copy of the config is root-only |
| `dciab media <config> (--key PUB \| --checksums-only) [--source DIR] [--cosign BIN] [--isos-from DIR] [--until STEP] [--print]` | [MEDIA.md](MEDIA.md) |
| `dciab services <config> [--state DIR] [--isos DIR] [--payload DIR] [--until STEP] [--print]`<br>`dciab services <config> --down [--state DIR] [--forget '<line>']` | [SERVICES.md](SERVICES.md) |
| `dciab hypervisors <config> [--state DIR] [--isos DIR] [--iso FILE] [--host NAME ...] [--reinstall NAME ...] [--first-install NAME ...] [--age-identity FILE] [--timeout SECONDS] [--attach-timeout SECONDS] [--until STEP] [--print]` | [HYPERVISORS.md](HYPERVISORS.md) |
| `dciab templates <config> [--state DIR] [--payload DIR] [--age-identity FILE] [--replace] [--timeout SECONDS] [--print]` | [TEMPLATES.md](TEMPLATES.md) |
| `dciab seed <config> [--payload DIR] [--state DIR] [--age-identity FILE] [--force-new-ca] [--until STEP]`<br>`dciab seed <config> --remove [--state DIR] [--force] [--purge-state]` | [SEED.md](SEED.md) |
| `dciab mgmt <config> [--state DIR] [--bootstrap-kubeconfig FILE] [--trust-bundle FILE] [--payload DIR] [--until STEP]` | [MGMT.md](MGMT.md) |
| `dciab infra`, `dciab platform`, `dciab finalize` | Not built yet ([ROADMAP.md](ROADMAP.md)) |

## Other commands

### `dciab status <config> [--state DIR] [--output text|json]`

Where the bootstrap stands, read-only: the Day 0 pod, the platform CA and registry certificate, the seed registry, the bootstrap cluster, and where MGMT's Cluster lives. Run it as root: the state folder is root-only, and without root it can't read it. Exit code `0` when every check that applies is ok, `1` otherwise.

### `dciab cluster-values <config> <cluster> [--state DIR] [--trust-bundle FILE] [-o FILE]`

Prints a cluster's `capi-cluster` chart values, generated from the config: `mgmt`, `infra` or a workload cluster (`--list` names them). `-o FILE` writes them to a file (mode 600). With `pki.mode: generate-root`, pass `--trust-bundle <state>/pki/trust-bundle.pem` once `seed` has created the platform CA. It validates the config first; `--state` names the steps' state folder, whose install records that check reads on a bare Proxmox VE site ([VALIDATE.md](VALIDATE.md)). `dciab mgmt` runs it for MGMT with its own `--state`.

### `dciab verify-archive (--key PUB | --checksums-only) [--cosign BIN] [ARCHIVE]`

Checks an archive (this one by default, or a `dciab-<version>.tar` or its unpacked folder) against its signed `MANIFEST`: the signature, every listed file, and nothing unlisted added. `--checksums-only` skips the signature, for unsigned builds only. `--cosign BIN` names the cosign binary (default: cosign on PATH, else the archive's `payload/bin/cosign`). `dciab media` runs it on its copy; [RELEASING.md](RELEASING.md) shows how to check a downloaded release.

### `dciab help`

Lists the commands and their main options.

`dciab verify` (site checks) isn't built yet.

## Exit codes

The Python commands (`validate`, `media`, `hypervisors`, `templates`, `seed`, `status`, `verify-archive`, `cluster-values`) use the same codes: `0` done (valid, matching, every check ok), `1` a step or check failed, `2` could not run (bad arguments, an unreadable config). Run without root, `media`, `hypervisors` and `templates` exit `2` (except with `--print`, which needs no root) and `seed` exits `1`. The shell steps (`services`, `mgmt`) exit non-zero on any failure.
