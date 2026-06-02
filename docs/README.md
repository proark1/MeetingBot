# JustHereToListen.io — Developer Documentation

Three short pages, one per integration surface.

| Surface | Doc | When to use |
|---|---|---|
| HTTP / REST | [API.md](./API.md) | Any language, any platform — the lowest common denominator |
| MCP server | [MCP.md](./MCP.md) | Claude Desktop, Cursor, Cline, or any MCP-compatible AI assistant |
| Official SDKs | [SDKs.md](./SDKs.md) | Python or TypeScript/JavaScript apps |

The full machine-readable contract lives at:

- **Swagger UI** — [`/api/docs`](http://localhost:8000/api/docs) (public endpoints)
- **ReDoc** — [`/api/redoc`](http://localhost:8000/api/redoc)
- **Admin schema** — [`/api/v1/admin/docs`](http://localhost:8000/api/v1/admin/docs) (admin accounts only)
- **OpenAPI snapshot** — [`api/openapi.json`](../api/openapi.json) — 117 public + 136 admin operations, every operation has summary, description, tags, request example, and a 2xx response example

If you only have five minutes, read [API.md → Quickstart](./API.md#quickstart-cURL).
