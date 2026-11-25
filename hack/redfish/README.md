# Redfish test tooling (CI only)

`dciab hypervisors` talks to BMCs over Redfish. Its unit tests use a hand-written fake BMC
(`tests/test_hypervisors.py`). These tools run the same code against Redfish trees DCiaB didn't
write. None of this goes into the archive.

## Mockup Server: published Redfish trees

[DMTF's Redfish Mockup Server](https://github.com/DMTF/Redfish-Mockup-Server) serves a recorded
Redfish tree over HTTPS, accepts PATCHes into memory and answers actions. The trees are DMTF's
public mockups ([DSP2043](https://www.dmtf.org/dsp/DSP2043), in
[Redfish-Publications](https://github.com/DMTF/Redfish-Publications)): `public-rackmount1`,
`public-tower` and `public-localstorage`. Both repositories are BSD-3-Clause; the versions and
commits are pinned in `bundle.lock` `redfishTesting`.

DMTF's public mockups list no boot options, and vendors don't publish their own recorded
mockups under a license that allows redistribution. So `tests/fixtures/redfish/overlays/` adds
small resources on top of a public mockup, each written after a vendor's documented behaviour:

| Overlay | What it adds |
| --- | --- |
| `legacy-raid` | Dell-style legacy server: a RAID controller boot entry with no device path, a NIC entry, `BootMode: Bios` |
| `uefi-os-on-page-2` | HPE-style UEFI server: boot options on two pages (`Members@odata.nextLink`), the OS entry on the second |
| `network-only` | PXE and HTTP boot entries only |
| `empty-boot-options` | `Members: []` |
| `boot-options-no-members` | A collection page with no `Members` |
| `supermicro-dual` | `BootModeSelect: DUAL`, `IntelVirtualizationTechnology: Disabled` |
| `lenovo-legacy` | `BootModes_SystemBootMode: LegacyMode` |
| `uefi-virt-off` | `ProcVirtualization: Disabled` |
| `drives-no-volumes` | `public-localstorage` without its RAID volumes |
| `vmedia-actions` | `InsertMedia`/`EjectMedia` actions on `public-tower`'s Manager CD slot |

An overlay mirrors the mockup tree: `index.json` adds or replaces a resource, and
`index.patch.json` is a JSON merge patch (RFC 7386, `null` deletes a key) on the published one.

Run it locally:

```sh
python3 hack/redfish/fetch.py /tmp/rf          # the pinned server and mockups
pip install requests grequests multipart       # versions: bundle.lock redfishTesting.mockupServer.pip
DCIAB_REDFISH_MOCKUPS=/tmp/rf python3 -m pytest -q tests/test_redfish_mockups.py
```

Without `DCIAB_REDFISH_MOCKUPS` the tests are skipped. CI runs them in the `Redfish mockups` job.
