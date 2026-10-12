# Releasing Nameplate

GitHub Actions owns publication in [`.github/workflows/release.yml`](.github/workflows/release.yml). Local packaging remains available for development; the former local release command has been removed.

## Prepare

Update `version.env` with the next marketing version and a strictly increasing build number. Nameplate also requires matching versions in both Linux Cargo manifests and `windows/Directory.Build.props`. Add a nonempty, dated `## X.Y.Z - YYYY-MM-DD` section to `CHANGELOG.md`, merge the release preparation, and wait for the **push CI run on that exact main commit** to succeed. A PR's checks alone do not qualify.

The release workflow accepts only signed annotated `vX.Y.Z` tags whose commit belongs to main. It verifies the SSH signature against `.github/release-allowed-signers`, the version metadata, release notes, increasing Sparkle build number, and absence of an existing release (drafts included). It never creates, moves, or replaces a tag.

Create a tag using the release SSH signing key after the preparation commit is green:

```sh
git tag -s vX.Y.Z <full-main-commit> -m 'Nameplate X.Y.Z'
git push origin refs/tags/vX.Y.Z
```

Alternatively, dispatch **Release** on main with `dry_run=false` and `tag=vX.Y.Z` to publish an already-existing signed tag. This does not create a tag.

## Prove without publishing

Dispatch **Release** on main with `dry_run=true` (the default) and leave `tag` empty. The workflow checks the current version's dated notes and exact-commit push CI, builds and tests, imports the Developer ID into an ephemeral keychain, signs, notarizes, staples, extracts the archive, verifies metadata and every Mach-O deployment target, and launches the app. It also signs the candidate Sparkle feed with the matching private key and checks the complete artifact inventory and SHA-256 sums.

This path uploads the `verified-release-<commit>` Actions artifact only. It creates no tags, GitHub Releases, appcast commits, or Homebrew PRs. It can verify the current version even when that version has already shipped. The artifact is retained for 14 days.

## Published channels

Nameplate ships an Apple silicon macOS ZIP and DMG, self-contained Windows x64/arm64 ZIPs, and Linux x86_64/arm64 tarballs. Every platform retains both versioned and stable filenames. Linux archives require system GTK4/X11 libraries.

Publication first uploads every file to a draft and downloads them again to compare exact bytes. Only then does it publish the GitHub Release with the dated changelog notes and `SHA256SUMS`. The signed `appcast.xml` is included in the release and committed to main, preserving the feed URL used by existing installations. A Homebrew PR updates the macOS cask and Linux source formula in `steipete/homebrew-tap`; merge that PR to complete the tap handoff.

The workflow refuses an existing release, including an existing draft. If a publication step fails, preserve the tag and draft, inspect the failed run and verified artifact, and repair the failed channel deliberately. Do not delete or move tags to restart publication. A failure after GitHub publication can leave the appcast or Homebrew handoff pending; finish those channels from the verified release bytes.

## Repository secrets

All credentials are repository secrets; do not add them to source or use the login keychain on hosted runners.

- `MACOS_SIGNING_P12`, `MACOS_SIGNING_P12_PASSWORD`: Peter Steinberger's Developer ID Application certificate (team `Y5PE65HELJ`).
- `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_PRIVATE_KEY_P8`: App Store Connect notarization credentials.
- `SPARKLE_PRIVATE_KEY`: Nameplate's EdDSA key, matching `.github/release.json` and the app's `SUPublicEDKey`.
- `HOMEBREW_TAP_TOKEN`: token with contents and pull-request write access to `steipete/homebrew-tap`.

The workflow token needs contents write to publish the release and update main's appcast; actions read checks the source CI run. No npm package is shipped, so there is no npm token or trusted publisher to configure.

`Scripts/mac-release` now permits only read-only status, notes, appcast, asset, and key verification commands through the shared release-mac-app helper. It cannot publish.
