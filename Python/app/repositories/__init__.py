"""Data-access layer.

**Transaction convention:** repository functions ``flush()`` (so generated ids
and defaults are available) but never ``commit()``. The caller - an endpoint via
``get_db``, or a service via ``session_scope`` - owns the transaction boundary.
That way several repository calls can be composed into one atomic unit of work,
which is impossible if each one commits on its own.
"""
