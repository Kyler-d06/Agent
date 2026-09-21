# Universal Harness MCP connection

The harness exposes its live tool catalog over a local MCP stdio server. Start the
Universal Assistant first; the MCP client then launches the bridge itself.

Use this MCP server entry in a client that accepts the common `mcpServers` format:

```json
{
  "mcpServers": {
    "universal-assistant": {
      "command": "C:\\Users\\kyler\\OneDrive\\Documents\\Scripts\\Code\\Agent\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\kyler\\OneDrive\\Documents\\Scripts\\Code\\Agent\\mcp_bridge.py"
      ]
    }
  }
}
```

No key needs to be pasted into that file. The bridge loads
`%LOCALAPPDATA%\UniversalAssistant\managed-secrets.json` and derives a scoped MCP
actor key in memory. To point at a non-default deployment, set `ASSISTANT_DATA_DIR`
and `CORE_URL` in the MCP server environment.

The bridge intentionally exposes the same permissions as the harness tool gateway:
read-only tools can run directly, while edits, execution, external writes, and other
effects still require the applicable operator grant. Diagnostics go to stderr;
stdout remains MCP protocol only.

## Claude Desktop

The easiest setup is in Command Center: open **Trading**, find **Claude through
MCP**, and select **Add Claude JSON automatically**. The owner-only action merges
the entry below, preserves other servers/settings, makes a timestamped backup
when changing an existing file, and refuses invalid JSON. Fully quit and reopen
Claude Desktop afterward.

Claude Desktop can launch the local stdio bridge directly. Put the JSON above in
`%APPDATA%\Claude\claude_desktop_config.json`, fully quit and reopen Claude, and
enable the `universal-assistant` tools in the conversation. Claude becomes an MCP
client of the harness: it can inspect or operate the harness within its grants.

This connection does **not** make Claude's subscription model callable by Ollama.
MCP connects models to tools; Claude Desktop is not a model-serving MCP endpoint.

## Authenticated HTTP transport (ChatGPT-compatible boundary)

Start a loopback Streamable HTTP server:

```powershell
Set-Location "C:\Users\kyler\OneDrive\Documents\Scripts\Code\Agent"
.\.venv\Scripts\python.exe .\mcp_http_bridge.py
```

It listens at `http://127.0.0.1:5078/mcp` and requires a transport-only bearer
token. Display that token only when configuring a trusted client:

```powershell
.\.venv\Scripts\python.exe .\mcp_http_bridge.py --print-token
```

ChatGPT-hosted connectors cannot reach `127.0.0.1`. They require a stable public
HTTPS URL terminating at this server plus the bearer token (or a full OAuth proxy,
if required by the account/workspace). Creating a tunnel is intentionally an
operator action because it exposes a network endpoint. Do not expose port 5077;
only proxy the authenticated port 5078 `/mcp` endpoint.

In ChatGPT, add the resulting HTTPS `/mcp` URL as a custom MCP connector and use
the transport token as its bearer credential when that option is available. The
harness permission gateway remains authoritative behind the connector.

As with Claude, this lets ChatGPT call harness tools. It does not provide a free
or subscription-backed ChatGPT completion endpoint for the local model. Automated
remote reasoning requires an explicitly supported API/provider route with its own
terms, credentials, quotas, and cost policy.
