"""Tests for priority execution queue ordering and thread-safe push."""

import asyncio
import sys
import os
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestPriorityExecution:
    """Unit tests for priority queue ordering and thread-safe push."""

    def test_stale_dequeues_before_marketmake(self):
        """StalePriceOpp (weight 3.0) should dequeue before MarketMake (weight 1.0)."""
        from continuous import _execution_priority

        stale_opp = {"type": "StalePriceOpp", "net_profit": 0.05, "_clob_depth": 100.0, "total_cost": "$1.00"}
        mm_opp = {"type": "MarketMake", "net_profit": 0.05, "_clob_depth": 100.0, "total_cost": "$1.00"}

        stale_priority = -_execution_priority(stale_opp)
        mm_priority = -_execution_priority(mm_opp)

        # Lower value dequeues first (min-heap), so stale_priority < mm_priority
        assert stale_priority < mm_priority, (
            f"StalePriceOpp priority {stale_priority} should be lower than "
            f"MarketMake priority {mm_priority} (min-heap = dequeues first)"
        )

    def test_resolution_dequeues_before_binary(self):
        """ResolutionSnipeOpp (weight 2.5) should dequeue before Binary (weight 2.0)."""
        from continuous import _execution_priority

        resolution_opp = {"type": "ResolutionSnipeOpp", "net_profit": 0.05, "_clob_depth": 100.0, "total_cost": "$1.00"}
        binary_opp = {"type": "Binary", "net_profit": 0.05, "_clob_depth": 100.0, "total_cost": "$1.00"}

        res_priority = -_execution_priority(resolution_opp)
        bin_priority = -_execution_priority(binary_opp)

        assert res_priority < bin_priority, (
            f"ResolutionSnipeOpp priority {res_priority} should be lower (dequeues first) "
            f"than Binary priority {bin_priority}"
        )

    def test_priority_queue_ordering_asyncio(self):
        """asyncio.PriorityQueue correctly dequeues lower values first (min-heap)."""
        from continuous import _execution_priority

        stale_opp = {"type": "StalePriceOpp", "net_profit": 0.05, "_clob_depth": 100.0, "total_cost": "$1.00"}
        mm_opp = {"type": "MarketMake", "net_profit": 0.05, "_clob_depth": 100.0, "total_cost": "$1.00"}
        binary_opp = {"type": "Binary", "net_profit": 0.05, "_clob_depth": 100.0, "total_cost": "$1.00"}

        async def _run():
            pq = asyncio.PriorityQueue()
            seq = 0
            # Put lower-priority items first (MarketMake, Binary), then StalePriceOpp
            for opp in [mm_opp, binary_opp, stale_opp]:
                priority = -_execution_priority(opp)
                await pq.put((priority, seq, opp))
                seq += 1

            # First dequeue should be StalePriceOpp (most negative priority = highest urgency)
            first_priority, _, first_opp = await pq.get()
            return first_opp

        first = asyncio.run(_run())
        assert first["type"] == "StalePriceOpp", (
            f"Expected StalePriceOpp to dequeue first, got {first['type']}"
        )

    def test_sequence_counter_breaks_ties(self):
        """When two opps have the same type and profit, lower seq dequeues first."""
        from continuous import _execution_priority

        opp_a = {"type": "Binary", "net_profit": 0.05, "_clob_depth": 100.0, "total_cost": "$1.00"}
        opp_b = {"type": "Binary", "net_profit": 0.05, "_clob_depth": 100.0, "total_cost": "$1.00"}

        async def _run():
            pq = asyncio.PriorityQueue()
            priority = -_execution_priority(opp_a)  # same for both
            await pq.put((priority, 0, opp_a))  # seq=0 first
            await pq.put((priority, 1, opp_b))  # seq=1 second

            first_priority, first_seq, first_opp = await pq.get()
            return first_seq

        first_seq = asyncio.run(_run())
        assert first_seq == 0, f"Expected seq=0 to dequeue first, got seq={first_seq}"

    def test_thread_safe_push_via_run_coroutine_threadsafe(self):
        """asyncio.run_coroutine_threadsafe pushes from a background thread into a running loop."""
        opp_to_push = {"type": "StalePriceOpp", "net_profit": 0.1, "_clob_depth": 50.0, "total_cost": "$1.00"}
        received = []

        async def _main():
            loop = asyncio.get_event_loop()
            pq: asyncio.PriorityQueue = asyncio.PriorityQueue()

            # Push from a background thread while the event loop is running.
            # Use asyncio.to_thread so the thread has a running loop to schedule into.
            def _thread_push():
                future = asyncio.run_coroutine_threadsafe(
                    pq.put((-999.0, 0, opp_to_push)), loop
                )
                future.result(timeout=2.0)  # Block until put is scheduled

            await asyncio.to_thread(_thread_push)

            # At this point the put coroutine has been scheduled; yield to let it run
            await asyncio.sleep(0)

            assert not pq.empty(), "Queue should have one item after thread push"
            _, _, dequeued = await pq.get()
            received.append(dequeued)

        asyncio.run(_main())
        assert len(received) == 1, f"Expected 1 result, got {len(received)}"
        assert received[0]["type"] == "StalePriceOpp"


class TestWSTriggerDeduper:
    def test_rejects_same_trigger_during_cooldown(self):
        from continuous import _WSTriggerDeduper

        deduper = _WSTriggerDeduper(5.0)
        opp = {
            "type": "Cross(PM_NO + K_YES)",
            "_source": "ws_cross_pair",
            "_market_key": "example-market",
        }

        assert deduper.admit(opp, now=100.0) is True
        assert deduper.admit(opp, now=104.99) is False
        assert deduper.admit(opp, now=105.0) is True

    def test_direction_change_uses_separate_key(self):
        from continuous import _WSTriggerDeduper

        deduper = _WSTriggerDeduper(5.0)
        first = {
            "type": "Cross(PM_NO + K_YES)",
            "_source": "ws_cross_pair",
            "_market_key": "example-market",
        }
        flipped = {**first, "type": "Cross(PM_YES + K_NO)"}

        assert deduper.admit(first, now=100.0) is True
        assert deduper.admit(flipped, now=100.0) is True

    def test_forget_releases_failed_queue_claim(self):
        from continuous import _WSTriggerDeduper

        deduper = _WSTriggerDeduper(5.0)
        opp = {"type": "Binary", "market": "Example"}

        assert deduper.admit(opp, now=100.0) is True
        deduper.forget(opp)
        assert deduper.admit(opp, now=100.1) is True
