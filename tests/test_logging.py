from __future__ import annotations

import json
import logging

from app.core.logging import JsonFormatter, request_id_ctx


def test_json_formatter_includes_request_id_and_extra_fields():
    token = request_id_ctx.set("req-42")
    try:
        record = logging.LogRecord(
            "app.test", logging.INFO, __file__, 1, "hola %s", ("mundo",), None
        )
        record.sync_run_id = 7
        data = json.loads(JsonFormatter().format(record))
    finally:
        request_id_ctx.reset(token)

    assert data["msg"] == "hola mundo"
    assert data["level"] == "INFO"
    assert data["request_id"] == "req-42"
    assert data["sync_run_id"] == 7
