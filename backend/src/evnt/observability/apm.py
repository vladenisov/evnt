"""Elastic APM client; imported only when ``elastic_apm.enabled`` is on."""

from elasticapm import Client
from elasticapm.contrib.starlette import make_apm_client

from evnt.config import settings


def create_elastic_apm_client() -> Client:
    """Build an Elastic APM client from the current settings."""
    apm = settings.elastic_apm
    # elasticapm reads upper-case keys only. An unset server URL is left out so
    # the agent's own default (or ELASTIC_APM_SERVER_URL) applies.
    elastic_config: dict[str, str] = {
        "SERVICE_NAME": apm.service_name or settings.common.service_name,
    }
    if apm.server_url is not None:
        elastic_config["SERVER_URL"] = apm.server_url

    client: Client = make_apm_client(elastic_config)
    return client
