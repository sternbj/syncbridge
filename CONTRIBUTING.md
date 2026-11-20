# Contributing to SyncBridge

Bug reports, ideas and pull requests are welcome.

- **Bugs and ideas:** use the menu bar icon › Help › Report a Bug… or Suggest an Idea…, or open an
  [issue](https://github.com/sternbj/syncbridge/issues/new/choose). Issues are public, so leave out
  private file names and personal data.
- **Pull requests:** fork the repo, create a branch, and open a pull request against `main`.

## Development

- App: Swift package in `app/` (macOS 13+). `cd app && swift build`
- Engine: `engine/syncbridge.py`, standard library only.
- Tests come first for engine changes: `cd engine && python3 -m unittest discover tests`
- Build and install the app: `scripts/install.sh`

Every pull request runs the engine tests (with the Python 3.9 that ships with macOS and with
Python 3.13) and builds the app on GitHub. A pull request is merged only when these checks pass.

Keep changes small and focused, and describe in the pull request what you tested.
