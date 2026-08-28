"""
Motet - Session Management

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Session management system for the Motet distributed framework.
    Provides conversation state tracking and message history management.

Dependencies:
    - typing: Type hints and annotations
    - Message type definitions
    - Conversation tracking

Usage:
    from motet.interfaces.sessions import SessionManager
    
    # Create session manager
    manager = SessionManager()
    
    # Manage session
    session = manager.get_or_create_session(session_id)

Notes:
    - Provides session state management
    - Includes message history tracking
    - Supports conversation persistence
    - Integrates with distributed architecture
"""

from __future__ import annotations

from typing import Dict, List

from ..core.types import Message


class SessionManager:
	def __init__(self, window_messages: int = 20) -> None:
		self._store: Dict[str, List[Message]] = {}
		self._window = max(2, int(window_messages))

	def get_history(self, conversation_id: str) -> List[Message]:
		return list(self._store.get(conversation_id, []))

	def append_turn(self, conversation_id: str, user_messages: List[Message], assistant_text: str) -> None:
		hist = self._store.setdefault(conversation_id, [])
		for m in user_messages:
			if m.role == "user":
				hist.append(m)
		if assistant_text:
			hist.append(Message(role="assistant", content=assistant_text))
		if len(hist) > self._window:
			self._store[conversation_id] = hist[-self._window :]


__all__ = ["SessionManager"]


