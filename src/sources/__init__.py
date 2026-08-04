"""Job sources. Every source exposes `fetch(config, ...) -> list[Job]` and is
contractually forbidden from raising: network trouble is logged and skipped.
"""
