import subprocess
import sys
import textwrap
from pathlib import Path


def test_cli_scripts_import_does_not_require_clickhouse_async_stack():
    repo_root = Path(__file__).resolve().parents[1]
    code = textwrap.dedent(
        """
        import builtins
        import sys

        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "aiohttp" or name.startswith("aiohttp."):
                raise AssertionError("aiohttp imported during CLI scripts setup")
            if name == "clickhouse_connect.driver.asyncclient":
                raise AssertionError("ClickHouse async client imported during CLI scripts setup")
            return original_import(name, *args, **kwargs)

        builtins.__import__ = guarded_import
        sys.path.insert(0, "evnt")

        import cli

        cli.CLI().scripts
        print("ok")
        """,
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout.strip() == "ok"
