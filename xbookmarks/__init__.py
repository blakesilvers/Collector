"""Collect your own X (Twitter) bookmarks with your own logged-in browser session.

No X API key and no paid service: a real Chromium window (Playwright) opens
x.com/i/bookmarks the way you would, and the JSON that X's page loads is captured,
stored in SQLite and written out as a daily report of bookmarks not seen before.
"""
__version__ = "1.0.0"
