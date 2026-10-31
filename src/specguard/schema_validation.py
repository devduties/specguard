"""Create version-specific validators rooted at the complete API contract."""

from typing import Any

from openapi_schema_validator import (
    OAS30Validator,
    OAS30WriteValidator,
    OAS31Validator,
    OAS32Validator,
    oas30_format_checker,
    oas31_format_checker,
    oas32_format_checker,
)
from referencing import Registry


def create_schema_validator(document: dict[str, Any], *, request_body: bool = False) -> Any:
    version = document.get("openapi", "3.1.0")
    if version.startswith("3.0."):
        cls = OAS30WriteValidator if request_body else OAS30Validator
        checker = oas30_format_checker
    elif version.startswith("3.2."):
        cls, checker = OAS32Validator, oas32_format_checker
    else:
        cls, checker = OAS31Validator, oas31_format_checker
    return cls(document, registry=Registry(), format_checker=checker)
