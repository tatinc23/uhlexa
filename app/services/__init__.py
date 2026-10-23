"""Business logic. Services take a `Session` and the acting `Principal`, scope every query to the principal's
household, `flush()`, and leave the commit to the request handler, MCP tool or job."""
