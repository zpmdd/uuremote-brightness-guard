# Changelog

All notable changes to this project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [1.0.3] - 2026-09-04

### Fixed

- Replace UU Remote UDP-socket detection with real ScreenCaptureKit stream lifecycle events, preventing background traffic and sleep assertions from dimming displays without a remote session.
- Use the UU display-sleep assertion only as a negative safety check, and fail open if monitoring becomes unavailable so displays are restored instead of remaining black.

## [1.0.2] - 2026-09-04

### Fixed

- Restore inbound-session detection with UU Remote 4.39.0, which replaced the plaintext server log with binary Xlog files.
- Detect active remote-control channels through the native macOS process interface instead of depending on UU Remote's private log format.
- Keep the last known session state when process inspection is temporarily unavailable, preventing an active remote session from restoring local brightness early.

## [1.0.1] - 2026-08-07

### Fixed

- Verify built-in brightness, external DDC values, and external gamma after the displays wake; automatically reapply the saved snapshot when MonitorControl shows the expected percentage but the picture remains too dark.
- Keep the recovery snapshot until post-wake verification succeeds, and retry failed repairs instead of discarding the last known-good values.
- Retarget saved gamma tables by the verified display topology when macOS changes a transient display identifier during wake.

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

[1.0.3]: https://github.com/zpmdd/uuremote-brightness-guard/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/zpmdd/uuremote-brightness-guard/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/zpmdd/uuremote-brightness-guard/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/zpmdd/uuremote-brightness-guard/releases/tag/v1.0.0
