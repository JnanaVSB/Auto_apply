"""Every job feed the app reads: a whole-file list read as one source, like Simplify's lists, and never a board.

Each feed file gives LABEL, HOST (where its listings are), SOURCE (what its jobs are saved under) and
read(fetcher, keywords), which returns jobs, how many entries were skipped, and whether the list is whole: a list cut
short closes nothing.
"""

from app.sources import remoteok, usajobs

FEEDS = {"remoteok": remoteok, "usajobs": usajobs}
