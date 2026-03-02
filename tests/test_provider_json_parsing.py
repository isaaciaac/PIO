import pytest
from pydantic import BaseModel

from vibe.providers.base import _parse_json_to_schema


class DemoPack(BaseModel):
    summary: str
    items: list[int]


def test_parse_json_to_schema_allows_trailing_text():
    text = '{ "summary": "ok", "items": [1,2,3] }\n\nextra commentary'
    parsed = _parse_json_to_schema(text, schema=DemoPack)
    assert parsed.summary == "ok"
    assert parsed.items == [1, 2, 3]


def test_parse_json_to_schema_prefers_fenced_json_block():
    text = "hello\n```json\n{ \"summary\": \"ok\", \"items\": [1] }\n```\nbye"
    parsed = _parse_json_to_schema(text, schema=DemoPack)
    assert parsed.items == [1]


def test_parse_json_to_schema_unwraps_schema_envelope():
    text = '{ "DemoPack": { "summary": "ok", "items": [9] } } trailing'
    parsed = _parse_json_to_schema(text, schema=DemoPack)
    assert parsed.items == [9]


def test_parse_json_to_schema_ignores_first_json_that_fails_validation():
    # First JSON is valid JSON but doesn't match schema; second does.
    text = '{ "not": "schema" }\n{ "summary": "ok", "items": [7] }'
    parsed = _parse_json_to_schema(text, schema=DemoPack)
    assert parsed.summary == "ok"
    assert parsed.items == [7]


def test_parse_json_to_schema_raises_when_no_json():
    with pytest.raises(Exception):
        _parse_json_to_schema("no json here", schema=DemoPack)


class DemoCodeChange(BaseModel):
    kind: str
    summary: str
    writes: list[dict] = []


def test_parse_json_to_schema_coerces_single_write_to_codechange():
    text = '{ "path": "src/x.txt", "content": "hi" }'
    from vibe.schemas import packs

    parsed = _parse_json_to_schema(text, schema=packs.CodeChange)
    assert parsed.kind == "patch"
    assert "src/x.txt" in parsed.summary
    assert parsed.writes and parsed.writes[0].path == "src/x.txt"
