-- Karaokemp library index — schema per spec §3.
-- STRICT tables, WAL mode, foreign keys ON, ISO-8601 UTC timestamps.
--
-- The v1 schema conflated content identity with physical location. v2 splits them:
-- `blobs` is content identity (one row per distinct byte-content); `file_locations` is
-- physical/remote copies (many locations may share one blob — that is the point).
--
-- DEVIATION FROM SPEC (documented, deliberate) — file_locations.size_bytes:
--   §5.2 requires the Stage 0 exact-dup pass to group by (gdrive_md5, size_bytes), but §3.2
--   gives file_locations no size column and §3.1 puts size_bytes on blobs — which cannot exist
--   until Stage 3 hashes the bytes. As written the spec asks Stage 0 to group on a field that
--   does not yet exist. file_locations.size_bytes closes that gap.
--   It is NOT redundant with blobs.size_bytes: this is Drive's *reported* size at enumeration
--   (remote claim, unverified); blobs.size_bytes is a *verified* local byte count. Keeping both
--   is what lets Stage 3 detect a mismatch between what Drive claimed and what we received.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- §3.1 blobs — content identity. One row per distinct byte-content.
CREATE TABLE IF NOT EXISTS blobs (
    content_hash     TEXT PRIMARY KEY,               -- SHA-256 of bytes
    size_bytes       INTEGER NOT NULL,               -- verified local byte count
    integrity_status TEXT NOT NULL DEFAULT 'unchecked'
        CHECK (integrity_status IN ('unchecked','probed_ok','decoded_ok','suspect','broken')),
    integrity_detail TEXT,                           -- JSON: which checks ran, failures
    first_hashed_at  TEXT
) STRICT;

-- §3.2 file_locations — physical/remote copies.
-- drive_file_id is the remote identity key. remote_path is informational only:
-- Drive permits duplicate names in one folder, so paths are NOT unique (§5.1).
CREATE TABLE IF NOT EXISTS file_locations (
    id                 INTEGER PRIMARY KEY,
    drive_file_id      TEXT UNIQUE,                  -- NULL for local-only files (artifacts, zip members)
    remote_path        TEXT,
    local_path         TEXT,                         -- NULL until downloaded/extracted
    parent_location_id INTEGER REFERENCES file_locations(id),  -- for zip members: the containing zip
    member_path        TEXT,                         -- path within the container
    filetype           TEXT NOT NULL,                -- lowercased extension
    size_bytes         INTEGER,                      -- Drive-reported size at enumeration (see header note)
    gdrive_md5         TEXT,
    content_hash       TEXT REFERENCES blobs(content_hash),    -- NULL until hashed. Deliberately NOT unique.
    role               TEXT CHECK (role IS NULL OR role IN ('av','audio','graphics','lyrics','container')),
    status             TEXT NOT NULL DEFAULT 'remote_only'
        CHECK (status IN ('remote_only','excluded','staged','active','archived')),
    archive_reason     TEXT,
    first_seen_at      TEXT,
    updated_at         TEXT
) STRICT;

-- The exact-dup pass (§5.2) groups on (gdrive_md5, size_bytes); this index serves it directly.
CREATE INDEX IF NOT EXISTS ix_loc_md5_size ON file_locations(gdrive_md5, size_bytes);
CREATE INDEX IF NOT EXISTS ix_loc_status   ON file_locations(status);
CREATE INDEX IF NOT EXISTS ix_loc_filetype ON file_locations(filetype);
CREATE INDEX IF NOT EXISTS ix_loc_hash     ON file_locations(content_hash);
CREATE INDEX IF NOT EXISTS ix_loc_parent   ON file_locations(parent_location_id);

