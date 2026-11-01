# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/ai/src/utils/validation.ts
"""JSON schema boundary for static, Skill and MCP tool parameters."""

import math
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing.exceptions import Unresolvable

from ..kernel import ToolError, plain_data


class JsonSchemaValidator:
    def check_schema(self, schema: dict[str, Any]) -> None:
        schema = plain_data(schema)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise ToolError("invalid_tool_schema", error.message) from error
        pending = [schema]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key in ("$ref", "$dynamicRef"):
                    if key in item and not item[key].startswith("#"):
                        raise ToolError(
                            "invalid_tool_schema", "External schema references are unavailable"
                        )
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)

    # Pi: Value.Convert inside validateToolArguments, limited to top-level string properties.
    def convert(self, schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        properties = schema.get("properties", {})
        return {
            name: convert_text(value, properties[name].get("type"))
            if isinstance(value, str) and name in properties
            else value
            for name, value in arguments.items()
        }

    def validate(self, schema: dict[str, Any], arguments: dict[str, Any]) -> None:
        try:
            Draft202012Validator(plain_data(schema)).validate(arguments)
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path)
            raise ToolError(
                "invalid_arguments", f"{location or 'arguments'}: {error.message}"
            ) from error
        except (Unresolvable, RecursionError) as error:
            raise ToolError("invalid_tool_schema", "Cannot resolve tool input schema") from error


def convert_text(value: str, kind: Any) -> Any:
    if kind == "boolean" and value in {"true", "false"}:
        return value == "true"
    if kind not in {"integer", "number"}:
        return value
    try:
        return int(value)
    except ValueError:
        pass
    if kind == "integer":
        return value
    try:
        number = float(value)
    except ValueError:
        return value
    return number if math.isfinite(number) else value
