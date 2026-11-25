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
small, hand-written resources on top of a public mockup. **None is captured from a vendor BMC,
and none cites a vendor source**, so each is named after the behaviour it models, not after a
vendor. Where an overlay uses a vendor's BIOS attribute name, it is the name
`lib/dciab_hypervisors/prepare.py` looks for, and the "Name seen on" column says whose firmware
uses it; captured mockups (Redfish-Mockup-Creator, sanitized) from real BMCs would replace these.

| Overlay | What it models | Name seen on |
| --- | --- | --- |
| `legacy-controller-entry` | Legacy boot: a RAID controller boot entry with no device path, a NIC entry, `BootMode: Bios` | Dell (`BootMode`) |
| `paging-path-style` | Boot options on two pages, the second linked by a path-style `Members@odata.nextLink` (`.../Page2`), the OS entry on page 2. Query-style paging (`?page=`) is covered in `tests/test_hypervisors.py` | — |
| `network-only` | PXE and HTTP boot entries only | — |
| `empty-boot-options` | `Members: []` | — |
| `boot-options-no-members` | A collection page with no `Members` | — |
| `bootmodeselect-dual` | `BootModeSelect: DUAL`, `IntelVirtualizationTechnology: Disabled` | Supermicro |
| `bootmodes-legacymode` | `BootModes_SystemBootMode: LegacyMode` | Lenovo |
| `procvirtualization-off` | `ProcVirtualization: Disabled` | Dell |
| `drives-no-volumes` | `public-localstorage` without its RAID volumes | — |
| `vmedia-actions` | `InsertMedia`/`EjectMedia` actions on `public-tower`'s Manager CD slot. The Mockup Server acknowledges actions without changing the slot, which models a BMC that accepts an insert and then fails to mount it | — |

An overlay mirrors the mockup tree: `index.json` adds or replaces a resource, and
`index.patch.json` is a JSON merge patch (RFC 7386, `null` deletes a key) on the published one.

Run it locally:

```sh
python3 hack/redfish/fetch.py /tmp/rf          # the pinned server and mockups
pip install --require-hashes -r hack/redfish/requirements.txt   # bundle.lock redfishTesting.mockupServer.requirements
DCIAB_REDFISH_MOCKUPS=/tmp/rf python3 -m pytest -q tests/test_redfish_mockups.py
```

Without `DCIAB_REDFISH_MOCKUPS` the tests are skipped. CI runs them in the `Redfish mockups` job.
