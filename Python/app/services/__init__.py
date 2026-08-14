"""Use-case / business-logic layer.

Services orchestrate repositories, storage, and AI providers. They raise domain
errors from :mod:`app.core.exceptions` and know nothing about HTTP, so the same
code is callable from an endpoint, a CLI, or a scheduled job.
"""
