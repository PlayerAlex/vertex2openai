"""Console session storage with optional, restart-safe device sessions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import tempfile
import threading
import time
from typing import Callable


class SessionStore:
    """Store only token hashes and API key fingerprints on disk.

    Ordinary browser sessions stay in memory. Sessions created with ``remember``
    are persisted atomically and can survive a server restart.
    """

    def __init__(
        self,
        state_file: str,
        *,
        session_ttl: int = 12 * 60 * 60,
        remembered_ttl: int = 30 * 24 * 60 * 60,
        max_sessions: int = 200,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.state_file = state_file
        self.session_ttl = session_ttl
        self.remembered_ttl = remembered_ttl
        self.max_sessions = max_sessions
        self._time = time_fn
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}
        self._load()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _key_fingerprint(api_key: str) -> str:
        return hashlib.sha256(("console-session-key\0" + api_key).encode("utf-8")).hexdigest()

    def _prune_locked(self, now: float) -> bool:
        changed = False
        for token_hash, record in list(self._sessions.items()):
            if float(record.get("expires_at", 0)) <= now:
                self._sessions.pop(token_hash, None)
                changed = True
        if len(self._sessions) > self.max_sessions:
            oldest = sorted(
                self._sessions.items(), key=lambda item: float(item[1].get("created_at", 0))
            )[: len(self._sessions) - self.max_sessions]
            for token_hash, _ in oldest:
                self._sessions.pop(token_hash, None)
                changed = True
        return changed

    def _load(self) -> None:
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return
        records = data.get("sessions", {}) if isinstance(data, dict) else {}
        if not isinstance(records, dict):
            return
        with self._lock:
            for token_hash, record in records.items():
                if (
                    isinstance(token_hash, str)
                    and isinstance(record, dict)
                    and record.get("persistent") is True
                ):
                    try:
                        if not all(math.isfinite(float(record[k])) for k in ("expires_at", "created_at")):
                            continue
                    except (KeyError, ValueError, TypeError, OverflowError):
                        continue
                    if not isinstance(record.get("key_fingerprint"), str):
                        continue
                    self._sessions[token_hash] = record
            changed = self._prune_locked(self._time())
            if changed:
                self._save_locked()

    def _save_locked(self) -> None:
        target_dir = os.path.dirname(self.state_file) or "."
        try:
            os.makedirs(target_dir, exist_ok=True)
            payload = {
                "version": 1,
                "sessions": {
                    token_hash: record
                    for token_hash, record in self._sessions.items()
                    if record.get("persistent") is True
                },
            }
            fd, tmp_path = tempfile.mkstemp(prefix=".console-sessions-", suffix=".tmp", dir=target_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, self.state_file)
                try:
                    os.chmod(self.state_file, 0o600)
                except OSError:
                    pass
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            print(f"⚠️ [控制台会话] 无法保存记住的设备，已降级为当前进程有效: {exc}")

    def issue(self, api_key: str, *, remember: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        now = self._time()
        record = {
            "created_at": now,
            "expires_at": now + (self.remembered_ttl if remember else self.session_ttl),
            "key_fingerprint": self._key_fingerprint(api_key),
            "persistent": bool(remember),
        }
        with self._lock:
            changed = self._prune_locked(now)
            self._sessions[self._token_hash(token)] = record
            changed = self._prune_locked(now) or changed
            if remember or changed:
                self._save_locked()
        return token

    def revoke(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            record = self._sessions.pop(self._token_hash(token), None)
            if record and record.get("persistent") is True:
                self._save_locked()

    def is_remembered(self, token: str) -> bool:
        with self._lock:
            record = self._sessions.get(self._token_hash(token), {})
            return record.get("persistent") is True

    def is_valid(self, token: str, api_key: str) -> bool:
        if not token:
            return False
        now = self._time()
        with self._lock:
            token_hash = self._token_hash(token)
            record = self._sessions.get(token_hash)
            if record is None:
                return False
            expired = float(record.get("expires_at", 0)) <= now
            key_matches = secrets.compare_digest(
                str(record.get("key_fingerprint", "")), self._key_fingerprint(api_key)
            )
            if expired or not key_matches:
                self._sessions.pop(token_hash, None)
                if record.get("persistent") is True:
                    self._save_locked()
                return False
            return True
