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
        self._server_order = {
            server: index for index, server in enumerate(self.server_priority)
        }
        self._next_server_order = len(self.server_priority)
        self._sequence = 0

        self._characters = []
        self._guilds = []
        self._mail = []
        self._listings = []
        self._friend_links = []
        self._blacklist_links = []
        self._scores = []

        self._merged_characters = {}
        self._merged_guilds = {}
        self._merged_character_keys = {}
        self._merged_guild_keys = {}
        self._taken_names = {"character": set(), "guild": set()}
        self._rename_queue = []
        self._batches = []
        self._merge_complete = False
        self._social = None

    def _server_rank(self, server):
        if server not in self._server_order:
            self._server_order[server] = self._next_server_order
            self._next_server_order += 1
        return self._server_order[server]

    def _next_sequence(self):
        sequence = self._sequence
        self._sequence += 1
        return sequence

    def _record(self, **values):
        values["sequence"] = self._next_sequence()
        values["merged"] = False
        values["merged_id"] = None
        values["merged_name"] = None
        return values

    def _invalidate_merge(self):
        self._merge_complete = False
        self._social = None

    def add_character(self, server, character_id, account, name):
        self._server_rank(server)
        character = self._record(
            server=server,
            character_id=character_id,
            account=account,
            name=name,
        )
        self._characters.append(character)
        self._invalidate_merge()
        return character

    def add_guild(self, server, guild_id, name):
        self._server_rank(server)
        guild = self._record(server=server, guild_id=guild_id, name=name)
        self._guilds.append(guild)
        self._invalidate_merge()
        return guild

    def add_mail(self, server, mail_id, recipient_id, subject):
        self._server_rank(server)
        mail = self._record(
            server=server,
            mail_id=mail_id,
            recipient_id=recipient_id,
            subject=subject,
        )
        self._mail.append(mail)
        self._social = None
        return mail

    def add_listing(self, server, listing_id, seller_id, item):
        self._server_rank(server)
        listing = self._record(
            server=server,
            listing_id=listing_id,
            seller_id=seller_id,
            item=item,
        )
        self._listings.append(listing)
        self._social = None
        return listing

    def add_friend(self, server, from_id, to_id):
        self._server_rank(server)
        friendship = self._record(server=server, from_id=from_id, to_id=to_id)
        self._friend_links.append(friendship)
        self._social = None
        return friendship

    def add_blacklist(self, server, owner_id, blocked_id):
        self._server_rank(server)
        blocked = self._record(
            server=server, owner_id=owner_id, blocked_id=blocked_id
        )
        self._blacklist_links.append(blocked)
        self._social = None
        return blocked

    def report_score(self, server, account, score):
        self._server_rank(server)
        row = self._record(server=server, account=account, score=score)
        self._scores.append(row)
        self._social = None
        return row

    def _pending(self):
        characters = [row for row in self._characters if not row["merged"]]
        guilds = [row for row in self._guilds if not row["merged"]]
        characters.sort(
            key=lambda row: (self._server_rank(row["server"]), row["sequence"])
        )
        guilds.sort(
            key=lambda row: (self._server_rank(row["server"]), row["sequence"])
        )
        return characters, guilds

    def _unique_name(self, kind, name, server):
        taken = self._taken_names[kind]
        if name not in taken:
            return name

        base = "{}@{}".format(name, server)
        candidate = base
        suffix = 2
        while candidate in taken:
            candidate = "{}#{}".format(base, suffix)
            suffix += 1
        return candidate

    def _unique_character_id(self, character_id, server):
        if character_id not in self._merged_characters:
            return character_id

        base = "{}@{}".format(character_id, server)
        candidate = base
        suffix = 2
        while candidate in self._merged_characters:
            candidate = "{}#{}".format(base, suffix)
            suffix += 1
        return candidate

    def _unique_guild_id(self, guild_id, server):
        if guild_id not in self._merged_guilds:
            return guild_id

        base = "{}@{}".format(guild_id, server)
        candidate = base
        suffix = 2
        while candidate in self._merged_guilds:
            candidate = "{}#{}".format(base, suffix)
            suffix += 1
        return candidate

    def _merge_character(self, character):
        merged_id = self._unique_character_id(
            character["character_id"], character["server"]
        )
        merged_name = self._unique_name(
            "character", character["name"], character["server"]
        )

        character["merged"] = True
        character["merged_id"] = merged_id
        character["merged_name"] = merged_name
        self._merged_characters[merged_id] = character
        self._merged_character_keys.setdefault(
            (character["server"], character["character_id"]), character
        )
        self._taken_names["character"].add(merged_name)

        renamed = merged_name != character["name"]
        if renamed:
            self._rename_queue.append(
                {
                    "kind": "character",
                    "entity_id": merged_id,
                    "old_name": character["name"],
                    "new_name": merged_name,
                }
            )
        return renamed

    def _merge_guild(self, guild):
        merged_id = self._unique_guild_id(guild["guild_id"], guild["server"])
        merged_name = self._unique_name("guild", guild["name"], guild["server"])

        guild["merged"] = True
        guild["merged_id"] = merged_id
        guild["merged_name"] = merged_name
        self._merged_guilds[merged_id] = guild
        self._merged_guild_keys.setdefault(
            (guild["server"], guild["guild_id"]), guild
        )
        self._taken_names["guild"].add(merged_name)

        renamed = merged_name != guild["name"]
        if renamed:
            self._rename_queue.append(
                {
                    "kind": "guild",
                    "entity_id": merged_id,
                    "old_name": guild["name"],
                    "new_name": merged_name,
                }
            )
        return renamed

    def merge(self, batch_size=None):
        if batch_size is not None:
            if not isinstance(batch_size, int) or isinstance(batch_size, bool):
                raise TypeError("batch_size must be an integer or None")
            if batch_size < 1:
                raise ValueError("batch_size must be at least 1")

        characters, guilds = self._pending()
        candidates = characters + guilds
        if batch_size is None:
            selected = candidates
        else:
            selected = candidates[:batch_size]

        journal = []
        queue_start = len(self._rename_queue)
        for entity in selected:
            if "character_id" in entity:
                kind = "character"
                self._merge_character(entity)
            else:
                kind = "guild"
                self._merge_guild(entity)
            journal.append((kind, entity))

        self._batches.append(
            {
                "entries": journal,
                "rename_queue_start": queue_start,
            }
        )

        remaining_characters, remaining_guilds = self._pending()
        remaining = len(remaining_characters) + len(remaining_guilds)
        self._merge_complete = remaining == 0
        self._social = None
        return {
            "processed": len(selected),
            "remaining": remaining,
            "complete": self._merge_complete,
        }

    def resolve_name(self, entity_id):
        character = self._merged_characters.get(entity_id)
        if character is not None:
            return character["merged_name"]
        guild = self._merged_guilds.get(entity_id)
        if guild is not None:
            return guild["merged_name"]
        raise KeyError(entity_id)

    def rename_queue(self):
        return [dict(item) for item in self._rename_queue]

    def login(self, character_id):
        if not self._merge_complete:
            raise RuntimeError("realm merge is not complete")
        character = self._merged_characters.get(character_id)
        if character is None:
            raise KeyError(character_id)
        return {
            "character_id": character["merged_id"],
            "name": character["merged_name"],
        }

    def _character_key(self, server, character_id):
        key = (server, character_id)
        if key in self._merged_character_keys:
            return self._merged_character_keys[key]

        matches = [
            row
            for row in self._merged_characters.values()
            if row["character_id"] == character_id
        ]
        if not matches:
            return self._merged_characters.get(character_id)
        matches.sort(
            key=lambda row: (self._server_rank(row["server"]), row["sequence"])
        )
        return matches[0]

    def _character_ref(self, server, character_id):
        row = self._character_key(server, character_id)
        if row is None:
            return character_id
        return row["merged_id"]

    def _migrate_mail(self):
        by_id = {}
        for mail in self._mail:
            current = by_id.get(mail["mail_id"])
            if current is None:
                by_id[mail["mail_id"]] = mail
                continue
            current_key = (
                self._server_rank(current["server"]),
                current["sequence"],
            )
            candidate_key = (
                self._server_rank(mail["server"]),
                mail["sequence"],
            )
            if candidate_key < current_key:
                by_id[mail["mail_id"]] = mail

        rows = []
        for mail in sorted(by_id.values(), key=lambda row: row["sequence"]):
            rows.append(
                {
                    "mail_id": mail["mail_id"],
                    "recipient_id": self._character_ref(
                        mail["server"], mail["recipient_id"]
                    ),
                    "subject": mail["subject"],
                    "server": mail["server"],
                }
            )
        return rows

    def _migrate_listings(self):
        rows = []
        for index, listing in enumerate(
            sorted(self._listings, key=lambda row: row["sequence"]), start=1
        ):
            rows.append(
                {
                    "listing_id": "L{}".format(index),
                    "original_listing_id": listing["listing_id"],
                    "seller_id": self._character_ref(
                        listing["server"], listing["seller_id"]
                    ),
                    "item": listing["item"],
                    "server": listing["server"],
                }
            )
        return rows

    def _migrate_friends(self):
        links = set()
        for friendship in sorted(
            self._friend_links, key=lambda row: row["sequence"]
        ):
            left = self._character_ref(
                friendship["server"], friendship["from_id"]
            )
            right = self._character_ref(
                friendship["server"], friendship["to_id"]
            )
            if left == right:
                continue
            links.add((left, right))
            links.add((right, left))
        return links

    def _migrate_blacklist(self):
        links = set()
        for blocked in sorted(
            self._blacklist_links, key=lambda row: row["sequence"]
        ):
            owner = self._character_ref(blocked["server"], blocked["owner_id"])
            other = self._character_ref(blocked["server"], blocked["blocked_id"])
            if owner != other:
                links.add((owner, other))
        return links

    def _migrate_leaderboard(self):
        best_scores = {}
        for row in self._scores:
            account = row["account"]
            score = row["score"]
            if account not in best_scores or score > best_scores[account]:
                best_scores[account] = score

        ordered = sorted(
            best_scores.items(), key=lambda item: (-item[1], item[0])
        )
        return [
            {"account": account, "score": score, "rank": rank}
            for rank, (account, score) in enumerate(ordered, start=1)
        ]

    def migrate_social(self):
        if not self._merge_complete:
            raise RuntimeError("realm merge is not complete")

        mail = self._migrate_mail()
        listings = self._migrate_listings()
        friend_links = self._migrate_friends()
        blacklist = self._migrate_blacklist()
        leaderboard = self._migrate_leaderboard()
        self._social = {
            "mail": mail,
            "listings": listings,
            "friends": friend_links,
            "blacklist": blacklist,
            "leaderboard": leaderboard,
        }
        return {
            "mail": len(mail),
            "listings": len(listings),
            "friends": len(friend_links),
            "blacklist": len(blacklist),
            "leaderboard": len(leaderboard),
        }

    def _require_social(self):
        if self._social is None:
            self.migrate_social()
        return self._social

    def friends(self, character_id):
        social = self._require_social()
        return sorted(
            other
            for owner, other in social["friends"]
            if owner == character_id
        )

    def is_blocked(self, owner_id, other_id):
        social = self._require_social()
        return (owner_id, other_id) in social["blacklist"]

    def mail_box(self, character_id):
        social = self._require_social()
        return [
            dict(row)
            for row in social["mail"]
            if row["recipient_id"] == character_id
        ]

    def listings(self):
        social = self._require_social()
        return [dict(row) for row in social["listings"]]

    def leaderboard(self):
        social = self._require_social()
        return [dict(row) for row in social["leaderboard"]]

    def rollback(self):
        if not self._batches:
            return 0

        batch = self._batches.pop()
        entries = batch["entries"]
        if not entries:
            return 0
        for kind, entity in reversed(entries):
            merged_id = entity["merged_id"]
            merged_name = entity["merged_name"]
            if kind == "character":
                if self._merged_characters.get(merged_id) is entity:
                    del self._merged_characters[merged_id]
                source_key = (entity["server"], entity["character_id"])
                if self._merged_character_keys.get(source_key) is entity:
                    del self._merged_character_keys[source_key]
            else:
                if self._merged_guilds.get(merged_id) is entity:
                    del self._merged_guilds[merged_id]
                source_key = (entity["server"], entity["guild_id"])
                if self._merged_guild_keys.get(source_key) is entity:
                    del self._merged_guild_keys[source_key]

            if merged_name is not None:
                self._taken_names[kind].discard(merged_name)
            entity["merged"] = False
            entity["merged_id"] = None
            entity["merged_name"] = None

        del self._rename_queue[batch["rename_queue_start"] :]
        self._merge_complete = False
        self._social = None
        return len(entries)
