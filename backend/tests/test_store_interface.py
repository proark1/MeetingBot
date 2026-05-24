"""The in-memory store must satisfy the BotStateStore contract (Phase 0)."""

from app.store import Store, store, get_bot_state_store
from app.store_interface import BotStateStore


def test_store_instance_conforms_to_protocol():
    assert isinstance(Store(), BotStateStore)


def test_singleton_conforms_and_is_returned_by_accessor():
    assert isinstance(store, BotStateStore)
    assert get_bot_state_store() is store


def test_protocol_lists_the_bot_state_surface():
    # The contract must cover exactly the bot-lifecycle operations call sites use.
    expected = {
        "create_bot", "get_bot", "update_bot",
        "get_bot_by_share_hash", "list_bots", "delete_bot",
    }
    members = set(getattr(BotStateStore, "__protocol_attrs__", expected))
    assert expected <= members
