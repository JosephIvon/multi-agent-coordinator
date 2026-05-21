__all__: list[str] = []


def __getattr__(name: str):
    if name != "create_app":
        raise AttributeError(name)
    try:
        from mac.transport.http_ws import create_app
    except ModuleNotFoundError as exc:
        if exc.name == "fastapi":
            raise ModuleNotFoundError(
                "mac.transport.create_app requires the HTTP extra. "
                'Install with: pip install "mac-agent[http]"'
            ) from exc
        raise
    return create_app
