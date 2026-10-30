# The winget manifest

These three files are what a pull request to
[`microsoft/winget-pkgs`](https://github.com/microsoft/winget-pkgs) adds, under

    manifests/m/maiconzeraxd/HyperVConsoleMCP/<version>/

so that `winget install maiconzeraxd.HyperVConsoleMCP` works. They are kept here
so the identifiers, the installer type and Inno's uninstall key stay beside the
installer script that defines them; a test checks the `ProductCode` against the
`AppId` in `packaging/installer.iss`.

Per release, three fields change:

- `PackageVersion` in all three files.
- `InstallerUrl` in the installer manifest — the draft release's own asset URL.
- `InstallerSha256` — `winget hash HyperVConsoleMCP-Setup-<version>.exe`.

Then, before opening the PR:

```powershell
winget validate --manifest packaging\winget
winget install --manifest packaging\winget
```

The second command installs from the local manifest, which is the only way to
know the `InstallModes`, the `Scope` and the uninstall key are right without
waiting for a reviewer. The CI workflow does not submit anything: a manifest
that has never been through `winget validate` should not be.
