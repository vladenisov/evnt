"""Static assets that may be downloaded after the application starts."""

from fastapi.staticfiles import StaticFiles


class DeferredStaticFiles(StaticFiles):
    async def check_config(self) -> None:
        """Let file lookup return 404 until the optional directory exists.

        ``check_dir=False`` only disables Starlette's constructor check; its
        first-request check still raises for a missing directory. Lookup
        already handles missing files and confines paths to the asset root.
        """
