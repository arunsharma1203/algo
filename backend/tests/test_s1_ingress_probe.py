"""
Unit tests and synthetic workload benchmark for Phase 1 (Measurement-Only)
of the S1 prospective data-recording programme (`app.data.s1_ingress_probe`).

Verifies:
  1. Strict Default-OFF state and runtime enable/disable without stream restart.
  2. Exact wire-level protobuf decoding & downstream payload equivalence.
  3. Frame sizes, msg/s, bytes/s, 100ms & 1s bursts, instruments/frame, and coverage.
  4. Separate S1 cohort accounting (NIFTY, INDIAVIX, RELIANCE, HDFCBANK, ICICIBANK,
     NIFTY 1-7 DTE options, other DTE options, futures, non-S1) and raw un-carried
     book-bearing vs book-only vs LTPC-only classification.
  5. Bounded memory rings & seen-key caps with explicit eviction/drop counters.
  6. Fail-open fault isolation & auto-disable without impacting live trading dispatch.
  7. Zero changes to subscriptions, broker-request counts, timestamps, S2_05 hashes,
     or simulation/live arming state.
"""

import copy
import datetime
import gc
import hashlib
import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import upstox_client
from google.protobuf import json_format
from upstox_client.feeder.proto import MarketDataFeedV3_pb2

from app.data.s1_ingress_probe import (
    BoundedHistogram,
    LATENCY_BOUNDS_US,
    LIFECYCLE_RING_MAXLEN,
    MAX_CONSECUTIVE_PROBE_FAULTS,
    MAX_TRACKED_SEEN_KEYS,
    RING_100MS_MAXLEN,
    RING_1M_MAXLEN,
    RING_1S_MAXLEN,
    S1_CORE_INSTRUMENT_KEYS,
    S1IngressMeasurementProbe,
    attach_probe_lifecycle_listeners,
    build_instrumented_streamer_class,
)
from app.data.upstox_provider import get_upstox_limiter
from app.data.upstox_stream_service import UpstoxStreamService


def encode_v3_feed_response(wire_dict: Dict[str, Any]) -> bytes:
    msg = MarketDataFeedV3_pb2.FeedResponse()
    json_format.ParseDict(wire_dict, msg)
    return msg.SerializeToString()


def make_dummy_streamer(probe: S1IngressMeasurementProbe, initial_keys: List[str]):
    cfg = upstox_client.Configuration()
    cfg.access_token = "DUMMY_TEST_TOKEN_DO_NOT_CONNECT"
    client = upstox_client.ApiClient(cfg)
    cls = build_instrumented_streamer_class(upstox_client.MarketDataStreamerV3, probe)
    streamer = cls(client, initial_keys, mode="full")
    attach_probe_lifecycle_listeners(streamer, probe)
    return streamer


