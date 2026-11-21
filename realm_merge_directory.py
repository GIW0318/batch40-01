"""Cross-realm merge directory.

Contract used by the tests
--------------------------
* ``__init__(server_priority=None)``: ordered server list, highest priority
  first.  Characters and guilds merge by priority then insertion order; on a
  name collision the first merged entity keeps the name and later ones
  become ``"<name>@<server>"`` and are appended to the rename queue.
* Registration helpers: ``add_character(server, character_id, account,
  name)``, ``add_guild(server, guild_id, name)``, ``add_mail(server,
  mail_id, recipient_id, subject)``, ``add_listing(server, listing_id,
  seller_id, item)``, ``add_friend(server, from_id, to_id)``,
  ``add_blacklist(server, owner_id, blocked_id)``,
  ``report_score(server, account, score)``.
* ``merge(batch_size=None)`` merges pending characters then guilds, at most
  batch_size entries (None = all).  Returns {"processed": n, "remaining":
  m, "complete": bool}.  Each call is one rollbackable batch.
* ``resolve_name(entity_id)`` returns the merged display name; KeyError when
  the entity is not merged yet.
* ``rename_queue()`` returns [{"kind", "entity_id", "old_name",
  "new_name"}].
* ``login(character_id)`` returns {"character_id", "name"}; it raises
  RuntimeError until a merge has completed (old data must not be served) and
  KeyError for unknown ids.
* ``migrate_social()`` requires a completed merge (else RuntimeError).  It
  consolidates mail (deduplicated by mail_id, higher priority server wins),
  listings (new ids "L1", "L2", ... in insertion order, original id and
  server kept), friend links (rebuilt bidirectionally), blacklists, and the
  leaderboard (one row per account using the best score, sorted by score
  desc then account asc).  Returns a stats dict.
* ``friends(character_id)`` returns sorted friend ids;
  ``is_blocked(owner_id, other_id)``; ``mail_box(character_id)``;
  ``listings()``; ``leaderboard()``.
* ``rollback()`` undoes only the most recent merge batch and returns the
  number of entries reverted.  Registrations made after the batch are
  untouched.
"""


class RealmMergeDirectory:
    def __init__(self, server_priority=None):
        self.server_priority = list(server_priority or [])

    def add_character(self, server, character_id, account, name):
        raise NotImplementedError

    def add_guild(self, server, guild_id, name):
        raise NotImplementedError

    def add_mail(self, server, mail_id, recipient_id, subject):
        raise NotImplementedError

    def add_listing(self, server, listing_id, seller_id, item):
        raise NotImplementedError

    def add_friend(self, server, from_id, to_id):
        raise NotImplementedError

    def add_blacklist(self, server, owner_id, blocked_id):
        raise NotImplementedError

    def report_score(self, server, account, score):
        raise NotImplementedError

    def merge(self, batch_size=None):
        raise NotImplementedError

    def resolve_name(self, entity_id):
        raise NotImplementedError

    def rename_queue(self):
        raise NotImplementedError

    def login(self, character_id):
        raise NotImplementedError

    def migrate_social(self):
        raise NotImplementedError

    def friends(self, character_id):
        raise NotImplementedError

    def is_blocked(self, owner_id, other_id):
        raise NotImplementedError

    def mail_box(self, character_id):
        raise NotImplementedError

    def listings(self):
        raise NotImplementedError

    def leaderboard(self):
        raise NotImplementedError

    def rollback(self):
        raise NotImplementedError
