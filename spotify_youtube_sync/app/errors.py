"""Explicit error hierarchy.

The distinction that matters most for safety: `SourceStateError` (and its
subclasses) means "we are not sure what Spotify wants" and MUST prevent any
destructive change to the YouTube playlist.
"""

from __future__ import annotations


class SyncError(Exception):
    """Base class for all errors raised by this add-on."""


class ConfigError(SyncError):
    """Invalid or missing configuration."""


class AuthError(SyncError):
    """OAuth token missing, expired-and-unrefreshable, or revoked."""


class SpotifyAuthError(AuthError):
    pass


class YouTubeAuthError(AuthError):
    pass


class ApiUnavailableError(SyncError):
    """A remote API was unreachable or returned 5xx after retries."""


class SpotifyUnavailableError(ApiUnavailableError):
    pass


class YouTubeUnavailableError(ApiUnavailableError):
    pass


class RateLimitedError(SyncError):
    """HTTP 429. Carries an optional retry-after hint (seconds)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class QuotaExceededError(SyncError):
    """A hard API quota was hit (YouTube dailyLimitExceeded / Spotify budget).

    Not an error condition to panic over: the sync stops cleanly, persists
    progress, and resumes on the next scheduled run.
    """


class SourceStateError(SyncError):
    """We could not obtain a trustworthy, complete view of the Spotify source.

    While this is raised, the sync engine will NOT remove anything from YouTube.
    """


class IncompleteSpotifyFetchError(SourceStateError):
    """Pagination did not return every track the playlist claims to contain."""


class PlaylistUnavailableError(SourceStateError):
    """Playlist deleted, private, or not accessible to the authorized user."""


class MalformedResponseError(SyncError):
    """An API response did not have the shape we require."""


class DatabaseError(SyncError):
    pass