-- DEVIATION FROM SPEC (documented, deliberate) — location_parses:
--   §6.1 says Stage 1 "produc[es] parse payloads per location", but §3 gives them nowhere to
--   live: song_metadata keys on media_item_id, and media_items do not exist until Stage 3.
--   As written the spec asks Stage 1 to persist a per-location fact into a per-item table that
--   is three stages away. Same class of gap as file_locations.size_bytes above, resolved the
--   same way: a table keyed on what actually exists now.
--   Stage 3 promotes these hints into song_metadata (source 'filename') once media items exist,
--   merging the hints of every location in a hash group onto the survivor's item (§5.2, §6.1).
--
--   The scalar columns are a queryable projection of `payload`; `payload` is the whole Parse and
--   is the record of what the parser actually said (flags, notes, folder_hints). Both are kept
--   because §6.2 makes the parser the component most likely to be re-run and re-argued about:
--   the columns serve pairing and enrichment, the payload serves auditing a past verdict.
CREATE TABLE IF NOT EXISTS location_parses (
    location_id     INTEGER PRIMARY KEY REFERENCES file_locations(id),
    artist          TEXT,
    title           TEXT,
    disc_series     TEXT,
    disc_id         TEXT,
    disc_track      TEXT,
    catalogue_code  TEXT,          -- trailing "- NNNN" of the Hebrew batch; NOT a disc series
    is_instrumental TEXT CHECK (is_instrumental IS NULL OR is_instrumental IN ('yes','no','unknown')),
    language        TEXT,
    layout          TEXT NOT NULL, -- which pattern fired; 'unparsed'/'opaque'/'non_media' are real answers
    confidence      REAL NOT NULL, -- ordinal, not a probability: it orders hint conflicts (§6.1)
    payload         TEXT NOT NULL, -- JSON: the full Parse, incl. flags/notes/folder_hints
    parser_version  TEXT NOT NULL, -- fingerprint of the parser source that produced this row
    parsed_at       TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS ix_parse_artist_title ON location_parses(artist, title);
CREATE INDEX IF NOT EXISTS ix_parse_layout       ON location_parses(layout);
CREATE INDEX IF NOT EXISTS ix_parse_version      ON location_parses(parser_version);

-- DEVIATION FROM SPEC (documented, deliberate) — provisional_pairs keyed on md5:
--   §6.3 pairs MP3+CDG "by identical basename within the same directory, on the remote listing",
--   and the pair must survive into Stage 3. But the §5.2 survivor rule ("lexicographically
--   smallest drive_file_id") is applied to each file INDEPENDENTLY, and Drive IDs are random —
--   so for a song present in two directories, the mp3's survivor and the cdg's survivor land in
--   different directories about half the time. Measured on this library: 1,120 intact pairs have
--   exactly one half excluded. Pairing over post-exclusion rows would shred each of them into a
--   stray audio_only file plus a discarded cdg.
--
--   The fix is not to change §5.2 (its rule is correct and re-run-safe) but to record the pair
--   where directories cannot break it: at CONTENT level. A pair is a fact about bytes, not about
--   folders. §9.2's Organize re-co-locates both halves at the end regardless of where they sat.
--
--   Keyed on gdrive_md5 rather than blobs.content_hash because blobs do not exist until Stage 3
--   hashes bytes; md5 is the content key that exists NOW, is 100% populated (§5.3), and survives
--   exclusion. Stage 3 maps md5 → content_hash and promotes these into media_items.
--
--   NOT one-to-one, by design: 759 cdg blobs here pair with more than one mp3 blob, because the
--   graphics track was reused byte-identically across several re-encodes of the same audio.
--   That is a real many-to-many, not a conflict; Stage 3's clustering collapses the audio variants.
CREATE TABLE IF NOT EXISTS provisional_pairs (
    audio_md5    TEXT NOT NULL,    -- the mp3's gdrive_md5
    graphics_md5 TEXT NOT NULL,    -- the cdg's gdrive_md5
    witnesses    INTEGER NOT NULL, -- how many directory+basename groups attest this pair
    example_path TEXT,             -- one witnessing remote_path, for eyeballing
    created_at   TEXT,
    PRIMARY KEY (audio_md5, graphics_md5)
) STRICT;

CREATE INDEX IF NOT EXISTS ix_pair_graphics ON provisional_pairs(graphics_md5);

-- Orphan MP3s (§6.3): "future audio_only item, flagged for vocal-presence check (possible full
-- originals — Demucs input, do not archive)". Derived rather than stored: an orphan is exactly an
-- mp3 whose content attests no pair, so a column would be a denormalized copy of that fact and
-- could drift out of date the moment pairing re-runs. Orphan CDGs are NOT derived this way —
-- §6.3 mandates excluding them, so their verdict is materialized on file_locations.
CREATE VIEW IF NOT EXISTS v_provisional_orphan_mp3 AS
SELECT l.id, l.drive_file_id, l.remote_path, l.gdrive_md5, l.size_bytes, l.status
FROM file_locations l
WHERE l.filetype = 'mp3'
  AND l.gdrive_md5 IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM provisional_pairs p WHERE p.audio_md5 = l.gdrive_md5);