def build_synthetic_protobuf_frame(
    target_bytes: int,
) -> Tuple[bytes, int, Set[str], Dict[str, Dict[str, Any]]]:
    """
    Builds a realistic multi-instrument Upstox V3 protobuf frame (S1 core + NIFTY options
    + non-S1 equities) tuned to `>= target_bytes` (e.g., 1 KiB, 16 KiB, 64 KiB).
    """
    today_iso = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    feeds: Dict[str, Any] = {}
    key_to_contract: Dict[str, Dict[str, Any]] = {}
    subscribed: Set[str] = set(S1_CORE_INSTRUMENT_KEYS.keys())

    feeds["NSE_INDEX|Nifty 50"] = {
        "fullFeed": {
            "indexFF": {
                "ltpc": {"ltp": 24180.5, "ltt": "1763030000000", "cp": 24050.0},
                "marketOHLC": {
                    "ohlc": [
                        {
                            "interval": "I1",
                            "open": 24170.0,
                            "high": 24185.0,
                            "low": 24168.0,
                            "close": 24180.5,
                            "ts": "1763029980000",
                        }
                    ]
                },
            }
        }
    }
    feeds["NSE_INDEX|India VIX"] = {
        "fullFeed": {"indexFF": {"ltpc": {"ltp": 14.12, "ltt": "1763030000000", "cp": 14.40}}}
    }

    idx = 0
    while True:
        wire = {"type": "live_feed", "currentTs": "1763030000000", "feeds": feeds}
        raw = encode_v3_feed_response(wire)
        if len(raw) >= target_bytes or idx >= 600:
            return raw, len(feeds), subscribed, key_to_contract

        if idx < 22:
            ikey = f"NSE_FO|{52000 + idx}"
            key_to_contract[ikey] = {
                "instrument_key": ikey,
                "underlying": "NIFTY",
                "expiry": today_iso,
                "dte": 3,
            }
        elif idx == 22:
            ikey = "NSE_EQ|INE002A01018"
        elif idx == 23:
            ikey = "NSE_EQ|INE040A01034"
        elif idx == 24:
            ikey = "NSE_EQ|INE090A01021"
        else:
            ikey = f"NSE_EQ|INE{100000 + idx:06d}01"

        subscribed.add(ikey)
        feeds[ikey] = {
            "fullFeed": {
                "marketFF": {
                    "ltpc": {
                        "ltp": 150.25 + (idx % 50),
                        "ltt": "1763030000000",
                        "ltq": "75",
                        "cp": 148.0,
                    },
                    "atp": 149.8,
                    "vtt": "125000",
                    "oi": 320000,
                    "tbq": 18000.0,
                    "tsq": 16500.0,
                    "optionGreeks": {
                        "delta": 0.48,
                        "gamma": 0.0011,
                        "theta": -7.9,
                        "vega": 10.4,
                    },
                    "marketLevel": {
                        "bidAskQuote": [
                            {
                                "bidP": 150.10 - lvl * 0.05,
                                "askP": 150.40 + lvl * 0.05,
                                "bidQ": "150",
                                "askQ": "150",
                            }
                            for lvl in range(5)
                        ]
                    },
                    "marketOHLC": {
                        "ohlc": [
                            {
                                "interval": "I1",
                                "open": 149.5,
                                "high": 151.0,
                                "low": 149.0,
                                "close": 150.25,
                                "vol": "4200",
                                "ts": "1763029980000",
                            }
                        ]
                    },
                }
            }
        }
        idx += 1


