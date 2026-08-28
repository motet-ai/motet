# Troubleshooting Guide

Common problems and solutions for Motet development. Use this guide to quickly diagnose and resolve issues.

## Common Errors

### Command Not Executing

**Symptoms**: 
- Command queued but not executing
- Tasks stuck in "pending" state
- No worker activity

**Diagnosis**:
```bash
# Check worker status
motet-cli workers health

# Check Redis connection
redis-cli -u redis://localhost:6379/0 ping

# Check worker logs
motet-cli local logs

# Check task queue
celery -A motet.core.workers.celery_app inspect active
```

**Solutions**:
1. **Verify Redis Connection**: Ensure Redis is running and accessible
2. **Check Worker Health**: Verify workers are healthy and ready
3. **Verify Command Registration**: Ensure command is registered
4. **Check Worker Capabilities**: Verify workers have required capabilities
5. **Restart Workers**: `motet-cli workers restart`

### Worker Not Receiving Tasks

**Symptoms**: 
- Workers don't appear in worker status
- No tasks being processed
- Workers idle

**Diagnosis**:
```bash
# Check worker status via API
curl http://localhost:8000/api/v1/workers/readiness

# Check worker status
celery -A motet.core.workers.celery_app inspect stats
```

**Solutions**:
1. **Check Worker Health**: Verify workers are healthy
2. **Check Redis Connection**: Ensure Redis is accessible
3. **Verify Worker Capabilities**: Ensure workers match command requirements
4. **Check Worker Readiness**: Verify workers are in "ready" state
5. **Restart Workers**: `motet-cli workers restart`

### Login / SSO not working

**Symptoms**:
- "Login with SSO" does nothing or shows an error
- Redirect to Keycloak fails or returns an error page
- After Keycloak login, callback fails or "Invalid or expired authentication state"

**Diagnosis**:

1. **Check that the auth API is loaded** (if the API v1 routers failed to load, auth will 404):
   ```bash
   curl -s http://localhost:8000/api/v1/auth/check
   ```
   - If you get **404**: The auth router did not load. Check API container logs for `Failed to load API v1 router` and `auth` in `failed_routers`. Fix the cause (e.g. env/import error) and restart the API.
   - If you get **200**: Check the JSON body.

2. **Interpret `/api/v1/auth/check` response**:
   ```bash
   curl -s http://localhost:8000/api/v1/auth/check | jq .
   ```
   - `login_ready: true` → Config and Redis are OK; problem may be Keycloak realm/client or browser (e.g. redirect URI).
   - `login_ready: false` → Read the `errors` array. Common causes:
     - `MOTET_JWT_JWKS_URL is not set` → API env not set; ensure motet-api has JWT/Keycloak env in your `.env` file (see [Authentication Guide](../operations/authentication.md)).
     - `Keycloak config: ...` → Fix issuer/client/URL (e.g. `MOTET_JWT_ISSUER`, `MOTET_KEYCLOAK_PUBLIC_URL`).
     - `Redis: ...` → API cannot reach Redis; check `MOTET_REDIS_URL` and that `redis-tls` is healthy (`motet-cli local logs redis-tls`). A clean clone has no `tls/` material; `motet-cli local up` writes it before starting the proxy.

3. **Ensure Keycloak is running and reachable**:
   ```bash
   motet-cli local status
   curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/realms/motet
   ```
   - Keycloak should be up; realm URL should return 200 (or 401 if realm exists but unauthenticated).

4. **Verify redirect URI**  
   Keycloak client `motet-ai-stack` must have a valid redirect URI for your base URL, e.g. `http://localhost:8000/api/v1/auth/callback`. If you use a different host/port, add that callback URL in Keycloak (Realm → Clients → motet-ai-stack → Valid redirect URIs).

**Solutions**:
1. **Auth 404**: Fix API v1 router loading (see API logs), then restart motet-api.
2. **login_ready false**: Fix the issues listed in `errors` (env vars, Redis, Keycloak config).
3. **Keycloak unreachable**: Start Keycloak and wait for it to be healthy; ensure port 8080 is not in use by something else.
4. **Invalid or expired state**: Usually Redis or timeout; ensure API and `redis-tls` are running and OAuth state TTL (e.g. 600s) is not exceeded (log in again promptly).
5. **`redis-tls:6380` name or service not known**: The TLS proxy is not on the Compose network (often crash-looping because `tls/` was empty). Run `motet-cli local down` then `motet-cli local up`.
6. **Redirect mismatch**: Use the same base URL in the browser as in Keycloak redirect URIs (e.g. `http://localhost:8000` vs `http://127.0.0.1:8000`).

