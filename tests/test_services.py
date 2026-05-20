from __future__ import annotations

import asyncio

from api.app.services import APIKeyPool, normalize_proxy_url


def test_normalize_proxy_url():
    assert normalize_proxy_url("socks://127.0.0.1:2080") == "socks5://127.0.0.1:2080"
    assert normalize_proxy_url("socks5://127.0.0.1:2080") == "socks5://127.0.0.1:2080"
    assert normalize_proxy_url("http://127.0.0.1:2080") == "http://127.0.0.1:2080"


def test_key_pool_rotation_and_cooldown():
    async def _run():
        pool = APIKeyPool(["k1", "k2", "k3"], cooldown_sec=10)

        first = await pool.next_candidates()
        second = await pool.next_candidates()

        assert first[0].slot == 0
        assert second[0].slot == 1

        pool.mark_failure(second[0], 429, "limited", 10)
        third = await pool.next_candidates()
        assert third[0].slot != 1

    asyncio.run(_run())
