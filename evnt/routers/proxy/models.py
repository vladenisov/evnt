from pydantic import AnyUrl, BaseModel, Field


class HashModel(BaseModel):
    """Request body for the proxy hash endpoint."""

    url: AnyUrl = Field(..., title="URL to hash")
