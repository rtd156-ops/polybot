"""API clients.

Strict separation:
  - ``gamma`` and ``clob`` (read paths) are PUBLIC, no-auth data clients.
  - the private/authenticated trading client lives in ``polybot.execution.live``
    and is gated OFF by config. Data clients must never hold trading secrets.
"""
