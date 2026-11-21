"""Match highlight reel builder.

Contract used by the tests
--------------------------
Frames are integer server frames at ``fps`` (default 30).  A clip id is
"<match_id>:<round>:<event_id>:<view>", so the same kill from another view
is a different clip.

* ``collect(match_id, event, match_time=0)``.  event keys: event_id, round,
  server_frame, client_time, round_start, round_end, kind ("kill"), killer,
  victim, view ("player"/"spectator"), cover_frame (optional).  The window
  is start = max(round_start, server_frame - pre_buffer) and end =
  min(round_end, server_frame + post_buffer) with pre_buffer=2 and
  post_buffer=3.  client_time is ignored.  The same kill (match, round,
  event_id, killer, victim, view) is collected once; a second collect
  returns {"clip_id", "created": False}.  A different view is a different
  clip.  cover_frame must lie in [start, end], else ValueError.  Returns
  {"clip_id", "created", "start", "end"}.
* ``cut(clip_id)`` requires a collected clip and returns {"clip_id",
  "status": "ready", "input_start", "input_end", "output_hash"};
  output_hash is sha256 of "<clip_id>:<start>:<end>".  Cutting twice returns
  the same result.
* ``publish(clip_id, now=0, ttl=60)`` requires status ready, else ValueError.
  Returns {"clip_id", "status": "published", "published_at", "expires_at",
  "share_token"}; publishing twice returns the same token and timestamps.
  share_token is "<clip_id>.<expires_at>.<sig>" where sig is the first 16
  hex chars of sha256("<clip_id>:<expires_at>:<secret>") with a secret fixed
  per instance.
* ``index(match_id=None)`` returns records sorted by (match_time,
  server_frame, clip_id), with keys clip_id, match_id, round, event_id,
  view, match_time, server_frame, start, end, audio_start, audio_end,
  cover_frame, cover_ms, status, published_at, expires_at.
  audio_start == start and audio_end == end.
* ``playback(clip_id, token=None, now=0)`` returns the record for a
  published, unexpired clip with a valid token.  A missing/invalid token
  raises PermissionError, an unpublished clip raises ValueError, and an
  expired clip raises PermissionError.
* ``expire(now)`` marks published clips with expires_at <= now as
  "expired", keeps their records, and returns the number expired.  Draft and
  ready clips are never removed.
* cover_ms is round(cover_frame * 1000 / fps).
"""


class MatchHighlightReel:
    def __init__(self, fps=30, pre_buffer=2, post_buffer=3):
        self.fps = fps
        self.pre_buffer = pre_buffer
        self.post_buffer = post_buffer

    def collect(self, match_id, event, match_time=0):
        raise NotImplementedError

    def cut(self, clip_id):
        raise NotImplementedError

    def publish(self, clip_id, now=0, ttl=60):
        raise NotImplementedError

    def expire(self, now):
        raise NotImplementedError

    def index(self, match_id=None):
        raise NotImplementedError

    def playback(self, clip_id, token=None, now=0):
        raise NotImplementedError
