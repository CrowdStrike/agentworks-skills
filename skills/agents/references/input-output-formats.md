# Agent Input/Output Formats

Agents support structured input validation and output formatting:

## Input Formats
- **`unstructured`** (default): Free-form text or JSON input, no schema validation
- **`json_with_schema`**: JSON input validated against a JSON Schema

## Output Formats
- **`unstructured`** (default): Free-form text response
- **`html`**: HTML-formatted response
- **`markdown`**: Markdown-formatted response
- **`json`**: JSON-formatted response (no schema enforcement)
- **`json_with_schema`**: JSON response validated against a JSON Schema

## Examples

**Add input validation:**
```bash
../../scripts/python.sh scripts/agent_upsert.py \
  --id <agent-id> \
  --input-format json_with_schema \
  --input-schema '{
    "type": "object",
    "properties": {
      "prompt": {"type": "string", "description": "User question"},
      "severity": {"type": "string", "enum": ["low", "medium", "high"]}
    },
    "required": ["prompt"]
  }'
```

**Request structured JSON output:**
```bash
../../scripts/python.sh scripts/agent_upsert.py \
  --id <agent-id> \
  --output-format json_with_schema \
  --output-schema '{
    "type": "object",
    "properties": {
      "risk_score": {"type": "number"},
      "recommendations": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["risk_score"]
  }'
```

Before writing an `enum` into a schema, check the tenant's real values. See the Common Pitfalls table in `../SKILL.md`.
