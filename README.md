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

Then tell agent 1 

`Begin a discussion using agent-discuss, share your thoughts, tell me the session ID, then wait for a reply, continue in turn until a consensus is reached.`

Then tell agent 2 

`Join <session ID> via agent-discuss, share your thoughts, then wait for a reply, continue in turn until a consensus is reached.`