-- §3.5 clusters. Declared before media_items, which references it.
-- A CLUSTER IS A SONG (§7.3). Every active item has one, singletons included, so cluster_id
-- is universally non-NULL and `v_songs` is one row per cluster.
--
-- `method` says WHAT MADE THIS A SONG. Name is the sole cluster-forming key, so:
--   title_match  the names agree — the ordinary way a song acquires a second copy
--   manual       no name evidence; an operator recorded a §10 "same song" edge
--   fingerprint  no name and no operator: the no-name fallback grouped items that have no
--                key at all (opaque parses) by their audio
--   NULL         SINGLETON — one copy, nothing merged it, nothing to explain. A real answer,
--                not missing data.
--   exact_hash   reserved for clusters created outside §7.3; never written or touched here.
-- Mixed evidence is not a fifth value: `notes` carries
-- {"items","merge_edges","evidence":{"audio","name","manual"},"confidence_basis"}.
-- `confidence` is the WEAKEST link holding the component together, on the scale named by
-- notes.confidence_basis ('name_evidence' / 'operator' / 'fingerprint_similarity' / 'none').
-- The scales are not comparable; never read confidence without confidence_basis.
CREATE TABLE IF NOT EXISTS clusters (
    id         INTEGER PRIMARY KEY,
    method     TEXT CHECK (method IS NULL OR method IN ('fingerprint','exact_hash','title_match','manual')),
    confidence REAL,
    notes      TEXT
) STRICT;

