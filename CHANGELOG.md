# Changelog

All notable changes to this project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Recognize UU Remote 4.41.2's renamed active-session display-sleep assertion while retaining the older name, preventing false disconnects and display-sleep requests during an active capture.
- Make the five-second post-restore display-sleep delay cancellable and recheck that UU is disconnected immediately before sleeping the displays.
- Collapse queued ScreenCaptureKit events to their final session state and bridge capture-stream turnover while UU's active-session assertion remains present.
- Stop automatic brightness writes after one unsuccessful post-wake repair, preserving manual control instead of retrying forever.
- Cancel wake verification when displays return to sleep, retaining the snapshot for the next wake instead of attempting recovery on sleeping displays.
- Validate DDC reply headers, status, and feature code before accepting brightness values; coordinate wake verification with MonitorControl and log per-target readback details.
- Protect new snapshots from wake-time zero readings: briefly re-read, then use a same-boot, same-display reliable record or the existing fallback instead of restoring and verifying an erroneous zero target. Preserve intentional minimum brightness outside the wake window and record the selected source in diagnostics.

### Validation — 2026-09-25

- Passed 30 Python tests, hardware-free DDC reply/wake snapshot checks, and LaunchAgent rendering checks. The new assertion-name regressions failed on the old parser and passed after the compatibility fix.
- Passed three local remote-control cycles on macOS 26.5.2 with UU Remote 4.41.2, a built-in display, and two Dell U2720QM displays: three connections, three restorations, and three display-sleep requests, without false disconnects or recurring flicker/permission dialogs. Post-wake brightness and gamma verification passed; this is single-setup acceptance, not a guarantee for other hardware or future UU versions.
- Updated Chinese/English documentation with compatibility details, upgrade risks, and the distinction between this unreleased branch and existing Release ZIPs.

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

[Unreleased]: https://github.com/zpmdd/uuremote-brightness-guard/compare/v1.0.3...HEAD
[1.0.3]: https://github.com/zpmdd/uuremote-brightness-guard/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/zpmdd/uuremote-brightness-guard/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/zpmdd/uuremote-brightness-guard/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/zpmdd/uuremote-brightness-guard/releases/tag/v1.0.0
