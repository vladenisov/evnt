"""evnt: a self-hosted collector for the Snowplow tracker protocol."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("evnt")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"
