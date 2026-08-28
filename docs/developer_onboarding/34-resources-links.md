# Resources & Links

Additional learning resources and helpful links for Motet development.

## Documentation

### Core Documentation

- **Main README**: [README.md](../../README.md) - Project overview and getting started
- **Command Development**: [Building Your First Command](./15-building-your-first-command.md), [Distributed Command System](./07-distributed-command-system.md), [Command Composition](./16-command-composition-patterns.md) - Command development and patterns
- **Supported Models**: [Supported Models](./03a-supported-models.md) - Providers, flagship ids, and the live catalog

### Quickstarts

- **Quickstart Script**: [Zero to Demo](../quickstarts/zero_to_demo.sh)
- **JWT/JWKS Quickstart**: [JWT JWKS Quickstart](../quickstarts/jwt_jwks_quickstart.md)
- **CLI Reference**: [Motet CLI Reference](./37-motet-cli-reference.md)

## External Resources

### Core Technologies

- **FastAPI Documentation**: https://fastapi.tiangolo.com/ - Web framework
- **Pydantic Documentation**: https://docs.pydantic.dev/ - Data validation
- **Redis Documentation**: https://redis.io/docs/ - In-memory data store

### Related Technologies

- **Playwright**: https://playwright.dev/ - Browser automation
- **PostgreSQL**: https://www.postgresql.org/docs/ - Database
- **pgvector**: https://github.com/pgvector/pgvector - Vector extension
- **Prometheus**: https://prometheus.io/docs/ - Metrics
- **Grafana**: https://grafana.com/docs/ - Visualization

### Standards and Protocols

- **MCP (Model Context Protocol)**: https://modelcontextprotocol.io/ - Tool integration protocol
- **JWT (JSON Web Tokens)**: https://jwt.io/ - Authentication tokens
- **OAuth 2.0**: https://oauth.net/2/ - Authorization framework

## Community Resources

### Getting Help

- **GitHub Issues**: https://github.com/motet-ai/motet/issues - Report bugs and ask questions
- **GitHub Discussions**: https://github.com/motet-ai/motet/discussions - Technical discussions

### Contributing

- **Contributing Guide**: [Contributing Guide](./32-contributing-guide.md) — feedback and pilots welcome at `hello@motet.dev`; no unsolicited PRs
- **Code of Conduct**: See repository for code of conduct
- **Issue Templates**: Use GitHub issue templates for bugs and features

## Learning Resources

### Distributed Systems

- **Redis Patterns**: https://redis.io/docs/manual/patterns/
- **Distributed Systems Concepts**: General distributed systems knowledge

### Python Development

- **Python Type Hints**: https://docs.python.org/3/library/typing.html
- **Pydantic Models**: https://docs.pydantic.dev/latest/
- **Async/Await**: https://docs.python.org/3/library/asyncio.html

### Testing

- **pytest Documentation**: https://docs.pytest.org/
- **Testing Best Practices**: See [Testing Strategies](./18-testing-strategies.md)

## Development Tools

### Recommended Tools

- **VS Code**: With Python extension
- **PyCharm**: Professional Python IDE
- **Docker Desktop**: For local development
- **Postman/Insomnia**: For API testing

### Useful Extensions

- **Python**: Language support
- **Pylance**: Type checking
- **Black Formatter**: Code formatting
- **isort**: Import sorting

## API Documentation

### Interactive API Docs

- **ReDoc**: http://localhost:8000/redoc - OpenAPI reference (also the manage API page)

### API Endpoints

- **Health**: `GET /health`
- **Stack versions**: `GET /api/v1/version` (authenticated; API + workers + siblings)
- **Metrics**: `GET /metrics`
- **API Docs**: `GET /redoc`

## Monitoring and Observability

### Local Development

- **Task Flow**: http://localhost:8000/manage - Debug visualization

## Examples and Tutorials

### Code Examples

- **Example Bundles**: `motet-sdk/examples/bundles/` — see [Example Bundles](./26-example-bundles.md)
- **Workflow Examples**: See [Building Workflows](./17-building-workflows.md)

### Tutorials

- **Building Your First Command**: [Building Your First Command](./15-building-your-first-command.md)
- **Building Workflows**: [Building Workflows](./17-building-workflows.md)
- **Command Composition**: [Command Composition Patterns](./16-command-composition-patterns.md)

## Getting Help

### Before Asking

1. ✅ Check this documentation
2. ✅ Search GitHub issues
3. ✅ Review relevant architecture docs (internal)
4. ✅ Check code examples
5. ✅ Review logs and error messages

### When Asking for Help

Provide:
- **Error Message**: Full error and stack trace
- **Configuration**: Relevant env vars (sanitized)
- **Logs**: Relevant log excerpts
- **Steps to Reproduce**: Clear reproduction steps
- **Expected vs Actual**: What you expected vs what happened

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

- **[Quick Start Guide](./04-quick-start-guide.md)** - Get started quickly

---

**Last Updated**: 2026-08-27

**Welcome to Motet!** We're excited to have you here. 🚀

If you have questions or need help, don't hesitate to reach out through GitHub Issues or the community.