class TestS1IngressProbe(unittest.TestCase):
    def test_01_default_off_and_runtime_toggle_without_reconnect(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S1_INGRESS_PROBE_ENABLED", None)
            probe = S1IngressMeasurementProbe()
            self.assertFalse(probe.enabled)

        keys = ["NSE_INDEX|Nifty 50", "NSE_EQ|INE002A01018"]
        streamer = make_dummy_streamer(probe, keys)

        connect_calls = 0
        subscribe_calls = 0

        def _forbid_connect(*args, **kwargs):
            nonlocal connect_calls
            connect_calls += 1

        def _forbid_subscribe(*args, **kwargs):
            nonlocal subscribe_calls
            subscribe_calls += 1

        streamer.connect = _forbid_connect
        streamer.subscribe = _forbid_subscribe

        received_dicts: List[Dict[str, Any]] = []
        streamer.on("message", lambda d: received_dicts.append(copy.deepcopy(d)))

        wire = {
            "type": "live_feed",
            "currentTs": "1763030000000",
            "feeds": {
                "NSE_INDEX|Nifty 50": {
                    "fullFeed": {
                        "indexFF": {"ltpc": {"ltp": 24150.25, "ltt": "1763030000000", "cp": 24000.0}}
                    }
                },
                "NSE_EQ|INE002A01018": {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 1295.5, "ltt": "1763030000000", "cp": 1280.0},
                            "marketLevel": {
                                "bidAskQuote": [
                                    {"bidP": 1295.4, "askP": 1295.6, "bidQ": "250", "askQ": "300"}
                                ]
                            },
                        }
                    }
                },
            },
        }
        raw = encode_v3_feed_response(wire)

        # 1. Default-OFF: zero frames recorded, downstream receives exact decoded message
        streamer.handle_message(None, raw)
        self.assertEqual(len(received_dicts), 1)
        self.assertEqual(probe.get_compact_status()["total_frames"], 0)

        # Compare against uninstrumented base MarketDataStreamerV3
        base_received: List[Dict[str, Any]] = []
        cfg = upstox_client.Configuration()
        cfg.access_token = "DUMMY_TEST_TOKEN_DO_NOT_CONNECT"
        base_streamer = upstox_client.MarketDataStreamerV3(
            upstox_client.ApiClient(cfg), keys, mode="full"
        )
        base_streamer.on("message", lambda d: base_received.append(copy.deepcopy(d)))
        base_streamer.handle_message(None, raw)
        self.assertEqual(received_dicts[0], base_received[0])

        # 2. Enable at runtime WITHOUT reconnecting or re-subscribing
        probe.enable()
        self.assertTrue(probe.enabled)
        streamer.handle_message(None, raw)
        self.assertEqual(len(received_dicts), 2)
        self.assertEqual(received_dicts[1], base_received[0])
        self.assertEqual(probe.get_compact_status()["total_frames"], 1)
        self.assertEqual(probe.get_compact_status()["total_raw_bytes"], len(raw))

        # 3. Disable at runtime WITHOUT reconnecting or re-subscribing
        probe.disable()
        self.assertFalse(probe.enabled)
        streamer.handle_message(None, raw)
        self.assertEqual(len(received_dicts), 3)
        self.assertEqual(received_dicts[2], base_received[0])
        self.assertEqual(probe.get_compact_status()["total_frames"], 1)

        self.assertEqual(connect_calls, 0)
        self.assertEqual(subscribe_calls, 0)

    def test_02_s1_cohorts_book_bearing_and_coverage_accounting(self):
        probe = S1IngressMeasurementProbe(default_enabled=False)
        today = datetime.date.today()
        exp_4d = (today + datetime.timedelta(days=4)).isoformat()
        exp_10d = (today + datetime.timedelta(days=10)).isoformat()

        opt_a = "NSE_FO|51001"    # 4 DTE, full 2-sided L5 book + LTP + OI + Greeks
        opt_b = "NSE_FO|51002"    # 4 DTE, book-only (no ltpc)
        opt_c = "NSE_FO|51003"    # 4 DTE, LTPC-only (no bidAskQuote)
        opt_d = "NSE_FO|51004"    # 4 DTE, crossed invalid book
        opt_far = "NSE_FO|59999"  # 10 DTE (>7 DTE)
        fut_nifty = "NSE_FO|60001"
        non_s1_eq = "NSE_EQ|INE009A01021"
        unseen_opt = "NSE_FO|51099"

        subscribed = set(S1_CORE_INSTRUMENT_KEYS.keys()) | {
            opt_a, opt_b, opt_c, opt_d, unseen_opt, opt_far, fut_nifty, non_s1_eq, "NSE_INDEX|Nifty Bank"
        }
        key_to_contract = {
            opt_a: {"instrument_key": opt_a, "underlying": "NIFTY", "expiry": exp_4d, "dte": 4},
            opt_b: {"instrument_key": opt_b, "underlying": "NIFTY", "expiry": exp_4d, "dte": 4},
            opt_c: {"instrument_key": opt_c, "underlying": "NIFTY", "expiry": exp_4d, "dte": 4},
            opt_d: {"instrument_key": opt_d, "underlying": "NIFTY", "expiry": exp_4d, "dte": 4},
            unseen_opt: {"instrument_key": unseen_opt, "underlying": "NIFTY", "expiry": exp_4d, "dte": 4},
            opt_far: {"instrument_key": opt_far, "underlying": "NIFTY", "expiry": exp_10d, "dte": 10},
            fut_nifty: {"instrument_key": fut_nifty, "underlying": "NIFTY", "is_futures": True},
        }
        probe.set_universe_supplier(lambda: (subscribed, key_to_contract))
        probe.enable(reset_counters=True)

        streamer = make_dummy_streamer(probe, list(subscribed))
        now_ms = int(time.time() * 1000)

        wire = {
            "type": "live_feed",
            "currentTs": str(now_ms - 12),
            "feeds": {
                "NSE_INDEX|Nifty 50": {
                    "fullFeed": {
                        "indexFF": {
                            "ltpc": {"ltp": 24200.0, "ltt": str(now_ms - 15)},
                            "marketOHLC": {
                                "ohlc": [
                                    {
                                        "interval": "I1",
                                        "open": 24190.0,
                                        "high": 24205.0,
                                        "low": 24188.0,
                                        "close": 24200.0,
                                        "ts": str(now_ms - 60000),
                                    }
                                ]
                            },
                        }
                    }
                },
                "NSE_INDEX|India VIX": {
                    "fullFeed": {"indexFF": {"ltpc": {"ltp": 14.35, "ltt": str(now_ms - 20)}}}
                },
                "NSE_EQ|INE002A01018": {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 1300.0, "ltt": str(now_ms - 10)},
                            "marketLevel": {
                                "bidAskQuote": [
                                    {"bidP": 1299.8, "askP": 1300.2, "bidQ": "100", "askQ": "120"}
                                    for _ in range(5)
                                ]
                            },
                            "marketOHLC": {
                                "ohlc": [
                                    {
                                        "interval": "I1",
                                        "open": 1298.0,
                                        "high": 1301.0,
                                        "low": 1297.5,
                                        "close": 1300.0,
                                        "vol": "12000",
                                        "ts": str(now_ms - 60000),
                                    }
                                ]
                            },
                        }
                    }
                },
                "NSE_EQ|INE040A01034": {
                    "fullFeed": {"marketFF": {"ltpc": {"ltp": 1750.0, "ltt": str(now_ms - 10)}}}
                },
                "NSE_EQ|INE090A01021": {
                    "fullFeed": {"marketFF": {"ltpc": {"ltp": 1280.0, "ltt": str(now_ms - 10)}}}
                },
                opt_a: {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 110.5, "ltt": str(now_ms - 5)},
                            "oi": 245000,
                            "optionGreeks": {"delta": 0.52, "gamma": 0.0012, "theta": -8.4, "vega": 11.2},
                            "marketLevel": {
                                "bidAskQuote": [
                                    {"bidP": 110.35, "askP": 110.65, "bidQ": "150", "askQ": "225"}
                                    for _ in range(5)
                                ]
                            },
                        }
                    }
                },
                opt_b: {
                    "fullFeed": {
                        "marketFF": {
                            "marketLevel": {
                                "bidAskQuote": [
                                    {"bidP": 84.10, "askP": 84.40, "bidQ": "75", "askQ": "75"}
                                ]
                            }
                        }
                    }
                },
                opt_c: {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 62.0, "ltt": str(now_ms - 8)},
                            "oi": 180000,
                        }
                    }
                },
                opt_d: {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 45.0, "ltt": str(now_ms - 8)},
                            "marketLevel": {
                                "bidAskQuote": [
                                    {"bidP": 46.0, "askP": 45.0, "bidQ": "75", "askQ": "75"}
                                ]
                            },
                        }
                    }
                },
                opt_far: {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 195.0, "ltt": str(now_ms - 8)},
                            "marketLevel": {
                                "bidAskQuote": [{"bidP": 194.5, "askP": 195.5, "bidQ": "65", "askQ": "65"}]
                            },
                        }
                    }
                },
                fut_nifty: {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 24255.0, "ltt": str(now_ms - 6)},
                            "oi": 9800000,
                            "marketLevel": {
                                "bidAskQuote": [{"bidP": 24254.0, "askP": 24256.0, "bidQ": "130", "askQ": "130"}]
                            },
                        }
                    }
                },
                non_s1_eq: {
                    "fullFeed": {"marketFF": {"ltpc": {"ltp": 1820.0, "ltt": str(now_ms - 10)}}}
                },
            },
        }

        raw = encode_v3_feed_response(wire)
        streamer.handle_message(None, raw)

        summary = probe.get_summary(include_recent_rings=True)
        cohorts = summary["cohorts"]

        self.assertEqual(summary["ingress_totals"]["frames"], 1)
        self.assertEqual(summary["ingress_totals"]["raw_bytes"], len(raw))
        self.assertEqual(summary["ingress_totals"]["instrument_updates"], 12)
        self.assertEqual(summary["ingress_totals"]["s1_relevant_updates"], 11)
        self.assertEqual(summary["ingress_totals"]["envelope_types"]["live_feed"], 1)

        self.assertEqual(cohorts["NIFTY"]["updates"], 1)
        self.assertEqual(cohorts["NIFTY"]["i1_candle_updates"], 1)
        self.assertEqual(cohorts["INDIAVIX"]["updates"], 1)
        self.assertEqual(cohorts["RELIANCE"]["updates"], 1)
        self.assertEqual(cohorts["RELIANCE"]["book_updates"], 1)
        self.assertEqual(cohorts["RELIANCE"]["book_l5_updates"], 1)
        self.assertEqual(cohorts["RELIANCE"]["i1_candle_updates"], 1)
        self.assertEqual(cohorts["HDFCBANK"]["updates"], 1)
        self.assertEqual(cohorts["ICICIBANK"]["updates"], 1)

        s1_opt = cohorts["NIFTY_OPTIONS_1_7_DTE"]
        self.assertEqual(s1_opt["updates"], 4)
        self.assertEqual(s1_opt["ltpc_updates"], 3)
        self.assertEqual(s1_opt["book_updates"], 2)
        self.assertEqual(s1_opt["book_2sided_qty_updates"], 2)
        self.assertEqual(s1_opt["book_l5_updates"], 1)
        self.assertEqual(s1_opt["book_only_no_ltp_updates"], 1)
        self.assertEqual(s1_opt["book_invalid_or_crossed_updates"], 1)
        self.assertEqual(s1_opt["oi_updates"], 2)
        self.assertEqual(s1_opt["greeks_updates"], 1)

        self.assertEqual(cohorts["NIFTY_OPTIONS_OTHER_DTE"]["updates"], 1)
        self.assertEqual(cohorts["NIFTY_OPTIONS_OTHER_DTE"]["book_updates"], 1)
        self.assertEqual(cohorts["NIFTY_FUTURES"]["updates"], 1)
        self.assertEqual(cohorts["NIFTY_FUTURES"]["book_updates"], 1)
        self.assertEqual(cohorts["NON_S1_UNIVERSE"]["updates"], 1)

        cov = summary["delivery_coverage"]
        self.assertEqual(cov["s1_core_missing_keys"], [])
        self.assertEqual(cov["nifty_options_1_7_dte_subscribed"], 5)
        self.assertEqual(cov["nifty_options_1_7_dte_delivered"], 4)
        self.assertEqual(cov["nifty_options_book_delivered"], 3)

        codex = summary["codex_feasibility_metrics"]
        self.assertTrue(codex["slot_64kib_fit"]["fits_64kib_slot"])
        self.assertEqual(
            codex["s1_universe_and_book_provenance"]["provider_current_ts_present_ratio"], 1.0
        )
        self.assertEqual(
            codex["s1_universe_and_book_provenance"]["dedicated_quote_exchange_ts_observed_updates"],
            0,
        )

    def test_03_burst_windows_and_bounded_memory_invariance(self):
        probe = S1IngressMeasurementProbe(default_enabled=True)
        base_ns = 10_000_000_000_000
        base_ms = 1_763_030_000_000

        total_slots = RING_100MS_MAXLEN + 50
        for i in range(total_slots):
            t0 = base_ns + i * 100_000_000
            wall_ms = base_ms + i * 100
            burst_count = 5 if (i % 10 == 0) else 1
            for b in range(burst_count):
                base_k = (i * 10 + b) * 8
                feeds_map = {
                    f"NSE_EQ|SYNTH_{base_k + off}": {"fullFeed": {"marketFF": {"ltpc": {"ltp": 100.0}}}}
                    for off in range(8)
                }
                data_dict = {
                    "type": "live_feed",
                    "currentTs": str(wall_ms),
                    "feeds": feeds_map,
                }
                probe.record_ingress_frame(
                    raw_bytes=1024,
                    is_bytes=True,
                    data_dict=data_dict,
                    recv_wall_ms=wall_ms,
                    t0_ns=t0 + b * 10_000,
                    t1_ns=t0 + b * 10_000 + 4_000,
                    t2_ns=t0 + b * 10_000 + 9_000,
                    t3_ns=t0 + b * 10_000 + 15_000,
                )

        for s in range(RING_1S_MAXLEN + 20):
            t0 = base_ns + (total_slots * 100_000_000) + (s + 1) * 1_000_000_000
            wall_ms = base_ms + 100_000 + s * 1000
            probe.record_ingress_frame(
                raw_bytes=512,
                is_bytes=True,
                data_dict={"type": "live_feed", "feeds": {"NSE_INDEX|Nifty 50": {}}},
                recv_wall_ms=wall_ms,
                t0_ns=t0,
                t1_ns=t0 + 2_000,
                t2_ns=t0 + 5_000,
                t3_ns=t0 + 8_000,
            )

        for m in range(RING_1M_MAXLEN + 15):
            t0 = base_ns + (total_slots * 100_000_000) + 2_000_000_000_000 + (m + 1) * 60_000_000_000
            wall_ms = base_ms + 3_000_000 + (m + 2) * 60_000
            probe.record_ingress_frame(
                raw_bytes=512,
                is_bytes=True,
                data_dict={"type": "live_feed", "feeds": {"NSE_INDEX|Nifty 50": {}}},
                recv_wall_ms=wall_ms,
                t0_ns=t0,
                t1_ns=t0 + 2_000,
                t2_ns=t0 + 5_000,
                t3_ns=t0 + 8_000,
            )

        summary = probe.get_summary(include_recent_rings=True)
        bounds = summary["failures_and_bounds"]
        peaks = summary["burst_peaks"]

        self.assertEqual(peaks["peak_100ms_frames"], 5)
        self.assertEqual(peaks["peak_100ms_bytes"], 5 * 1024)
        self.assertEqual(bounds["ring_100ms_len"], RING_100MS_MAXLEN)
        self.assertGreater(bounds["ring_100ms_evictions"], 0)
        self.assertEqual(bounds["ring_1s_len"], RING_1S_MAXLEN)
        self.assertGreater(bounds["ring_1s_evictions"], 0)
        self.assertEqual(bounds["ring_1m_len"], RING_1M_MAXLEN)
        self.assertGreater(bounds["ring_1m_evictions"], 0)
        self.assertEqual(summary["delivery_coverage"]["delivered_unique_keys"], MAX_TRACKED_SEEN_KEYS)
        self.assertGreater(bounds["seen_key_cap_drops"], 0)

    def test_04_reconnect_lifecycle_and_fail_open_fault_isolation(self):
        probe = S1IngressMeasurementProbe(default_enabled=True)
        streamer = make_dummy_streamer(probe, ["NSE_INDEX|Nifty 50"])

        for idx in range(LIFECYCLE_RING_MAXLEN + 4):
            probe.record_ws_lifecycle("error", detail=f"transient_{idx}")
        probe.record_ws_lifecycle("close", detail="1006")
        time.sleep(0.005)
        probe.record_ws_lifecycle("reconnecting", detail="attempt 1/5")
        probe.record_ws_lifecycle("open")

        summary = probe.get_summary()
        lc = summary["lifecycle_and_reconnects"]
        self.assertEqual(lc["ws_open_count"], 1)
        self.assertEqual(lc["ws_close_count"], 1)
        self.assertEqual(lc["ws_reconnecting_count"], 1)
        self.assertGreater(lc["max_disconnect_gap_ms"], 0.0)
        self.assertEqual(len(lc["recent_events"]), LIFECYCLE_RING_MAXLEN)
        self.assertGreater(summary["failures_and_bounds"]["lifecycle_ring_evictions"], 0)

        with self.assertRaises(Exception):
            streamer.handle_message(None, b"\xff\xff\xffINVALID_PROTOBUF")
        self.assertEqual(probe.get_summary()["failures_and_bounds"]["sdk_decode_errors"], 1)

        downstream_msgs: List[Dict[str, Any]] = []
        streamer.on("message", lambda d: downstream_msgs.append(d))
        valid_raw = encode_v3_feed_response(
            {
                "type": "live_feed",
                "feeds": {"NSE_INDEX|Nifty 50": {"fullFeed": {"indexFF": {"ltpc": {"ltp": 24000.0}}}}},
            }
        )

        # Force internal exception inside record_ingress_frame -> fail-open + auto-disable
        probe._hist_frame_bytes = None  # type: ignore[assignment]
        for _ in range(MAX_CONSECUTIVE_PROBE_FAULTS + 2):
            streamer.handle_message(None, valid_raw)

        self.assertEqual(len(downstream_msgs), MAX_CONSECUTIVE_PROBE_FAULTS + 2)
        self.assertFalse(probe.enabled)
        self.assertTrue(probe.get_compact_status()["auto_disabled_on_fault"])
        self.assertEqual(probe.get_compact_status()["probe_internal_errors"], MAX_CONSECUTIVE_PROBE_FAULTS)

    def test_05_zero_impact_on_stream_normalization_broker_requests_and_models(self):
        limiter_before = copy.deepcopy(get_upstox_limiter().get_stats())

        protected_files = [
            BACKEND_DIR / "models" / "champion_ensemble.pkl",
            BACKEND_DIR / "app" / "analytics" / "option_sniffer_advance_s205" / "s205_meta_weights.json",
            BACKEND_DIR / "app" / "analytics" / "option_sniffer_advance_s205" / "s205_upstox_engine.py",
        ]
        hashes_before = {}
        for pf in protected_files:
            if pf.exists():
                hashes_before[pf] = hashlib.sha256(pf.read_bytes()).hexdigest()

        svc = UpstoxStreamService.__new__(UpstoxStreamService)
        svc._lock = UpstoxStreamService()._lock
        svc._is_running = False
        svc._is_connected = False
        svc._last_msg_ts = 0.0
        svc._total_messages = 0
        svc._total_ticks = 0
        svc._reconnect_count = 0
        svc._last_error = None
        svc._key_to_symbol = {"NSE_EQ|INE002A01018": "RELIANCE.NS"}
        svc._symbol_to_key = {"RELIANCE.NS": "NSE_EQ|INE002A01018"}
        svc._live_quotes = {}
        svc._minute_bars = {"RELIANCE.NS": {}}
        svc._completed_15m_bars = {}
        svc._flushed_slots = set()
        svc._latest_frozen_snapshot = None
        svc._feed_listeners = []
        svc._additional_keys = set()
        svc._subscribed_ws_keys = {"NSE_EQ|INE002A01018"}
        local_probe = S1IngressMeasurementProbe(default_enabled=False)
        svc._s1_ingress_probe = local_probe

        sample_msg = {
            "type": "live_feed",
            "currentTs": "1763030100000",
            "feeds": {
                "NSE_EQ|INE002A01018": {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": 1302.5, "ltt": "1763030100000", "cp": 1290.0},
                            "atp": 1298.25,
                            "vtt": "550000",
                            "marketOHLC": {
                                "ohlc": [
                                    {
                                        "interval": "I1",
                                        "open": 1300.0,
                                        "high": 1303.0,
                                        "low": 1299.5,
                                        "close": 1302.5,
                                        "vol": "18500",
                                        "ts": "1763030100000",
                                    }
                                ]
                            },
                        }
                    }
                }
            },
        }

        svc._handle_feed_message(copy.deepcopy(sample_msg))
        quote_off = copy.deepcopy(svc._live_quotes["RELIANCE.NS"])
        bars_off = copy.deepcopy(svc._minute_bars["RELIANCE.NS"])

        svc._live_quotes.clear()
        svc._minute_bars["RELIANCE.NS"].clear()
        local_probe.enable(reset_counters=True)
        svc._handle_feed_message(copy.deepcopy(sample_msg))
        quote_on = copy.deepcopy(svc._live_quotes["RELIANCE.NS"])
        bars_on = copy.deepcopy(svc._minute_bars["RELIANCE.NS"])

        self.assertEqual(quote_off, quote_on)
        self.assertEqual(bars_off, bars_on)
        self.assertEqual(local_probe.get_summary()["latencies_us"]["equity_ingest_us"]["count"], 1)

        limiter_after = copy.deepcopy(get_upstox_limiter().get_stats())
        self.assertEqual(limiter_after.get("total_requests", 0), limiter_before.get("total_requests", 0))

        for pf, h_prev in hashes_before.items():
            self.assertEqual(hashlib.sha256(pf.read_bytes()).hexdigest(), h_prev)

    def test_06_synthetic_overhead_stays_within_safety_budget(self):
        raw_1k, n_inst, sub, k2c = build_synthetic_protobuf_frame(1024)
        probe = S1IngressMeasurementProbe(default_enabled=False)
        probe.set_universe_supplier(lambda: (sub, k2c))
        probe.enable(reset_counters=True)
        streamer = make_dummy_streamer(probe, list(sub))
        streamer.on("message", lambda _d: None)

        for _ in range(400):
            streamer.handle_message(None, raw_1k)

        summary = probe.get_summary()
        probe_self_p99 = summary["latencies_us"]["probe_self_overhead_us"]["p99"]
        self.assertGreaterEqual(n_inst, 2)
        self.assertLess(probe_self_p99, 100.0)