-- §3.3 media_items — logical playable units. Created at Stage 3.
CREATE TABLE IF NOT EXISTS media_items (
    id                 INTEGER PRIMARY KEY,
    format             TEXT NOT NULL CHECK (format IN ('video','mp3g','audio_only','audio_lrc')),
    cluster_id         INTEGER REFERENCES clusters(id),
    duration_sec       REAL,
    is_instrumental    TEXT CHECK (is_instrumental IS NULL OR is_instrumental IN ('yes','no','unknown')),
    audio_codec        TEXT,
    audio_bitrate_kbps INTEGER,
    sample_rate        INTEGER,
    video_codec        TEXT,
    width              INTEGER,
    height             INTEGER,
    quality_attrs      TEXT,   -- JSON of measured attributes used for ranking (§7.3).
                               -- Deliberately no scalar score: verdict + attrs + rules are the contract.
    -- §7.4 verdict — what should HAPPEN to this file. Migration 006 replaced 'loser' with
    -- 'alternate': only exact-content duplicates are duplicates (and Stage 0 already collapsed
    -- those), so a lower-ranked copy of a song stays ACTIVE and is never archived. The old
    -- name described an outcome that no longer happens.
    --   sole_copy     the song has exactly one active item
    --   winner        rank 1 of a song with more than one copy, per FORMAT (formats are peers)
    --   alternate     rank >= 2. Active. Ranked. Not archived.
    --   manual_review a human is needed (av split, duration outlier); the rank still stands
    quality_verdict    TEXT CHECK (quality_verdict IS NULL OR
                                   quality_verdict IN ('winner','alternate','sole_copy','pending','manual_review')),
    -- §7.4 rank within (cluster, format): 1 is the default copy to serve. A TOTAL order, so
    -- every song always has a default even while a manual_review question about it is open.
    quality_rank       INTEGER,
    status             TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','archived','superseded')),
    superseded_by      INTEGER REFERENCES media_items(id),
    created_at         TEXT,
    updated_at         TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS ix_item_cluster ON media_items(cluster_id);
CREATE INDEX IF NOT EXISTS ix_item_status  ON media_items(status);

-- An mp3g item has roles audio+graphics; video has av; future audio_lrc has audio+lyrics.
CREATE TABLE IF NOT EXISTS media_item_files (
    media_item_id INTEGER NOT NULL REFERENCES media_items(id),
    content_hash  TEXT NOT NULL REFERENCES blobs(content_hash),
    role          TEXT NOT NULL CHECK (role IN ('av','audio','graphics','lyrics','container')),
    PRIMARY KEY (media_item_id, role)
) STRICT;

-- §3.4 fingerprints — keyed by blob: a fingerprint is a property of the audio content,
-- so identical blobs are fingerprinted once for free. For video blobs the fingerprint is
-- of the demuxed audio stream, still keyed by the video blob's hash.
CREATE TABLE IF NOT EXISTS fingerprints (
    content_hash            TEXT PRIMARY KEY REFERENCES blobs(content_hash),
    chromaprint             TEXT NOT NULL,   -- fpcalc raw fingerprint; encoding documented in stage3
    fp_duration_sec         REAL,
    acoustid_checked_at     TEXT,
    -- ACOUSTIC identity: the recording this audio actually is, per AcoustID.
    -- Expected NULL for karaoke covers. Distinct from song-level song_mbid (§3.6, §8).
    acoustid_recording_mbid TEXT,
    acoustid_score          REAL
) STRICT;

-- §3.5 cluster_edges — persist ALL edges, including sub-threshold candidates,
-- for auditability and threshold re-tuning (§11 retune).
-- edge_type 'manual' (similarity 1.0) is an OPERATOR duplicate verdict (§10 review tooling):
-- a reviewer said "these two items are the same song". cluster.py's union-find merges manual
-- edges unconditionally so `cluster` then `verdicts` re-derive the merge every run, the same
-- re-apply discipline as resolve_pair_mismatch. Manual edges are NOT touched by cluster's
-- fingerprint-edge reconciliation (they are not derived data).
CREATE TABLE IF NOT EXISTS cluster_edges (
    item_a     INTEGER NOT NULL REFERENCES media_items(id),
    item_b     INTEGER NOT NULL REFERENCES media_items(id),
    edge_type  TEXT NOT NULL CHECK (edge_type IN ('fingerprint','prefix_fingerprint','title_match','manual')),
    similarity REAL,
    PRIMARY KEY (item_a, item_b, edge_type)
) STRICT;

-- §3.6 song_metadata — field-level provenance.
-- The trust order is NOT restated here: `v_metadata`'s CASE below is its single authority, and
-- it was renumbered by five migrations (003 demoted id3 below filename, 005 seeded the
-- catalogue ranks, 006 widened the CHECK to match, 007 inserted llm_parse, 008 demoted
-- musicbrainz_fp below filename), all since folded into this file. A second copy of the
-- ladder in prose is exactly what drifts.
-- Each enrichment pass writes only its own source's rows; no pass overwrites a higher-trust source.
--
-- song_mbid here is SONG-LEVEL identity (which song this is), NOT acoustic identity. For a
-- karaoke cover it points at the original song's MB recording/work, whose length and ISRC do
-- NOT describe this audio. Nothing downstream may treat it as acoustic truth; acoustic identity
-- lives only in fingerprints.acoustid_recording_mbid (§3.4, §8).
CREATE TABLE IF NOT EXISTS song_metadata (
    media_item_id INTEGER NOT NULL REFERENCES media_items(id),
    field         TEXT NOT NULL CHECK (field IN
                      ('artist','title','language','year','genre','song_mbid','disc_id','disc_series')),
    value         TEXT,   -- genre: JSON array
    source        TEXT NOT NULL CHECK (source IN
                      ('filename','id3','musicbrainz_text','musicbrainz_freetext',
                       'musicbrainz_fp','musicbrainz_artist','wikidata','spotify_text',
                       'itunes_text','deezer_text','title_card_ocr','llm_parse','manual')),
    confidence    REAL,
    updated_at    TEXT,
    PRIMARY KEY (media_item_id, field, source)
) STRICT;

-- Winning value per (item, field) by trust order, confidence as tie-break.
--
-- `source_rank` is the trust ladder made READABLE (migration 005). It was previously an
-- anonymous CASE buried in the ORDER BY, so any consumer that needed to compare two items'
-- metadata trust — `v_songs` picking the representative name for a song — had to restate the
-- whole ladder and would silently drift out of step with the next migration that renumbers it.
-- Exposing it is behaviour-neutral: the same expression now has a name.
--
-- The NUMBERS are renumbered wholesale by any migration that inserts a source mid-ladder (007
-- last did so), and carry no meaning beyond their order — only the RELATIVE order is a contract.
-- Never persist a source_rank value or compare one across schema versions.
--
-- `ELSE 0` is a TRAP worth stating once: a source that is missing from this CASE ranks BELOW
-- 'id3', so its rows stay in song_metadata but become permanently inert. Every migration that
-- widens the source CHECK must therefore also recreate this view (004's header warns about this;
-- 005 pre-listed 006's four sources for the same reason).
CREATE VIEW IF NOT EXISTS v_metadata AS
SELECT media_item_id, field, value, source, confidence, source_rank
FROM (
    SELECT media_item_id, field, value, source, confidence, source_rank,
           ROW_NUMBER() OVER (
               PARTITION BY media_item_id, field
               ORDER BY source_rank DESC,
                        COALESCE(confidence, 0) DESC,
                        source ASC          -- deterministic final tie-break
           ) AS rn
    FROM (
        SELECT media_item_id, field, value, source, confidence,
               CASE source
                   WHEN 'manual'                THEN 14
                   WHEN 'musicbrainz_text'      THEN 13
                   -- Unqualified free-text MB search (§9.1.1), accepted only on agreement
                   -- with BOTH existing fields. Below the field-qualified search, which
                   -- matched on fields we had already segmented correctly.
                   WHEN 'musicbrainz_freetext'  THEN 12
                   -- MB ARTIST index (§9.1.2). Anchored on who, not on which song.
                   WHEN 'musicbrainz_artist'    THEN 11
                   -- Curated knowledge graph; the best Hebrew NAME source measured in this
                   -- project, weaker at identifying songs.
                   WHEN 'wikidata'              THEN 10
                   -- §6.1 order/segmentation corrections.
                   WHEN 'spotify_text'          THEN  9
                   -- Music catalogues, same role as spotify_text. iTunes above Deezer
                   -- on measured accuracy (67.5% vs 55.0%).
                   WHEN 'itunes_text'           THEN  8
                   WHEN 'deezer_text'           THEN  7
                   -- The producer's own on-screen title card, read by OCR. Above both local
                   -- parses (opaque names carry nothing to parse), below the
                   -- catalogue-matched corrections.
                   WHEN 'title_card_ocr'        THEN  6
                   -- §6.1/§6.2 LLM segmentation of the FULL PATH, for the layouts the regex
                   -- parser could not resolve (title_artist, title_only, style_of, and the
                   -- Hebrew artist_title order coin-flip). Below every corroborated
                   -- catalogue above it — those were accepted only on agreement with our own
                   -- text, an LLM read has no such corroboration — and above the regex parse
                   -- it replaces, which already told us it did not know.
                   WHEN 'llm_parse'             THEN  5
                   -- filename ABOVE id3 (migration 003): this library's ID3v1 tags truncate
                   -- at a fixed 30 bytes and keep decorations the parser strips. 2,098 titles
                   -- land exactly on that boundary. id3 remains a last resort for
                   -- unparseable names.
                   WHEN 'filename'              THEN  4
                   -- AcoustID/chromaprint BELOW filename (migration 008): a karaoke backing
                   -- track fingerprints onto OTHER karaoke and cover recordings, so MusicBrainz
                   -- names the cover act. Measured 2026-08-22: overriding musicbrainz_text it
                   -- was never an improvement. Rows kept as evidence, inert at this rank.
                   WHEN 'musicbrainz_fp'        THEN  3
                   WHEN 'id3'                   THEN  2
                   ELSE 0
               END AS source_rank
        FROM song_metadata
        WHERE value IS NOT NULL
    )
)
WHERE rn = 1;

-- §3.11 v_songs — ONE ROW PER SONG. A cluster is a song (§7.3), so this is one row per
-- cluster holding at least one active item.
--
-- **A VIEW, not a table, and deliberately so.** It cannot drift from the index, it needs no
-- migration when clustering changes, and it is automatically correct after every re-cluster —
-- the same reasoning that makes v_metadata a view, and §1's "the index is the source of
-- truth". A durable `songs` TABLE is explicitly DEFERRED until something OUTSIDE the index
-- needs a song id that survives re-clustering (a public permalink, a printed songbook
-- number, an external playlist referencing songs by id). Re-clustering renumbers clusters
-- freely; the moment an outside system stores one of those numbers, that stops being
-- acceptable and a table with stable ids plus a mapping becomes necessary. Nothing needs
-- that today. Recorded here so the decision is not relitigated.
--
-- COST, measured 2026-07-31 on the full index (22,195 songs): about **55-60 seconds** per
-- query. Every constituent piece is fast on its own (the metadata fold is 1.3s, the item/blob
-- joins 0.1s) -- the time goes on SQLite re-deriving the pipeline, and neither MATERIALIZED
-- CTE hints nor hoisting the aggregate into its own CTE moved it by more than ~3% (both were
-- tried and measured; the output was verified byte-identical, and neither earned its
-- complexity). So: fine for a report, a catalogue export or an ad-hoc question; NOT something
-- to put in a loop or behind an interactive search box. A caller that needs it repeatedly
-- should fold it into a temp table once per run -- exactly the pattern
-- stage5.materialize_metadata already uses and documents for v_metadata. If a UI ever needs
-- this live, that is the point at which the deferred `songs` table earns its keep, and the
-- reason will be latency rather than id stability.
--
-- Representative name: the item whose artist+title come from the highest-trust sources
-- (v_metadata.source_rank summed over the two fields), tie-broken by the §7.4 rank — the
-- default copy — and then by item id. Fully deterministic.
--
-- `distinct_names` is the honesty column. An order-insensitive, containment-aware key
-- deliberately unites "Pink" with "Pink & Nate Ruess", so a song can legitimately hold more
-- than one raw name string. Exposing the count keeps that visible per row instead of
-- collapsing it silently behind one representative; > 1 means "look before trusting the
-- displayed name". It uses the same crude lower+trim key the operator measures with, so the
-- number here and the number in the cluster report mean the same thing.
CREATE VIEW IF NOT EXISTS v_songs AS
WITH meta AS (
    SELECT v.media_item_id AS item_id,
           MAX(CASE WHEN v.field='artist'    THEN v.value END) AS artist,
           MAX(CASE WHEN v.field='title'     THEN v.value END) AS title,
           MAX(CASE WHEN v.field='language'  THEN v.value END) AS language,
           MAX(CASE WHEN v.field='song_mbid' THEN v.value END) AS song_mbid,
           COALESCE(MAX(CASE WHEN v.field='artist' THEN v.source_rank END), 0)
         + COALESCE(MAX(CASE WHEN v.field='title'  THEN v.source_rank END), 0) AS name_trust,
           MAX(CASE WHEN v.field='title' THEN v.source END) AS title_source
    FROM v_metadata v
    GROUP BY v.media_item_id
),
itm AS (
    SELECT i.id, i.cluster_id AS song_id, i.format, i.quality_rank, i.quality_verdict,
           m.artist, m.title, m.language, m.song_mbid, m.name_trust, m.title_source,
           b.integrity_status AS integrity
    FROM media_items i
    LEFT JOIN meta m ON m.item_id = i.id
    LEFT JOIN media_item_files f ON f.media_item_id = i.id AND f.role IN ('audio','av')
    LEFT JOIN blobs b ON b.content_hash = f.content_hash
    WHERE i.status = 'active' AND i.cluster_id IS NOT NULL
),
rep AS (
    SELECT song_id, id, artist, title, language, song_mbid, title_source,
           ROW_NUMBER() OVER (
               PARTITION BY song_id
               ORDER BY name_trust DESC,
                        COALESCE(quality_rank, 1000000) ASC,
                        id ASC
           ) AS rn
    FROM itm
)
SELECT
    r.song_id                                              AS song_id,
    r.artist                                               AS artist,
    r.title                                                AS title,
    r.id                                                   AS representative_item_id,
    r.title_source                                         AS name_source,
    c.method                                               AS song_method,
    c.confidence                                           AS song_confidence,
    a.copies                                               AS copies,
    a.formats                                              AS formats,
    a.distinct_names                                       AS distinct_names,
    a.has_playable                                         AS has_playable,
    a.has_decoded                                          AS has_decoded,
    a.default_item_id                                      AS default_item_id,
    COALESCE(r.language, a.any_language)                   AS language,
    CASE WHEN COALESCE(r.title, r.artist, '') GLOB '*[֐-׿]*' THEN 'hebrew'
         WHEN COALESCE(r.title, r.artist) IS NULL          THEN 'unknown'
         ELSE 'latin' END                                  AS script,
    COALESCE(r.song_mbid, a.any_song_mbid)                 AS song_mbid,
    a.distinct_mbids                                       AS distinct_mbids
FROM rep r
JOIN (
    SELECT song_id,
           COUNT(*)                                        AS copies,
           TRIM(CASE WHEN MAX(format='audio_lrc')  THEN 'audio_lrc '  ELSE '' END
             || CASE WHEN MAX(format='audio_only') THEN 'audio_only ' ELSE '' END
             || CASE WHEN MAX(format='mp3g')       THEN 'mp3g '       ELSE '' END
             || CASE WHEN MAX(format='video')      THEN 'video '      ELSE '' END)
                                                           AS formats,
           COUNT(DISTINCT CASE WHEN artist IS NOT NULL OR title IS NOT NULL
                          THEN lower(trim(COALESCE(artist,''))) || ' | '
                            || lower(trim(COALESCE(title,''))) END)
                                                           AS distinct_names,
           MAX(integrity IN ('probed_ok','decoded_ok'))     AS has_playable,
           MAX(integrity = 'decoded_ok')                    AS has_decoded,
           MIN(CASE WHEN quality_rank = 1 THEN id END)      AS default_item_id,
           MAX(language)                                    AS any_language,
           MAX(song_mbid)                                   AS any_song_mbid,
           COUNT(DISTINCT song_mbid)                        AS distinct_mbids
    FROM itm GROUP BY song_id
) a ON a.song_id = r.song_id
LEFT JOIN clusters c ON c.id = r.song_id
WHERE r.rn = 1;

-- title_cards — Stage 5 title-card OCR evidence (see karaokemp/titlecard.py).
-- 308 SONG-<uuid>.mp4 and ~300 Chapter_NN.avi locations parse to layout='opaque' with
-- artist/title NULL, so §9.1's `WHERE title IS NOT NULL` worklist never sees them. They are
-- videos with an on-screen title card in the first ~10s; reading it puts them back in that
-- worklist. The OCR result is EVIDENCE and is kept separately from what gets promoted into
-- song_metadata, so re-tuning promotion never needs a re-OCR (§11) and a run is resumable:
-- an item with a row here is done, card or no card.
--
-- The field split is the whole point. `styled_artist` is the PERFORMING artist, and only
-- ever comes from a Karaoke Channel card's `IN THE STYLE OF <name>` line. `writer_credit`
-- is an AUTHORSHIP credit -- the name in parentheses under a KaraFun title (measured: a
-- card titled "The Shoop Shoop Song (It's In His Kiss)" credits Rudy Clark, who WROTE it;
-- the famous performers are Betty Everett and Cher), or a Hebrew card's מילים: / לחן:
-- (words-by / music-by) lines. Writing an authorship credit into `artist` would silently
-- mislabel ~60 rows with a name that never performed the song, so the two live in
-- different columns and only `styled_artist` is promotable. Nothing reads writer_credit
-- except a human.
CREATE TABLE IF NOT EXISTS title_cards (
    media_item_id  INTEGER PRIMARY KEY REFERENCES media_items(id),
    location_id    INTEGER REFERENCES file_locations(id),  -- frame source
    producer       TEXT,    -- 'karaoke_channel' | 'karafun' | 'other' | 'none'
    card_found     INTEGER NOT NULL,                       -- 0/1
    title          TEXT,
    styled_artist  TEXT,    -- 'IN THE STYLE OF X' — a PERFORMING artist. Promotable.
    writer_credit  TEXT,    -- KaraFun parens / Hebrew מילים|לחן. NOT an artist. Never promoted.
    year           INTEGER,
    musical_key    TEXT,
    script         TEXT,
    frame_ts       TEXT,    -- JSON array of timestamps sent, e.g. '[2.5,5,9]'
    model          TEXT NOT NULL,
    raw_response   TEXT,    -- JSON as returned; re-tuning promotion never needs a re-OCR
    extracted_at   TEXT
) STRICT;

-- llm_parses — §6.1/§6.2 LLM filename segmentation, the durable per-stem cache behind metadata
-- source 'llm_parse'. Added by migration `007_llm_parse_source.sql` (see tools/llm_parse.py).
--
-- WHAT IT EXISTS FOR. §6.1 makes the filename this library's identity of record, and the stage1
-- regex parser answers 46,425 of 53,674 locations unambiguously. It cannot answer the rest:
-- `title_artist` / `title_only` / `title_artist_from_folder` / `disc_title_only` / `style_of`
-- (3,502 rows), plus 1,897 Hebrew `artist_title` rows where the order is a coin flip because
-- Hebrew carries no punctuation cue for it. Union 5,399 rows / 4,862 distinct stems. Migration
-- 006 measured that external catalogues cannot close this: for the Hebrew half MusicBrainz finds
-- the ARTIST 62.5% of the time but yielded a song_mbid for 4/25, iTunes matched 5.0% and Deezer
-- 2.5% — there is usually no external row at all, so the segmentation has to come from reading
-- the text. The 626 `opaque` + 1 `unparsed` locations are deliberately OUT of this workset: they
-- carry zero information in the filename and need title-card OCR or a fingerprint instead.
--
-- KEYED BY STEM, not location_id: basename minus extension, NFC, whitespace-collapsed,
-- lowercased. mp3/cdg pairs and duplicate copies across folders share a stem, so 53,674 locations
-- collapse to 29,399 stems. That is what makes a rerun cheap and `extract` resumable — a stem
-- already answered at the current PROMPT_VERSION is never re-emitted into a batch.
--
-- SURVIVES un-promotion, deliberately. `DELETE FROM song_metadata WHERE source='llm_parse'`
-- fully reverses 007's effect on the index, and this table is untouched by it: re-tuning the
-- acceptance floor (config.LLM_PARSE_FLOOR) must never require re-running the agents. Same
-- contract as mb_cache/enrich_cache (§3.10, §11) — the lesson that cost ~23h of Spotify downtime.
-- `confidence` is therefore compared against the floor at PROMOTE time, never at ingest time.
CREATE TABLE IF NOT EXISTS llm_parses (
    stem           TEXT PRIMARY KEY,  -- NFC, whitespace-collapsed, lowercased basename sans ext
    artist         TEXT,              -- NULL is a legitimate, REQUIRED answer: the prompt demands
    title          TEXT,              -- null over a guess, and title-only paths have no artist
    confidence     REAL NOT NULL,     -- 0.0-1.0, the agent's own; vs LLM_PARSE_FLOOR at promote
    layout         TEXT NOT NULL,     -- what the agent decided the path was; comparable to
                                      -- location_parses.layout, so it shows where the regex was
                                      -- WRONG rather than merely unsure
    notes          TEXT,              -- free text: mojibake fixed, folder used as artist, ...
    script         TEXT,              -- 'he' / 'latn' / 'ru' / ...; lets promote report per-script
                                      -- yield and keeps the no-transliteration rule auditable
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,     -- a bump makes every stem eligible again WITHOUT discarding
                                      -- the old answer, so a prompt change is a queryable event
    batch_id       TEXT,              -- audit trail back to the emitted JSON batch file
    parsed_at      TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS ix_llm_parses_prompt ON llm_parses(prompt_version);

-- §3.7 review_queue. resolution NULL = open.
CREATE TABLE IF NOT EXISTS review_queue (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN
                      ('metadata_match','dedup_verdict','quality_flag','pair_mismatch',
                       'parser_goldset','alignment_check')),
    media_item_id INTEGER REFERENCES media_items(id),
    cluster_id    INTEGER REFERENCES clusters(id),
    payload       TEXT,   -- JSON
    resolution    TEXT,   -- JSON; NULL = open
    created_at    TEXT,
    resolved_at   TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS ix_review_open ON review_queue(kind, resolved_at);

-- §3.8 pipeline_runs. report must include review-queue size deltas per kind (§11).
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              INTEGER PRIMARY KEY,
    stage           TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    host            TEXT,
    items_processed INTEGER,
    items_failed    INTEGER,
    tool_versions   TEXT,   -- JSON
    report          TEXT,   -- JSON
    notes           TEXT
) STRICT;

-- §3.9 artifacts — created empty now, populated by the future generation pipeline (§12).
-- Derived, disposable, regenerable from (source item + tool + params).
CREATE TABLE IF NOT EXISTS artifacts (
    id            INTEGER PRIMARY KEY,
    media_item_id INTEGER REFERENCES media_items(id),   -- source item
    kind          TEXT CHECK (kind IS NULL OR kind IN
                      ('instrumental_stem','vocal_stem','lrc','enhanced_lrc','rendered_video')),
    content_hash  TEXT REFERENCES blobs(content_hash),
    tool          TEXT,
    tool_version  TEXT,
    params        TEXT,   -- JSON; params.granularity records line- vs word-level for LRC
    quality_check TEXT,
    created_at    TEXT
) STRICT;

-- §3.10 mb_cache — every MusicBrainz/AcoustID response cached; re-runs are free.
CREATE TABLE IF NOT EXISTS mb_cache (
    query_hash    TEXT PRIMARY KEY,
    request       TEXT,
    response_json TEXT,
    fetched_at    TEXT
) STRICT;

-- spotify_cache — the §6.1 order-resolution pass, and the ONE EXCEPTION to §3.10's "raw,
-- verbatim" rule that mb_cache and enrich_cache keep. `response_json` holds the SLIM
-- projection spotify_order.Client.search stored ([{name, artists, id}] per track), because
-- that client never wrote the raw body down; the originals do not exist to import. Populated
-- from the loose <LIBRARY>/spotify/search_cache.jsonl by tools/import_spotify_cache.py, which
-- carries the full rationale.
--
-- The projection is exactly what spotify_order.resolve() reads, so SCORE_FLOOR / FIELD_FLOOR /
-- MARGIN stay retunable from cache with no network (§11) — which is the property that matters.
-- A field OUTSIDE the projection (popularity, album, ISRC, release date) is NOT recoverable
-- here and costs quota: the app is development-mode at ~700 requests/day with a ~24h
-- Retry-After, the most expensive cache in this project to rebuild.
--
-- `fetched_at` is the source file's mtime, identical on every row — per-query timestamps were
-- never recorded. It is an upper bound, not a measurement.
CREATE TABLE IF NOT EXISTS spotify_cache (
    query_hash    TEXT PRIMARY KEY,
    request       TEXT,
    response_json TEXT,
    fetched_at    TEXT
) STRICT;

-- enrich_cache — same contract again (§3.10), for the §9.1.2 catalogue passes, but keyed by
-- SOURCE as well so ONE table serves Wikidata, the MusicBrainz artist index, iTunes and
-- Deezer instead of proliferating a table per API. Added by migration
-- `006_catalogue_sources.sql`.
--
-- Raw responses are stored verbatim on purpose: re-tuning an acceptance floor must never
-- require re-querying (§11). Measured — re-scoring a Hebrew run after tightening one
-- acceptance branch read 2,969 cache hits against 88 network fetches. This is the lesson
-- that cost ~23h of Spotify downtime.
CREATE TABLE IF NOT EXISTS enrich_cache (
    source        TEXT NOT NULL,   -- 'wikidata' | 'itunes' | 'deezer' | 'mb_artist'
    query_hash    TEXT NOT NULL,   -- sha256 of the request URL
    request       TEXT,
    response_json TEXT,
    fetched_at    TEXT,
    PRIMARY KEY (source, query_hash)
) STRICT;
