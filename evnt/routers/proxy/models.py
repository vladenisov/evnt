from pydantic import AnyUrl, BaseModel, Field


class HashModel(BaseModel):
    url: AnyUrl = Field(..., title="URL to hash")
