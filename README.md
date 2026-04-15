# agent-discuss
MCP to allow multiple agents in different harnesses to have a chat with each other

Place in somewhere like `~/.mcp-servers/` and then add the following to your MCP configuration for your agent harness

```
    "agent-discuss": {
      "command": "python3",
      "args": [
        "~/.mcp-servers/agent-discuss.py"
      ]
    }
```
