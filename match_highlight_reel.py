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
import hashlib
import threading
import uuid


class MatchHighlightReel:
    def __init__(self, fps=30, pre_buffer=2, post_buffer=3):
        self.fps = fps
        self.pre_buffer = pre_buffer
        self.post_buffer = post_buffer
        self._secret = uuid.uuid4().hex
        self._clips = {}
        self._dedupe = {}
        self._match_locks = {}
        self._match_locks_guard = threading.Lock()

    def collect(self, match_id, event, match_time=0):
        key = (
            match_id,
            event["round"],
            event["event_id"],
            event["killer"],
            event["victim"],
            event["view"],
        )
        if key in self._dedupe:
            clip_id = self._dedupe[key]
            clip = self._clips[clip_id]
            return {"clip_id": clip_id, "created": False,
                    "start": clip["start"], "end": clip["end"]}

        server_frame = event["server_frame"]
        start = max(event["round_start"], server_frame - self.pre_buffer)
        end = min(event["round_end"], server_frame + self.post_buffer)
        cover_frame = event.get("cover_frame", server_frame)
        if not start <= cover_frame <= end:
            raise ValueError("cover_frame outside clip window")

        clip_id = "{}:{}:{}:{}".format(
            match_id, event["round"], event["event_id"], event["view"]
        )
        clip = {
            "clip_id": clip_id,
            "match_id": match_id,
            "round": event["round"],
            "event_id": event["event_id"],
            "view": event["view"],
            "match_time": match_time,
            "server_frame": server_frame,
            "start": start,
            "end": end,
            "audio_start": start,
            "audio_end": end,
            "cover_frame": cover_frame,
            "cover_ms": round(cover_frame * 1000 / self.fps),
            "status": "draft",
            "published_at": None,
            "expires_at": None,
            "share_token": None,
            "output_hash": None,
        }
        self._clips[clip_id] = clip
        self._dedupe[key] = clip_id
        return {"clip_id": clip_id, "created": True,
                "start": start, "end": end}

    def cut(self, clip_id):
        clip = self._clips.get(clip_id)
        if clip is None:
            raise ValueError("unknown clip")
        if clip["output_hash"] is None:
            clip["output_hash"] = hashlib.sha256(
                "{}:{}:{}".format(clip_id, clip["start"], clip["end"])
                .encode("utf-8")
            ).hexdigest()
        clip["status"] = "ready"
        return {
            "clip_id": clip_id,
            "status": "ready",
            "input_start": clip["start"],
            "input_end": clip["end"],
            "output_hash": clip["output_hash"],
        }

    def _match_lock(self, match_id):
        with self._match_locks_guard:
            lock = self._match_locks.get(match_id)
            if lock is None:
                lock = threading.Lock()
                self._match_locks[match_id] = lock
            return lock

    def publish(self, clip_id, now=0, ttl=60):
        clip = self._clips.get(clip_id)
        if clip is None:
            raise ValueError("unknown clip")
        with self._match_lock(clip["match_id"]):
            if clip["status"] == "published":
                return {
                    "clip_id": clip_id,
                    "status": "published",
                    "published_at": clip["published_at"],
                    "expires_at": clip["expires_at"],
                    "share_token": clip["share_token"],
                }
            if clip["status"] != "ready":
                raise ValueError("clip is not ready to publish")
            expires_at = now + ttl
            sig = hashlib.sha256(
                "{}:{}:{}".format(clip_id, expires_at, self._secret)
                .encode("utf-8")
            ).hexdigest()[:16]
            clip["share_token"] = "{}.{}.{}".format(clip_id, expires_at, sig)
            clip["published_at"] = now
            clip["expires_at"] = expires_at
            clip["status"] = "published"
            return {
                "clip_id": clip_id,
                "status": "published",
                "published_at": now,
                "expires_at": expires_at,
                "share_token": clip["share_token"],
            }

    def expire(self, now):
        count = 0
        for clip in self._clips.values():
            if (clip["status"] == "published"
                    and clip["expires_at"] is not None
                    and clip["expires_at"] <= now):
                clip["status"] = "expired"
                count += 1
        return count

    def index(self, match_id=None):
        records = [
            self._record(clip)
            for clip in self._clips.values()
            if match_id is None or clip["match_id"] == match_id
        ]
        records.sort(key=lambda row: (
            row["match_time"], row["server_frame"], row["clip_id"]
        ))
        return records

    def _record(self, clip):
        return {
            "clip_id": clip["clip_id"],
            "match_id": clip["match_id"],
            "round": clip["round"],
            "event_id": clip["event_id"],
            "view": clip["view"],
            "match_time": clip["match_time"],
            "server_frame": clip["server_frame"],
            "start": clip["start"],
            "end": clip["end"],
            "audio_start": clip["audio_start"],
            "audio_end": clip["audio_end"],
            "cover_frame": clip["cover_frame"],
            "cover_ms": clip["cover_ms"],
            "status": clip["status"],
            "published_at": clip["published_at"],
            "expires_at": clip["expires_at"],
        }

    def playback(self, clip_id, token=None, now=0):
        clip = self._clips.get(clip_id)
        if clip is None:
            raise ValueError("unknown clip")
        if not token or token != clip["share_token"]:
            raise PermissionError("invalid share token")
        if clip["status"] == "expired":
            raise PermissionError("clip has expired")
        if clip["status"] != "published":
            raise ValueError("clip is not published")
        if clip["expires_at"] is not None and clip["expires_at"] <= now:
            raise PermissionError("clip has expired")
        return self._record(clip)