### MCP Server Not Found

**Symptoms**: 
- Tool execution fails with "MCP server not found"
- Tools not appearing in tool list
- MCP-related errors

**Diagnosis**:
```bash
# Check MCP configuration
cat config/mcp_instance_manager.yaml

# Verify MCP servers registered
curl http://localhost:8000/api/v1/mcp/servers
curl http://localhost:8000/api/v1/tools/mcp/discover

# Check MCP logs
motet-cli local logs

# Check MCP manager health (local compose maps 9091 → 9191)
curl http://localhost:9191/health
```

**Solutions**:
1. **Verify Configuration**: Check `mcp_instance_manager.yaml` syntax
2. **Check MCP Server Process**: Verify MCP servers are running (`GET /api/v1/mcp/servers`)
3. **Verify Transport**: Check stdio vs HTTP transport configuration
4. **Check Dependencies**: Ensure MCP server dependencies installed
5. **Restart MCP Manager**: Restart the `mcp-manager` service (not the workers) so YAML and process ownership reload

### Memory Not Storing

**Symptoms**: 
- Memory operations fail silently
- Memory not persisting
- Vector search not working

**Diagnosis**:
```bash
# Check vector backend health
curl http://localhost:8000/health/vector

# Check memory configuration
echo $MOTET_ENABLE_VECTOR_MEMORY
echo $MOTET_VECTOR_BACKEND

# Check Valkey (LTM vector store)
redis-cli -u $MOTET_REDIS_URL PING

# Check database connection (for migrate-pgvector CLI only)
psql $MOTET_PGVECTOR_DSN -c "SELECT 1"
```

**Solutions**:
1. **Enable Vector Memory**: Set `MOTET_ENABLE_VECTOR_MEMORY=true`
2. **Check Backend**: Verify Valkey is running (memory uses Valkey Search)
3. **Check Valkey**: Ensure Valkey/Redis is reachable for LTM vector index
4. **Check Permissions**: Verify write permissions for memory storage
5. **Check Logs**: Review memory operation logs for errors

### High Latency

**Symptoms**: 
- Commands taking too long
- Slow response times
- Timeout errors

**Diagnosis**:
```bash
# Check worker load
curl http://localhost:8000/metrics | grep worker_load

# Check command execution times
curl http://localhost:8000/metrics | grep command_duration

# Check worker count
motet-cli workers health
```

**Solutions**:
1. **Scale Workers**: `motet-cli local manage` (to add more worker instances)
2. **Check Network Latency**: Verify network connectivity
3. **Optimize Commands**: Profile and optimize slow commands
4. **Check Resource Usage**: Monitor CPU, memory, GPU usage
5. **Use Parallel Execution**: Use `motet.join()` for independent operations

### MCP Tools Hang / Time Out (Especially Across Conversations)

**Symptoms**:
- MCP tool calls appear to "hang" until the request timeout
- Works in one conversation but fails in another for the same user

**Likely Causes**:
- **Stream scoping mismatch** (proxy consuming user-scoped streams while client publishes to a different scope)
- MCP manager not restarted after YAML/code changes (old proxies still consuming the previous stream scope)

**Quick Checks**:
- Verify the service entry in `config/mcp_instance_manager.yaml` matches intended `visibility` and `lifecycle_duration`
- Restart the `mcp-manager` service so proxies load the latest code/config (workers do not own those processes)
- Use the MCP integration and configuration guides for `instance_key` and stream scoping rules

### Serialization Errors

**Symptoms**: 
- "Cannot serialize" errors
- Command execution fails
- Type errors

**Diagnosis**:
```bash
# Check command data types
# Ensure all data is JSON-serializable

# Check logs for serialization errors
motet-cli local logs | grep -i serializ
```

**Solutions**:
1. **Use Pydantic Models**: Ensure command data uses Pydantic models
2. **Check Data Types**: Verify all data is serializable
3. **Avoid Non-Serializable Types**: Don't use file handles, connections, etc.
4. **Check Context**: Ensure MotetContext is properly injected

### Worker Crashes

