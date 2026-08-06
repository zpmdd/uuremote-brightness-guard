# Contributing

Thanks for helping improve UU Remote Brightness Guard.

## Before opening an issue

- Confirm that MonitorControl can control each external display through DDC/CI.
- Run `./status.sh` and review `~/Library/Logs/UURemoteBrightnessGuard`.
- Search existing issues for the same macOS, display, dock, or UU Remote version.
- Remove UU account details, remote device identifiers, IP addresses, and other private data from logs before posting them.

## Development workflow

1. Fork the repository and create a focused branch.
2. Keep display-changing behavior fail-safe: never delete a snapshot before restoration succeeds.
3. Add or update tests for Python state/session logic.
4. Run `make lint`, `make test`, and `make build` on an Apple Silicon Mac.
5. Test connect, disconnect, emergency restore, restart recovery, and uninstall with physical access to the Mac.
6. Open a pull request describing the display topology and versions used for validation.

Avoid committing compiled binaries, local logs, brightness snapshots, device identifiers, or account information.
