"""Tests for DataStore integration in ProtocolContext."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from cubos.data.data_store import DataStore
from cubos.protocol_engine.runtime import ProtocolContext


class TestProtocolContextDataStore:


    def test_accepts_data_store(self):
        store = DataStore(db_path=":memory:")
        cid = store.create_campaign(description="test")
        ctx = ProtocolContext(
            gantry=MagicMock(),
            deck=MagicMock(),
            data_store=store,
            campaign_id=cid,
            fluid_state_id=12,
        )
        assert ctx.data_store is store
        assert ctx.campaign_id == cid
        assert ctx.fluid_state_id == 12
        store.close()

    def test_no_errors_without_data_store(self):
        ctx = ProtocolContext(
            gantry=MagicMock(),
            deck=MagicMock(),
            logger=logging.getLogger("test"),
        )
        assert ctx.gantry is not None
        assert ctx.deck is not None
        assert ctx.data_store is None
