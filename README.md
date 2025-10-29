# KBase SDK base client

This repo contains the base client code that all other SDK clients depend on. It's generally
expected that users don't directly interact with it, but it can be useful for cases where
a compiled client isn't available.

## Installation

```
pip install kbase-sdk-baseclient
```

## Usage

The client is not intended for general use, and power users should be easily able to inspect the
code and tests to determine proper useage.

## Development

### Adding and releasing code

* Adding code
  * All code additions and updates must be made as pull requests directed at the develop branch.
    * All tests must pass and all new code must be covered by tests.
    * All new code must be documented appropriately
      * Pydocs
      * General documentation if appropriate
      * Release notes
* Releases
  * The main branch is the stable branch. Releases are made from the develop branch to the main
    branch.
  * Update the version in `sdk_baseclient.py` and `pyproject.toml`.
  * Tag the version in git and github.
  * Create a github release.

### Testing

Copy `test.cfg.example` to `test.cfg` and fill it in appropriately.

```
uv sync --dev  # only required on first run or when the uv.lock file changes
PYTHONPATH=src uv run pytest test
```
