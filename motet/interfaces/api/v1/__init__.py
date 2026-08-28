"""API v1 endpoints."""
from .commands import router as commands_router
from .schedules import router as schedules_router
from .vault import router as vault_router
from .debug import router as debug_router
from .workers import router as workers_router
from .workflows import router as workflows_router
from .tools import router as tools_router
from .memories import router as memories_router
from .conversations import router as conversations_router
from .models import router as models_router
from .chat import router as chat_router
from .service_accounts import router as service_accounts_router
from .events import router as events_router
from .identity import router as identity_router
from .oauth import router as oauth_router
from .auth import router as auth_router
from .artifacts import router as artifacts_router
from .cost import router as cost_router
from .developer_docs import router as developer_docs_router
from .deploy import router as deploy_router
from .agents import router as agents_router
from .devices import router as devices_router
from .image_stacks import router as image_stacks_router
from .workspace_containers import router as workspace_containers_router
from .skills import router as skills_router
from .tenants import router as tenants_router
from .surfaces import router as surfaces_router
from .tasks import router as tasks_router
from .mcp import router as mcp_router
from .version import router as version_router

__all__ = [
    "commands_router",
    "schedules_router",
    "vault_router",
    "debug_router",
    "workers_router",
    "workflows_router",
    "tools_router",
    "memories_router",
    "conversations_router",
    "models_router",
    "chat_router",
    "service_accounts_router",
    "events_router",
    "identity_router",
    "oauth_router",
    "auth_router",
    "artifacts_router",
    "cost_router",
    "developer_docs_router",
    "deploy_router",
    "agents_router",
    "devices_router",
    "image_stacks_router",
    "workspace_containers_router",
    "skills_router",
    "tenants_router",
    "surfaces_router",
    "tasks_router",
    "mcp_router",
    "version_router",
]
