#!/usr/bin/env python3
"""
Discovery service plugin for capturing and storing mesh node discovery data.

Listens on a configurable discovery channel for observation reports containing
observer GPS location and lists of discovered nodes with SNR and coordinates.
Stores data in SQLite for visualization as a heatmap in the web viewer.
"""

import asyncio
import copy
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from meshcore import EventType

from modules.service_plugins.base_service import BaseServicePlugin


class DiscoveryService(BaseServicePlugin):
    """Captures discovery channel messages and stores observation data."""

    config_section = 'Discovery'
    description = "Captures mesh node discovery data for heatmap visualization"

    def __init__(self, bot: Any):
        super().__init__(bot)

        self.channel_name = self.bot.config.get(
            self.config_section, 'channel_name', fallback='#discovery'
        ).strip()
        self.retention_days = self.bot.config.getint(
            self.config_section, 'retention_days', fallback=90
        )
        self.cleanup_interval_hours = self.bot.config.getint(
            self.config_section, 'cleanup_interval_hours', fallback=24
        )

        self._cleanup_task: Optional[asyncio.Task] = None

        # Initialize database tables
        self._init_tables()

    # ------------------------------------------------------------------
    # Database setup
    # ------------------------------------------------------------------

    def _init_tables(self) -> None:
        """Create discovery tables if they don't exist."""
        db_path = self.bot.db_manager.db_path
        try:
            with sqlite3.connect(db_path, timeout=30) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS discovery_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        observer_lat REAL NOT NULL,
                        observer_lon REAL NOT NULL,
                        timestamp INTEGER NOT NULL,
                        sender_id TEXT,
                        channel_idx INTEGER,
                        message_count INTEGER DEFAULT 0,
                        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(observer_lat, observer_lon, timestamp)
                    )
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_disc_sessions_ts
                    ON discovery_sessions(timestamp)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_disc_sessions_coords
                    ON discovery_sessions(observer_lat, observer_lon)
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS discovery_nodes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL,
                        pk_prefix TEXT NOT NULL,
                        snr REAL NOT NULL,
                        node_lat REAL,
                        node_lon REAL,
                        FOREIGN KEY (session_id) REFERENCES discovery_sessions(id) ON DELETE CASCADE
                    )
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_disc_nodes_session
                    ON discovery_nodes(session_id)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_disc_nodes_coords
                    ON discovery_nodes(node_lat, node_lon)
                ''')
                # Migration: add sender_name column if missing
                cursor.execute("PRAGMA table_info(discovery_sessions)")
                columns = [row[1] for row in cursor.fetchall()]
                if 'sender_name' not in columns:
                    cursor.execute(
                        'ALTER TABLE discovery_sessions ADD COLUMN sender_name TEXT'
                    )
                conn.commit()
            self.logger.info("Discovery tables initialized")
        except Exception as e:
            self.logger.error(f"Failed to initialize discovery tables: {e}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("Discovery service is disabled")
            return

        self.logger.info("Starting discovery service...")

        # Ensure discovery channel exists on device
        await self._ensure_channel()

        # Subscribe to channel messages
        if hasattr(self.bot, 'meshcore') and self.bot.meshcore:
            self.bot.meshcore.subscribe(
                EventType.CHANNEL_MSG_RECV, self._on_channel_message
            )
            self.logger.info("Subscribed to CHANNEL_MSG_RECV events")
        else:
            self.logger.error("Cannot subscribe to events - meshcore not available")
            return

        # Start periodic cleanup
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

        self._running = True
        self.logger.info(
            f"Discovery service started (channel={self.channel_name}, "
            f"retention={self.retention_days}d)"
        )

    async def stop(self) -> None:
        self.logger.info("Stopping discovery service...")
        self._running = False

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        self.logger.info("Discovery service stopped")

    # ------------------------------------------------------------------
    # Channel auto-add
    # ------------------------------------------------------------------

    async def _ensure_channel(self) -> None:
        """Make sure the discovery channel exists on the device."""
        if not hasattr(self.bot, 'channel_manager'):
            self.logger.warning("Channel manager not available, skipping channel auto-add")
            return

        cm = self.bot.channel_manager
        existing = cm.get_channel_number(self.channel_name)
        if existing is not None:
            self.logger.info(
                f"Discovery channel '{self.channel_name}' found at index {existing}"
            )
            return

        # Find first empty slot
        max_channels = self.bot.config.getint('Bot', 'max_channels', fallback=40)
        for idx in range(max_channels):
            info = await cm.get_channel(idx, use_cache=True)
            if info is None:
                # Empty slot found
                self.logger.info(
                    f"Adding discovery channel '{self.channel_name}' at slot {idx}"
                )
                success = await cm.add_hashtag_channel(idx, self.channel_name)
                if success:
                    self.logger.info(
                        f"Successfully added '{self.channel_name}' at slot {idx}"
                    )
                else:
                    self.logger.error(
                        f"Failed to add '{self.channel_name}' at slot {idx}"
                    )
                return

        self.logger.warning(
            f"No empty channel slots available for '{self.channel_name}'"
        )

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_channel_message(self, event, metadata=None) -> None:
        """Handle incoming channel messages, filter for discovery channel."""
        try:
            payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
            if payload is None:
                return

            channel_idx = payload.get('channel_idx', 0)
            channel_name = self.bot.channel_manager.get_channel_name(channel_idx)

            # Check if this is the discovery channel
            if channel_name.lower() != self.channel_name.lower():
                return

            text = payload.get('text', '')

            # Extract sender name from prefix ("SENDER: content")
            sender_name = None
            if ':' in text and not text.startswith('http'):
                sender_name = text.split(':', 1)[0].strip()
                text = text.split(':', 1)[1].strip()

            sender_id = payload.get('sender_id', None)

            parsed = self._parse_discovery_message(text)
            if parsed is None:
                self.logger.debug(f"Could not parse discovery message: {text[:80]}")
                return

            self._store_discovery(parsed, sender_id, channel_idx, sender_name)

        except Exception as e:
            self.logger.error(f"Error processing discovery message: {e}")

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_discovery_message(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse discovery message format: lat,lon,ts|PK:snr/lat,lon|...

        Returns dict with observer info and list of nodes, or None on failure.
        """
        text = text.strip()
        if '|' not in text:
            return None

        parts = text.split('|')
        if len(parts) < 2:
            return None

        # Parse header: lat,lon,timestamp
        header = parts[0].strip()
        header_fields = header.split(',')
        if len(header_fields) < 3:
            return None

        try:
            observer_lat = float(header_fields[0])
            observer_lon = float(header_fields[1])
            timestamp = int(header_fields[2])
        except (ValueError, IndexError):
            return None

        # Parse node entries
        nodes: List[Dict[str, Any]] = []
        for node_part in parts[1:]:
            node_part = node_part.strip()
            if not node_part:
                continue

            node = self._parse_node_entry(node_part)
            if node is not None:
                nodes.append(node)

        return {
            'observer_lat': observer_lat,
            'observer_lon': observer_lon,
            'timestamp': timestamp,
            'nodes': nodes,
        }

    @staticmethod
    def _parse_node_entry(entry: str) -> Optional[Dict[str, Any]]:
        """Parse a single node entry: PK:snr/lat,lon

        Returns dict with pk_prefix, snr, node_lat, node_lon or None.
        """
        # Split location from PK:snr
        if '/' in entry:
            pk_snr_part, coords_part = entry.split('/', 1)
        else:
            pk_snr_part = entry
            coords_part = None

        # Parse PK:snr
        if ':' not in pk_snr_part:
            return None

        pk_prefix, snr_str = pk_snr_part.split(':', 1)
        pk_prefix = pk_prefix.strip()
        try:
            snr = float(snr_str.strip())
        except ValueError:
            return None

        # Parse coordinates if present
        node_lat = None
        node_lon = None
        if coords_part:
            coord_fields = coords_part.split(',')
            if len(coord_fields) >= 2:
                try:
                    lat = float(coord_fields[0])
                    lon = float(coord_fields[1])
                    # Treat 0,0 as no location
                    if lat != 0.0 or lon != 0.0:
                        node_lat = lat
                        node_lon = lon
                except ValueError:
                    pass

        return {
            'pk_prefix': pk_prefix,
            'snr': snr,
            'node_lat': node_lat,
            'node_lon': node_lon,
        }

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _store_discovery(
        self,
        parsed: Dict[str, Any],
        sender_id: Optional[str],
        channel_idx: int,
        sender_name: Optional[str] = None,
    ) -> None:
        """Store parsed discovery data in the database."""
        db_path = self.bot.db_manager.db_path
        try:
            with sqlite3.connect(db_path, timeout=30) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                cursor = conn.cursor()

                # Upsert session (dedup by observer coords + timestamp)
                cursor.execute('''
                    INSERT INTO discovery_sessions
                        (observer_lat, observer_lon, timestamp, sender_id, channel_idx, message_count, sender_name)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(observer_lat, observer_lon, timestamp)
                    DO UPDATE SET message_count = message_count + 1,
                                 sender_name = COALESCE(excluded.sender_name, sender_name)
                ''', (
                    parsed['observer_lat'],
                    parsed['observer_lon'],
                    parsed['timestamp'],
                    sender_id,
                    channel_idx,
                    sender_name,
                ))

                # Get session id
                cursor.execute('''
                    SELECT id FROM discovery_sessions
                    WHERE observer_lat = ? AND observer_lon = ? AND timestamp = ?
                ''', (
                    parsed['observer_lat'],
                    parsed['observer_lon'],
                    parsed['timestamp'],
                ))
                row = cursor.fetchone()
                if row is None:
                    self.logger.error("Failed to retrieve session id after insert")
                    return
                session_id = row[0]

                # Insert nodes
                for node in parsed['nodes']:
                    cursor.execute('''
                        INSERT INTO discovery_nodes
                            (session_id, pk_prefix, snr, node_lat, node_lon)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        session_id,
                        node['pk_prefix'],
                        node['snr'],
                        node.get('node_lat'),
                        node.get('node_lon'),
                    ))

                conn.commit()

            self.logger.info(
                f"Stored discovery: observer=({parsed['observer_lat']},{parsed['observer_lon']}), "
                f"{len(parsed['nodes'])} node(s)"
            )
        except Exception as e:
            self.logger.error(f"Failed to store discovery data: {e}")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _periodic_cleanup(self) -> None:
        """Periodically delete old discovery data."""
        interval = self.cleanup_interval_hours * 3600
        while self._running:
            try:
                await asyncio.sleep(interval)
                self._cleanup_old_data()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Discovery cleanup error: {e}")

    def _cleanup_old_data(self) -> None:
        """Delete sessions older than retention_days."""
        cutoff = int(time.time()) - (self.retention_days * 86400)
        db_path = self.bot.db_manager.db_path
        try:
            with sqlite3.connect(db_path, timeout=30) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                cursor = conn.cursor()
                # Delete old nodes first (cascade may not work without pragma)
                cursor.execute('''
                    DELETE FROM discovery_nodes
                    WHERE session_id IN (
                        SELECT id FROM discovery_sessions WHERE timestamp < ?
                    )
                ''', (cutoff,))
                nodes_deleted = cursor.rowcount
                cursor.execute(
                    'DELETE FROM discovery_sessions WHERE timestamp < ?',
                    (cutoff,),
                )
                sessions_deleted = cursor.rowcount
                conn.commit()

            if sessions_deleted > 0 or nodes_deleted > 0:
                self.logger.info(
                    f"Discovery cleanup: removed {sessions_deleted} sessions, "
                    f"{nodes_deleted} nodes older than {self.retention_days}d"
                )
        except Exception as e:
            self.logger.error(f"Discovery cleanup failed: {e}")
