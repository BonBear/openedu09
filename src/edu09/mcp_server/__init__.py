"""MCP server for the EDU09 (architecture.md 6): ``edu09-mcp`` CLI, stdio and streamable HTTP.

Import the server API from :mod:`edu09.mcp_server.server` (``build_server``, ``ServerOptions``,
``run_stdio``, ``run_http``, ``start_http_in_thread``, ``HttpServerHandle``). This package
``__init__`` stays import-light so ``edu09`` does not pull in the MCP SDK.
"""
