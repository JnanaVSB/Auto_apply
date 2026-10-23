"""Every kind of company job board the app reads, each with its reader file in app/sources/.

The rest of the app takes its list of kinds from here; see docs/adding-a-board-type.md.
"""

from app.sources import ashby, greenhouse, lever

BOARDS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
