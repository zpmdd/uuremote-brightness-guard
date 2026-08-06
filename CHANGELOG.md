# Changelog

All notable changes to this project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-08-06

### Added

- Automatic UU Remote session detection from the local server log.
- Per-display capture, absolute dimming, and restoration for built-in and DDC/CI external displays.
- MonitorControl coordination and safe 85%/70% fallback values.
- Stale-state recovery across Mac and monitor restarts.
- Display-only sleep after a successful disconnect restore.
- Per-user LaunchAgent installation, status, emergency restore, and fail-safe uninstall controls.
- Built-in-display-only installation without requiring MonitorControl.
- English and Simplified Chinese documentation plus macOS CI.

[1.0.0]: https://github.com/zpmdd/uuremote-brightness-guard/releases/tag/v1.0.0
