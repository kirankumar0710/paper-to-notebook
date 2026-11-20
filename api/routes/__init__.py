"""API routes, one module per dashboard panel.

- ``dashboard``: ``GET /``, the page that assembles the panels.
- ``config``: panel 1 (Configuration), plus ``/health``.
- ``convert``: panels 2-4 (Upload, LLM output, Metrics), which all read the
  same ``/convert/stream`` events.
- ``download``: panel 5 (Download).
"""
