# Hermes MCP Boundary

`agent-mcp/` is an adapter package. It can use the optional MCP SDK later,
but its tested core only calls the CityNexus Agent HTTP endpoint. It has no
database, ORM, shell, file, browser, or arbitrary URL dispatch capability.

The adapter discovers the same six tool names as the server catalog and passes
typed safe arguments to `/api/v1/agent/tools/{tool_name}`. A live Hermes or
MCP SDK installation is optional for this checkpoint.

