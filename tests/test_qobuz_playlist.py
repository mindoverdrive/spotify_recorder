import json
import os
import sqlite3
import tempfile
import unittest

from qobuz_playlist import (
    list_qobuz_playlists,
    scan_qobuz_playlist,
    write_playlist_m3u8,
)
from recording_catalog import get_qobuz_playlist_job
from qobuz_playlist_job import QobuzPlaylistJob


def create_playlist_fixture(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE S_Playlist (
                id INTEGER PRIMARY KEY, synchronized_at TEXT
            );
            CREATE TABLE S_Playlist_Track (
                playlist_id INTEGER, playlist_track_id INTEGER,
                sort INTEGER, synchronization_id TEXT, track_id INTEGER
            );
            CREATE TABLE S_Track (
                id INTEGER PRIMARY KEY, title TEXT, track_artists_names TEXT,
                release_name TEXT, duration REAL, data TEXT
            );
            CREATE TABLE L_Track (
                track_id INTEGER, data TEXT, status TEXT, format INTEGER,
                sampling_rate REAL, bit_depth INTEGER, duration REAL,
                is_completed INTEGER
            );
            CREATE TABLE L_Playlist (id INTEGER, name TEXT, is_completed INTEGER);
            """
        )
        connection.execute(
            "INSERT INTO S_Playlist VALUES (10, '2026-07-20T00:00:00Z')"
        )
        connection.execute("INSERT INTO L_Playlist VALUES (10, 'DJ Set', 1)")
        tracks = [
            (101, "First", "Artist A", "Album", 180.0, 96000, 24, 1),
            (102, "Second", "Artist B", "Album", 200.0, 44100, 16, 1),
            (103, "Third", "Artist C", "Album", 220.0, 48000, 24, 0),
        ]
        for order, (track_id, title, artist, album, duration, rate, depth, complete) in enumerate(tracks):
            connection.execute(
                "INSERT INTO S_Playlist_Track VALUES (?, ?, ?, ?, ?)",
                (10, order + 1, order, "sync", track_id),
            )
            connection.execute(
                "INSERT INTO S_Track VALUES (?, ?, ?, ?, ?, ?)",
                (track_id, title, artist, album, duration, "{}"),
            )
            connection.execute(
                "INSERT INTO L_Track VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    track_id,
                    json.dumps(
                        {
                            "title": title,
                            "performer": {"name": artist},
                            "album": {"title": album, "id": f"album-{track_id}"},
                            "track_number": order + 1,
                            "media_number": 1,
                            "maximum_channel_count": 2,
                        }
                    ),
                    "Import",
                    6 if depth == 16 else 7,
                    rate / 1000.0,
                    depth,
                    duration,
                    complete,
                ),
            )


class QobuzPlaylistScanTests(unittest.TestCase):
    def test_lists_and_blocks_incomplete_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "qobuz.db")
            create_playlist_fixture(database)
            playlists = list_qobuz_playlists(database)
            scan = scan_qobuz_playlist("10", database_path=database)

        self.assertEqual(playlists[0].name, "DJ Set")
        self.assertEqual(playlists[0].track_count, 3)
        self.assertFalse(scan.can_start)
        self.assertEqual(len(scan.blocking_tracks), 1)
        self.assertIn("完全ダウンロード", " ".join(scan.blocking_tracks[0].issues))

    def test_exclusion_unblocks_and_execution_is_grouped_by_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "qobuz.db")
            create_playlist_fixture(database)
            scan = scan_qobuz_playlist(
                "10", excluded_track_ids={"103"}, database_path=database
            )

        self.assertTrue(scan.can_start)
        self.assertEqual([item.track_id for item in scan.execution_tracks()], ["102", "101"])
        self.assertEqual([item.track_id for item in scan.tracks], ["101", "102", "103"])
        self.assertEqual(scan.rate_groups, {44100: 1, 96000: 1})

    def test_job_catalog_keeps_original_and_execution_order(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "qobuz.db")
            catalog = os.path.join(directory, "catalog.sqlite3")
            create_playlist_fixture(database)
            scan = scan_qobuz_playlist(
                "10", excluded_track_ids={"103"}, database_path=database
            )
            job = QobuzPlaylistJob(scan, catalog, directory, job_id="job-1")
            job.create()
            stored = get_qobuz_playlist_job("job-1", catalog)

        self.assertEqual([item["track_id"] for item in stored["execution"]], ["102", "101"])
        self.assertEqual([item["track_id"] for item in stored["tracks"]], ["101", "102", "103"])
        self.assertEqual(stored["tracks"][2]["status"], "excluded")

    def test_m3u8_is_written_in_original_order(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "qobuz.db")
            create_playlist_fixture(database)
            scan = scan_qobuz_playlist(
                "10", excluded_track_ids={"103"}, database_path=database
            )
            first = os.path.join(directory, "first.flac")
            second = os.path.join(directory, "second.flac")
            target = write_playlist_m3u8(
                os.path.join(directory, "playlist.m3u8"),
                scan,
                {"101": first, "102": second},
            )
            with open(target, "r", encoding="utf-8") as handle:
                text = handle.read()

        self.assertLess(text.index("Artist A - First"), text.index("Artist B - Second"))


if __name__ == "__main__":
    unittest.main()
