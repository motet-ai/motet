# Quick Start Guide

Get up and running with Motet in 30 minutes. This guide will help you install Motet, run your first command, and verify your setup.

## Prerequisites

Before you begin, ensure you have:

- **Python 3.11+** installed
- **Docker** and **Docker Compose** installed
- **Git** installed
- Basic familiarity with terminal/command line
- **At least one model provider API key** — chat and agents will not get a model reply without it

## Model API key

Put the key in a `.env` file in the **repo root before** `motet-cli local up`. Docker Compose reads that file and passes the value into the API and workers.

The stack default is OpenAI `gpt-4o-mini`:

```bash
OPENAI_API_KEY=your-openai-key-here
```

Add other provider keys only for models you will select. Names and aliases are on [Supported models](./03a-supported-models.md) and [Configuration Reference](./29-configuration-reference.md#model-configuration).

## Install and start the stack

Docker is required. `motet-cli local` starts the distributed Compose stack:

```bash
# Clone the repository
git clone https://github.com/motet-ai/motet.git
cd motet

# Install the project and SDK in editable mode so motet-cli is available and uses repo code (recommended for local dev)
python -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install -e motet-sdk
pip install -e .

# Start the distributed stack (pulls published images; add --build to rebuild from this tree)
docker login ghcr.io   # eval / invite-only snapshot
motet-cli local up

# Check stack status + worker readiness summary
motet-cli local status

# Optional diagnostics
motet-cli local doctor

# Open local management UI
motet-cli local manage
```

This starts:
- **API Server**: FastAPI application (http://localhost:8000)
- **Workers**: Distributed task execution workers
- **Redis**: Message broker and state storage
- **Postgres**: Database with pgvector extension

The stack sets Redis, Postgres, and the rest of the runtime configuration. Model calls still use the key from [Model API key](#model-api-key).

Starting Redis, Postgres, Celery, and the API as host processes — without Compose — is unsupported. Use `motet-cli local up`.

## Running Your First Command

### Using the API

Once your services are running, you can interact with Motet via the API:

```bash
# Health check
curl http://localhost:8000/health

# Simple chat (streamed response via SSE)
curl -N -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "Hello! Can you help me with a task?"}
    ]
  }'
```

### Using Python

Create a simple Python script (`test_chat.py`):

```python
import asyncio
from motet.core import MotetStack, Message

async def main():
    stack = MotetStack()
    reply = await stack.chat([
        Message(role="user", content="Hello! Can you help me with a task?")
    ])
    print(reply.content)

if __name__ == "__main__":
    asyncio.run(main())
```

Run it:
```bash
python test_chat.py
```

### Using the CLI

```bash
# Direct message
motet-cli chat --message "Hello! Can you help me?"

# Stream the response in real time
motet-cli chat --message "Hello!" --stream
```

## Basic "Hello World" Example

Let's create a simple distributed command to understand how Motet works:

### Step 1: Create Command File

Create `my_first_command.py`:

```python
from motet_sdk import motet, MotetContext, BaseCommandData
from pydantic import Field
from typing import Dict, Any

class HelloWorldData(BaseCommandData):
    """Data model for hello world command."""
    name: str = Field(..., description="Name to greet")

@motet.command()
def hello_world(data: HelloWorldData, motet: MotetContext) -> Dict[str, Any]:
    """
    Simple hello world command.
    
    Args:
        data: HelloWorldData with name parameter
        motet: MotetContext for resource access
    
    Returns:
        Dict with greeting message
    """
    return {
        "message": f"Hello, {data.name}! Welcome to Motet!",
        "command_id": motet.command_id,
        "task_id": motet.task_id
    }
```

> **Note:** When this module is loaded by the Motet runtime (for example from a bundle command directory), `@motet.command()` registers the command automatically.  
> Manual registration is only needed in custom local harnesses or isolated tests where functions are imported directly without runtime loading.

### Step 2: Execute the Command

```python
from motet.core.commands.decorator import MotetContext
from my_first_command import hello_world, HelloWorldData

# Create motet context (in real usage, this is injected by decorator)
motet = MotetContext(...)

# Execute the command
result = motet.do(hello_world, data=HelloWorldData(name="Developer"))

print(result["message"])  # "Hello, Developer! Welcome to Motet!"
```

## Verifying Your Setup

### 1. Check API Health

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "healthy",
  "components": {
    "memory": "ok",
    "vector": "ok",
    "orchestrator": "ok"
  }
}
```

### 2. Check Worker Status

```bash
motet-cli workers readiness
```

### 3. Check Metrics

```bash
# Metrics endpoint
curl http://localhost:8000/metrics
```

### 4. Test Distributed Execution

```bash
motet-cli chat --message "What is 2 + 2?"
```

If you see a streamed response, the full distributed stack (API, workers, Redis) is operational.

You can also open the management UI directly:

```bash
motet-cli local manage --wait
```

## Common First-Time Issues and Solutions

### Issue 1: motet-cli: command not found

**Error**: `zsh: command not found: motet-cli` (or `bash: motet-cli: command not found`)

**Cause**: The `motet-cli` command is provided by this project and is only available after installing the package.

**Solution**:
```bash
# From the project root (imf/)
python -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install -e motet-sdk
pip install -e .

# Then run
docker login ghcr.io   # eval / invite-only snapshot
motet-cli local up
```

Use `motet-cli local up --build` when you are changing Motet image contents.

### Issue 2: Sign in with SSO fails on redis-tls

**Error**: `Login initiation failed: Error -2 connecting to redis-tls:6380. Name or service not known`

The API stores SSO state in Redis through the `redis-tls` proxy. A clean clone
does not include `tls/` (it is gitignored). If that proxy is not running,
`redis-tls` does not resolve on the Compose network.

**Solution**:
```bash
motet-cli local status
motet-cli local logs redis-tls

# local up writes tls/ when it is missing, then starts the proxy
motet-cli local down
motet-cli local up
```

Do not start a standalone `redis` container on the host. Compose already runs
Valkey plus the TLS proxy; the API hostname is `redis-tls`, not `localhost`.

### Issue 3: Workers Not Starting

**Error**: Workers don't appear in status

**Solution**:
```bash
# Check worker logs
motet-cli local logs --follow

# Restart workers
motet-cli local restart

# Check worker app configuration
# Ensure MOTET_REDIS_URL is set correctly
```

### Issue 4: MCP Servers Not Found

**Error**: `MCP server not found` or tool execution fails

**Solution**:
```bash
# Check MCP configuration
cat config/mcp_instance_manager.yaml

# Verify MCP servers are registered
curl http://localhost:8000/api/v1/mcp/servers
curl http://localhost:9191/health

# Check stack logs
motet-cli local logs --follow
```

### Issue 5: Import Errors

**Error**: `ModuleNotFoundError` or import issues

**Solution**:
```bash
# Ensure you're in the virtual environment
source venv/bin/activate

# Reinstall dependencies
pip install -r requirements.txt

# Check Python path
python -c "import sys; print(sys.path)"
```

### Issue 6: Port Conflicts

**Error**: Port already in use

**Solution**:
```bash
# Find process using port
lsof -i :8000  # For API
lsof -i :6379  # For Redis
lsof -i :5432  # For Postgres

# Kill process or change the port via motet-cli local manage
```

## Next Steps

Now that you have Motet running:

1. **Try the Chat Explorer**: Run [Chat Explorer](./36-chat-explorer.md) to see the full framework in action with a real UI
2. **Point an OpenAI client at Motet**: See [OpenAI-Compatible API](./28-api-reference.md#openai-compatible-api) (enable with `MOTET_OPENAI_COMPAT_ENABLED=true`)
3. **Understand the Fundamentals**: Read [Core Concepts Overview](./05-core-concepts-overview.md)
4. **Learn the Philosophy**: Study [Design Principles](./06-design-principles.md)
5. **Build Your First Command**: Follow [Building Your First Command](./15-building-your-first-command.md)
6. **Try fast bundle iteration**: Use `motet-cli bundle hot-deploy .` (Mutagen sync) while developing bundles

## Additional Resources

- **API Documentation**: http://localhost:8000/redoc (ReDoc)
- **Project README**: See main `README.md` for more details

## Getting Help

If you encounter issues:

1. **Check Logs**: Review container logs for errors
2. **Verify Configuration**: Ensure environment variables are set correctly
3. **Review Documentation**: Check relevant sections in this guide
4. **Community Support**: Reach out via GitHub Issues

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-08-26

**Congratulations!** You've successfully set up Motet. Continue to [Core Concepts Overview](./05-core-concepts-overview.md) to understand how Motet works.