def run_synthetic_workload_benchmarks() -> List[Dict[str, Any]]:
    """
    Representative synthetic workload benchmark matching Codex's payload tiers:
      - ~1 KiB frame (small multi-instrument tick)
      - ~16 KiB frame (medium multi-instrument option + equity L5 frame)
      - ~64 KiB frame (large burst frame near 64 KiB slot boundary)
    Evaluated at paced 1,000 msg/s and 5,000 msg/s equivalents, comparing:
      1. Uninstrumented MarketDataStreamerV3.handle_message
      2. Instrumented streamer with probe DISABLED (default-OFF fast path)
      3. Instrumented streamer with probe ENABLED (including standalone probe self-overhead)
    """
    results: List[Dict[str, Any]] = []
    configs = [
        (1024, 2000),
        (16384, 1500),
        (65536, 600),
    ]

    for target_b, iterations in configs:
        raw, n_inst, sub, k2c = build_synthetic_protobuf_frame(target_b)

        cfg = upstox_client.Configuration()
        cfg.access_token = "DUMMY_BENCHMARK_TOKEN"
        client = upstox_client.ApiClient(cfg)

        base_streamer = upstox_client.MarketDataStreamerV3(client, list(sub), mode="full")
        base_streamer.on("message", lambda _d: None)

        probe = S1IngressMeasurementProbe(default_enabled=False)
        probe.set_universe_supplier(lambda s=sub, m=k2c: (s, m))
        inst_streamer = make_dummy_streamer(probe, list(sub))
        inst_streamer.on("message", lambda _d: None)

        # Warm-up
        for _ in range(60):
            base_streamer.handle_message(None, raw)
            inst_streamer.handle_message(None, raw)
        probe.enable(reset_counters=True)
        for _ in range(60):
            inst_streamer.handle_message(None, raw)
        probe.disable()

        hist_base = BoundedHistogram(LATENCY_BOUNDS_US)
        hist_off = BoundedHistogram(LATENCY_BOUNDS_US)
        hist_on = BoundedHistogram(LATENCY_BOUNDS_US)

        gc_was_enabled = gc.isenabled()
        gc.collect()
        gc.disable()
        try:
            # 1. Baseline uninstrumented SDK
            for _ in range(iterations):
                t0 = time.perf_counter_ns()
                base_streamer.handle_message(None, raw)
                hist_base.observe((time.perf_counter_ns() - t0) / 1000.0)

            # 2. Instrumented default-OFF
            probe.disable()
            for _ in range(iterations):
                t0 = time.perf_counter_ns()
                inst_streamer.handle_message(None, raw)
                hist_off.observe((time.perf_counter_ns() - t0) / 1000.0)

            # 3. Instrumented ENABLED
            probe.enable(reset_counters=True)
            for _ in range(iterations):
                t0 = time.perf_counter_ns()
                inst_streamer.handle_message(None, raw)
                hist_on.observe((time.perf_counter_ns() - t0) / 1000.0)
        finally:
            if gc_was_enabled:
                gc.enable()

        probe_summary = probe.get_summary()
        probe_self = probe_summary["latencies_us"]["probe_self_overhead_us"]
        sdk_dec = probe_summary["latencies_us"]["sdk_decode_us"]
        sdk_dict = probe_summary["latencies_us"]["sdk_dict_convert_us"]

        results.append(
            {
                "target_bytes": target_b,
                "actual_wire_bytes": len(raw),
                "instruments_in_frame": n_inst,
                "iterations": iterations,
                "baseline_uninstrumented_us": hist_base.summary(),
                "instrumented_default_off_us": hist_off.summary(),
                "instrumented_enabled_total_us": hist_on.summary(),
                "probe_self_overhead_us": probe_self,
                "sdk_protobuf_decode_us": sdk_dec,
                "sdk_message_to_dict_us": sdk_dict,
                "cpu_budget_pct_at_1000_msgs_s_probe_only": round(
                    (probe_self["mean"] * 1000.0) / 1_000_000.0 * 100.0, 3
                ),
                "cpu_budget_pct_at_5000_msgs_s_probe_only": round(
                    (probe_self["mean"] * 5000.0) / 1_000_000.0 * 100.0, 3
                ),
            }
        )
    return results


if __name__ == "__main__":
    if "--benchmark" in sys.argv:
        print(json.dumps(run_synthetic_workload_benchmarks(), indent=2))
    else:
        unittest.main(verbosity=2)
