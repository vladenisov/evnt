from typing import Any, Self

from json_repair import repair_json
from pydantic import BaseModel, ValidationError


class Model(BaseModel):
    """Base model with enhanced JSON validation."""

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray | memoryview,
        *,
        strict: bool | None = None,
        context: Any = None,
        by_alias: bool | None = True,
        by_name: bool | None = False,
    ) -> Self:
        """
        Validate the given JSON data against the Pydantic model with auto-repair.

        Args:
            json_data: The JSON data to validate
            strict: Whether to enforce types strictly
            context: Extra variables to pass to the validator
            by_alias: Whether to use alias names for validation
            by_name: Whether field names should be matched by name

        Returns:
            The validated Pydantic model

        Raises:
            ValidationError: If the object could not be validated after repair
        """
        __tracebackhide__ = True
        kwargs = {
            "input": json_data,
            "strict": strict,
            "context": context,
            "by_alias": by_alias,
            "by_name": by_name,
        }

        try:
            return cls.__pydantic_validator__.validate_json(**kwargs)
        except ValidationError:
            if isinstance(json_data, str):
                json_str = json_data
            elif isinstance(json_data, (bytes, bytearray)):
                try:
                    json_str = json_data.decode("utf-8")
                except UnicodeDecodeError:
                    json_str = json_data.decode("utf-8", errors="ignore")
            elif isinstance(json_data, memoryview):
                raw = json_data.tobytes()
                try:
                    json_str = raw.decode("utf-8")
                except UnicodeDecodeError:
                    json_str = raw.decode("utf-8", errors="ignore")
            else:
                json_str = str(json_data)
            kwargs["input"] = repair_json(json_str)

        return cls.__pydantic_validator__.validate_json(**kwargs)
