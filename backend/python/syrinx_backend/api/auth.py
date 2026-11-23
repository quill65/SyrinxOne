from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Query, Request


def verify_token_dependency(expected_token: str | None):
    """Optional Bearer or ?token= dependency (required when SYRINX_API_TOKEN is configured)."""

    async def _check(request: Request, token: str | None = Query(default=None)) -> None:
        required = expected_token if expected_token is not None else os.environ.get("SYRINX_API_TOKEN")
        if not required:
            return
        auth_header = request.headers.get("Authorization", "")
        candidate = ""
        if auth_header.startswith("Bearer "):
            candidate = auth_header[7:].strip()
        elif token:
            candidate = token.strip()
        if not candidate or not secrets.compare_digest(candidate, required):
            raise HTTPException(status_code=401, detail="Invalid or missing API token")

    return _check
