"""Elastic APM client; imported only when ``elastic_apm.enabled`` is on."""

from elasticapm import Client
from elasticapm.contrib.starlette import make_apm_client

from evnt.config import settings


def create_elastic_apm_client() -> Client:
    """Build an Elastic APM client from the current settings."""
    elastic_config = settings.elastic_apm.model_dump()
    elastic_config.pop("enabled")
    elastic_config["SERVICE_NAME"] = settings.common.service_name

    client: Client = make_apm_client(elastic_config)
    return client
