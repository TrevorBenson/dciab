# The DCiaB USB — under evaluation

One disk image that any OS writes to a stick with an ordinary imaging tool. It's a [Ventoy](https://www.ventoy.net) stick:

| On the stick | What it is |
| --- | --- |
| Partition 1, exFAT, label `DCIAB` | Everything you see from a laptop: `dciab.yaml`, `secrets/`, `examples/`, `isos/`, and the appliance vDisk |
| `dciab-bootstrap-<version>.raw.vtoy` | The [bootstrap appliance](../bootstrap-bootc/README.md), booted as a Ventoy Linux vDisk; the default menu entry |
| `isos/` | Installers to boot by hand from the same menu (with `--with-isos`, the hypervisor ISOs DCiaB ships) |
| `ventoy/ventoy.json` | Menu entry name and the default boot |
| Partition 2, VTOYEFI | Ventoy's boot loader |

Why Ventoy, and its Secure Boot trade-off: [ADR 0002](../../docs/adr/0002-bootstrap-appliance-on-ventoy.md).

## Build

```console
$ images/usb/build.sh --print
$ sudo images/usb/build.sh --with-isos            # after images/bootstrap-bootc/build.sh
```

Output: `images/usb/output/dciab-usb-<version>.img` and its `.sha256`. Ventoy's version and checksum come from `bundle.lock` (`bootstrapAppliance.ventoy`).

## Using it

1. Write the image to a USB 3.2 SSD-class stick, at least as large as the image.
2. On any laptop, open the `DCIAB` partition. Add `dciab.yaml` (start from `examples/`) and the files it references under `secrets/`. For ESXi, add the installer ISO of the build in `bundle.lock`.
3. Boot one x86_64 server on the node network from the stick. With Secure Boot on, the first boot on each machine asks you to enroll Ventoy's key in MokManager at the console (see the ADR).
4. SSH to `spec.bootstrap.address` and run `dciab`.
