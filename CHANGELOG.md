# Changelog

## [0.3.0](https://github.com/EffizienzAgent/davsync/compare/v0.2.1...v0.3.0) (2026-10-27)


### Features

* **caldav:** compose outgoing events ([2ead43d](https://github.com/EffizienzAgent/davsync/commit/2ead43d528a8e061583a390bdd54ffb7dd19d6d4))
* **caldav:** write events and query free/busy (opt-in per account) ([4d14951](https://github.com/EffizienzAgent/davsync/commit/4d149513305eee345c81ff9441c4dcad9914cb9d))


### Bug Fixes

* **caldav:** surface import failures after writing ([0195716](https://github.com/EffizienzAgent/davsync/commit/019571634a9a3264da0b1bad39b23d89b6ecbd7b))

## [0.2.1](https://github.com/EffizienzAgent/davsync/compare/v0.2.0...v0.2.1) (2026-10-27)


### Bug Fixes

* **caldav:** leave out invitations the owner declined ([#13](https://github.com/EffizienzAgent/davsync/pull/13)) ([dd4a7f8](https://github.com/EffizienzAgent/davsync/commit/dd4a7f8b7ccd54f1fdb5022da65472d81c72e224))


### Documentation

* how tests get past the sync interval gate ([#11](https://github.com/EffizienzAgent/davsync/pull/11)) ([31eaac4](https://github.com/EffizienzAgent/davsync/commit/31eaac4edf6e4896c7112fec58c2170da9ae7bec))

## [0.2.0](https://github.com/EffizienzAgent/davsync/compare/v0.1.1...v0.2.0) (2026-10-26)


### Features

* configurable contacts and calendar sync intervals ([#9](https://github.com/EffizienzAgent/davsync/pull/9)) ([c47ab7d](https://github.com/EffizienzAgent/davsync/commit/c47ab7df571bf4bbe2af703a5ccabb13b0aa58c7))

## [0.1.1](https://github.com/EffizienzAgent/davsync/compare/v0.1.0...v0.1.1) (2026-10-24)


### Bug Fixes

* make Event caldav_uid unique per account instead of site-wide ([#2](https://github.com/EffizienzAgent/davsync/pull/2)) ([25e5a9c](https://github.com/EffizienzAgent/davsync/commit/25e5a9ce1031da34a759516ea72f475da562c6cb))
* harden the sync against forged fields and hostile servers ([a985358](https://github.com/EffizienzAgent/davsync/commit/a98535897dd1a1773765a4988ba010d3b65829d2))
* strip markup from remote text and expand rules only in the window ([487f835](https://github.com/EffizienzAgent/davsync/commit/487f8356886e7cceec8ed1ceb6624fcd20c85612))

## 0.1.0 (2026-10-24)


### Features

* CardDAV and CalDAV sync app ([22bc2b6](https://github.com/EffizienzAgent/davsync/commit/22bc2b6636431583888f4ef18faa4ecc2e0716bf))
