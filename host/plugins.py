#!/usr/bin/env python3
"""host/plugins.py -- message-type -> handler registry for Remote v4 host.

PluginRegistry maps each wire message type to a (handler, perm_flag) pair.
The handler signature stays (session, payload) -> bool, where a True return
means "this session is done" (disconnect). perm_flag is one of
remote_proto.PERMISSION_FLAGS, or None for messages every authenticated
client may send (PING, DISCONNECT, CHAT_MSG, AGENT_QUERY, DISPLAYS_QUERY,
PERMS_*, NET_STATUS, POLICY_GET).

Permission mapping lives in the registry; host/remote_host.py's _perm_check
consults it (with the special cases the protocol demands: INPUT event-type
granularity, SYSTEM_CMD launch-app -> "apps", policy master switches).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "common"))

import remote_proto as proto  # noqa: E402

LOG = logging.getLogger("remote-host")


class PluginRegistry:
    """Registry of wire handlers with their permission flags."""

    def __init__(self):
        self._handlers = {}  # mtype -> (handler, perm_flag_or_None)

    def register(self, mtype, handler, perm_flag_or_None=None):
        """Register handler(session, payload)->bool for mtype.

        perm_flag_or_None must be None or one of proto.PERMISSION_FLAGS
        (validated here so a typo fails loudly at startup, not at runtime).
        """
        if (perm_flag_or_None is not None
                and perm_flag_or_None not in proto.PERMISSION_FLAGS):
            raise ValueError("unknown permission flag %r for mtype 0x%02x"
                             % (perm_flag_or_None, mtype))
        if mtype in self._handlers:
            LOG.warning("re-registering handler for mtype 0x%02x", mtype)
        self._handlers[mtype] = (handler, perm_flag_or_None)

    def lookup(self, mtype):
        """Return (handler, perm_flag_or_None), or (None, None) if unknown."""
        return self._handlers.get(mtype, (None, None))

    def permission_for(self, mtype):
        """The permission flag registered for mtype, or None."""
        _handler, flag = self.lookup(mtype)
        return flag

    def registered_types(self):
        return sorted(self._handlers.keys())
