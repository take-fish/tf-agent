from app.tools import db_query, deploy, health, kb, logs, pipeline
from app.tools.registry import register

_SPECS = (
    logs.spec,
    health.spec,
    db_query.spec,
    pipeline.spec,
    deploy.spec_trigger,
    deploy.spec_restart,
    kb.spec,
)

_REGISTERED = False


def register_all() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    for spec in _SPECS:
        register(spec)
    _REGISTERED = True