**Symptoms**: 
- Workers restarting frequently
- Worker health checks failing
- Memory errors

**Diagnosis**:
```bash
# Check recent worker logs
motet-cli local logs

# Check worker health and resource info
motet-cli workers health

# Check for OOM errors
dmesg | grep -i oom
```

**Solutions**:
1. **Increase Resources**: Allocate more memory/CPU to workers
2. **Check Memory Leaks**: Profile memory usage
3. **Reduce Concurrency**: Lower worker concurrency
4. **Check Dependencies**: Verify all dependencies installed
5. **Review Code**: Check for memory leaks in commands

### Authentication Failures

**Symptoms**: 
- 401 Unauthorized errors
- JWT validation failures
- Principal/tenant errors

**Diagnosis**:
```bash
# Check JWT configuration
echo $MOTET_JWT_JWKS_URL
echo $MOTET_JWT_ISSUER

# Test authentication
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/me

# Check logs
motet-cli local logs | grep -i auth
```

**Solutions**:
1. **Verify JWT Configuration**: Check JWKS URL and issuer
2. **Check Token Validity**: Ensure token is not expired
3. **Verify Claims**: Check JWT claims match configuration
4. **Check Keycloak**: If using Keycloak, verify it's running
5. **Review Security Settings**: Check multi-tenant mode settings

## Debug Checklist

### Service Status

```bash
# ✅ Check all services are running
motet-cli local status

# ✅ Check API health
curl http://localhost:8000/health

# ✅ Check Motet versions (API + workers + siblings; requires auth)
motet-cli version

# ✅ Check Redis
redis-cli ping

# ✅ Check Postgres
psql $MOTET_PGVECTOR_DSN -c "SELECT 1"
```

### Logs

```bash
# ✅ Stream all logs in real-time
motet-cli local logs

# ✅ Check worker health
motet-cli workers health
```

### Metrics

```bash
# ✅ Check metrics
curl http://localhost:8000/metrics

```

### Configuration

```bash
# ✅ Verify environment variables
env | grep MOTET_

# ✅ Check configuration file
cat config/mcp_instance_manager.yaml

# ✅ Test configuration
python -c "from motet.core.config import Config; c = Config(); print(c)"
```

### Connectivity

```bash
# ✅ Test Redis connection
redis-cli -u $MOTET_REDIS_URL ping

# ✅ Test database connection
psql $MOTET_PGVECTOR_DSN -c "SELECT 1"

# ✅ Test API connectivity
curl http://localhost:8000/health
```

## Common Patterns

### Pattern 1: Worker Not Ready

**Problem**: Workers not ready for tasks

**Solution**:
```bash
# Check worker readiness
curl http://localhost:8000/api/v1/workers/readiness | jq '.workers | to_entries[] | select(.value.state != "ready")'

# Wait for workers to be ready
# Workers warm up on startup
```

### Pattern 2: Command Timeout

**Problem**: Commands timing out

**Solution**:
```python
# Increase timeout
@motet.command(timeout_seconds=300)  # 5 minutes
def long_running_command(data: MyData, motet: MotetContext) -> Dict[str, Any]:
    ...
```

### Pattern 3: Memory Issues

**Problem**: High memory usage

**Solution**:
```bash
# Scale workers horizontally
motet-cli local manage

# Check worker health and memory info
motet-cli workers health
```

## Getting Help

### Before Asking for Help

1. ✅ Check this troubleshooting guide
2. ✅ Review relevant documentation sections
3. ✅ Check logs for error messages
4. ✅ Verify configuration
5. ✅ Test with minimal example

### Where to Get Help

- **GitHub Issues**: Report bugs and ask questions
- **Documentation**: Review relevant sections
- **Code Examples**: Check `motet-sdk/examples/bundles/`

### Providing Information

When asking for help, provide:

1. **Error Message**: Full error message and stack trace
2. **Configuration**: Relevant environment variables (sanitized)
3. **Logs**: Relevant log excerpts
4. **Steps to Reproduce**: Clear steps to reproduce issue
5. **Expected vs Actual**: What you expected vs what happened

## Next Steps

- **[Contributing Guide](./32-contributing-guide.md)** — feedback and pilots welcome at `hello@motet.dev`
- **[Project Structure](./33-project-structure.md)** - Understand codebase
- **[Resources & Links](./34-resources-links.md)** - Additional resources

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-08-27
