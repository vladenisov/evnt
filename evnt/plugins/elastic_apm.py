"""Elastic APM client integration.

The APM client is built lazily via :func:`create_elastic_apm_client` rather than
at module import time, so importing this module has no side effects (no config
reading or network/transport setup) until a caller explicitly opts in.
"""

from core.config import settings
from elasticapm.contrib.starlette import make_apm_client


def create_elastic_apm_client():
    """Build and return an Elastic APM client from the current settings.

    The Elastic APM configuration is read inside the function so the client is
    created lazily on call, never at import time.
    """
    elastic_config = settings.elastic_apm.model_dump()
    elastic_config.pop("enabled")
    elastic_config["SERVICE_NAME"] = settings.common.service_name

    return make_apm_client(elastic_config)
