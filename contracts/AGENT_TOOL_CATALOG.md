# Safe Agent Tool Catalog V1

The runtime and MCP adapter expose exactly these tools:

| Tool | Access | Domain effect |
| --- | --- | --- |
| `inspect_journey` | read-only | none |
| `inspect_twin` | read-only | projection read |
| `list_replans` | read-only | none |
| `search_city_services` | read-only | none |
| `request_replan` | automatic | creates PRESENTED proposal only |
| `accept_replan` | guarded | may activate an already verified proposal |

Payment, refund, C4 modification, policy mutation, arbitrary HTTP, SQL,
terminal, file, and browser tools are outside the allowlist.

