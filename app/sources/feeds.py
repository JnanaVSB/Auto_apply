"""Every job feed the app reads: a whole-file list read as one source, like Simplify's lists, and never a board.

Each feed file gives LABEL, HOST and read(fetcher, roles), which returns jobs and how many entries were skipped.
"""

from app.sources import remoteok

FEEDS = {"remoteok": remoteok}
