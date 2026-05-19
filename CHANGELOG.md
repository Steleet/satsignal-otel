# Changelog

## 0.2.0

Additive proof/folder vocabulary aliases — fully backward-compatible.

- New `folder_slug=` on `SatsignalSpanProcessor(...)` and on
  `SatsignalApi.anchor_standard` / `anchor_manifest`, plus
  `.folder_slug` / `.matter_slug` accessors and
  `AnchorResult.folder_slug` / `proof_url` / `proof_id`, alongside the
  frozen legacy `matter_slug=`.
- The previously-required ctor `matter_slug=` is now satisfiable by
  **either** `folder_slug` or `matter_slug` (raise only if neither) —
  no previously-working construction breaks.
- Conflict rule: `folder_slug` and `matter_slug` with different
  non-empty values raise before any network/thread work (mirrors the
  server's `conflicting_alias`); equal accepted.
- Response reading prefers the new `folder_slug` / `proof_*` keys with
  legacy fallback; the HTTP request body still sends the frozen
  `matter_slug` wire token, so this works unchanged against every
  Satsignal server (including older / self-hosted deployments).
- `User-Agent` default aligned to the package version.

This package has no CLI and no `SATSIGNAL_MATTER` env (ctor/library
only); that surface is unchanged. Every existing `matter_slug=` usage
keeps working byte-identically.

## 0.1.1 and earlier

See the git history.
