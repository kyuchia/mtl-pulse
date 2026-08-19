"""
Unit tests for the GTFS static simulator.

    python simulate/test_gtfs_sim.py          # or: pytest simulate/test_gtfs_sim.py

Most tests build a tiny synthetic feed in a temp directory rather than leaning on the
archived real feeds, so they stay deterministic when a new GTFS is downloaded. The
cross-midnight test is the important one: GTFS lets stop_times run past 24:00:00, and
naive calendar-day slicing silently drops the entire late-night service.
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gtfs_sim import (TZ, Gtfs, _metres, _secs, _service_midnight, effective_date,
                      resolve_service_ids, simulate, to_payload, trip_stop_profile)


def build_feed(tmpdir, *, calendar_rows, calendar_dates_rows, stop_times_rows,
               trips_rows, with_shape_dist=False, agency="stm", day="2026-08-18"):
    """Write a minimal but valid GTFS zip into gtfs/<agency>/<day>/gtfs.zip."""
    d = os.path.join(tmpdir, agency, day)
    os.makedirs(d, exist_ok=True)

    # A straight 4 km east-west line at latitude 45.5, five shape points.
    shape_pts = []
    for i in range(5):
        lon = -73.60 + i * 0.01
        if with_shape_dist:
            shape_pts.append(f"S1,45.50,{lon:.5f},{i},{i * 1000}")
        else:
            shape_pts.append(f"S1,45.50,{lon:.5f},{i}")
    shape_hdr = ("shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled"
                 if with_shape_dist else
                 "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence")

    stops = ["stop_id,stop_name,stop_lat,stop_lon"]
    for i in range(5):
        stops.append(f"ST{i},Stop {i},45.50,{-73.60 + i * 0.01:.5f}")

    st_hdr = ("trip_id,arrival_time,departure_time,stop_id,stop_sequence,shape_dist_traveled"
              if with_shape_dist else
              "trip_id,arrival_time,departure_time,stop_id,stop_sequence")

    files = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,Synth,http://x,America/Montreal",
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\nR1,R1,Route One,1",
        "trips.txt": "route_id,service_id,trip_id,shape_id\n" + "\n".join(trips_rows),
        "stop_times.txt": st_hdr + "\n" + "\n".join(stop_times_rows),
        "stops.txt": "\n".join(stops),
        "shapes.txt": shape_hdr + "\n" + "\n".join(shape_pts),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
                         "sunday,start_date,end_date\n" + "\n".join(calendar_rows)),
        "calendar_dates.txt": "service_id,date,exception_type\n" + "\n".join(calendar_dates_rows),
    }
    with zipfile.ZipFile(os.path.join(d, "gtfs.zip"), "w") as z:
        for name, body in files.items():
            z.writestr(name, body + "\n")
    return tmpdir


WEEKDAY_SERVICE = "WK,1,1,1,1,1,0,0,20260601,20260831"


class TestTimeParsing(unittest.TestCase):
    def test_hours_past_24(self):
        self.assertEqual(_secs("00:00:00"), 0)
        self.assertEqual(_secs("23:59:59"), 86399)
        self.assertEqual(_secs("24:00:00"), 86400)
        self.assertEqual(_secs("25:30:00"), 91800)
        self.assertEqual(_secs("26:15:30"), 94530)

    def test_service_midnight_is_noon_minus_12(self):
        m = _service_midnight(date(2026, 4, 14))
        self.assertEqual(m.hour, 0)
        self.assertEqual(m.date(), date(2026, 4, 14))
        # 25:30 on the 14th is 01:30 on the 15th in wall-clock terms
        self.assertEqual((m + timedelta(seconds=_secs("25:30:00"))).isoformat(),
                         "2026-04-15T01:30:00-04:00")


class TestServiceIds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        build_feed(
            self.tmp,
            calendar_rows=[WEEKDAY_SERVICE, "SAT,0,0,0,0,0,1,0,20260601,20260831"],
            calendar_dates_rows=["WK,20260703,2", "SAT,20260703,1"],
            trips_rows=["R1,WK,T1,S1"],
            stop_times_rows=["T1,08:00:00,08:00:00,ST0,1", "T1,08:10:00,08:10:00,ST4,2"],
        )
        self.g = Gtfs(os.path.join(self.tmp, "stm", "2026-08-18", "gtfs.zip"), {"1"})

    def test_weekday_pattern(self):
        # 2026-06-16 is a Tuesday
        self.assertEqual(resolve_service_ids(self.g, date(2026, 6, 16)), {"WK"})
        # 2026-06-20 is a Saturday
        self.assertEqual(resolve_service_ids(self.g, date(2026, 6, 20)), {"SAT"})

    def test_outside_span_is_empty(self):
        self.assertEqual(resolve_service_ids(self.g, date(2026, 4, 14)), set())

    def test_exceptions_add_and_remove(self):
        # 2026-07-03 is a Friday: WK removed (type 2), SAT added (type 1)
        self.assertEqual(resolve_service_ids(self.g, date(2026, 7, 3)), {"SAT"})


class TestEffectiveDate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        build_feed(
            self.tmp,
            calendar_rows=[WEEKDAY_SERVICE],
            calendar_dates_rows=["WK,20260602,2"],   # exception on a Tuesday
            trips_rows=["R1,WK,T1,S1"],
            stop_times_rows=["T1,08:00:00,08:00:00,ST0,1", "T1,08:10:00,08:10:00,ST4,2"],
        )
        self.g = Gtfs(os.path.join(self.tmp, "stm", "2026-08-18", "gtfs.zip"), {"1"})

    def test_in_span_passes_through(self):
        d = date(2026, 6, 16)
        self.assertEqual(effective_date(self.g, d, verbose=False), d)

    def test_substitute_is_real_same_weekday_in_span(self):
        src = date(2026, 4, 14)                       # Tuesday, outside the span
        got = effective_date(self.g, src, verbose=False)
        self.assertNotEqual(got, src)
        self.assertEqual(got.weekday(), src.weekday(), "must keep the day of week")
        self.assertTrue(self.g.span_start <= got <= self.g.span_end, "must be in span")
        self.assertNotIn(got, self.g.exception_dates, "must avoid exception dates")

    def test_substitute_skips_the_exception_date(self):
        # nearest Tuesday in span is 2026-06-02, but it carries an exception
        got = effective_date(self.g, date(2026, 4, 14), verbose=False)
        self.assertNotEqual(got, date(2026, 6, 2))
        self.assertEqual(got, date(2026, 6, 9))

    def test_substitute_preserves_dst_offset(self):
        src = date(2026, 4, 14)
        got = effective_date(self.g, src, verbose=False)
        off_src = datetime(src.year, src.month, src.day, 12, tzinfo=TZ).utcoffset()
        off_got = datetime(got.year, got.month, got.day, 12, tzinfo=TZ).utcoffset()
        self.assertEqual(off_src, off_got, "an EDT date must not map onto an EST date")


class TestCrossMidnight(unittest.TestCase):
    """The trap: a trip departing 25:30 belongs to the PREVIOUS service day."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        build_feed(
            self.tmp,
            calendar_rows=[WEEKDAY_SERVICE],
            calendar_dates_rows=[],
            trips_rows=["R1,WK,LATE,S1", "R1,WK,DAY,S1"],
            stop_times_rows=[
                # crosses 24:00 — 23:50 on the service day through 01:30 the next morning
                "LATE,23:50:00,23:50:00,ST0,1",
                "LATE,25:30:00,25:30:00,ST4,2",
                # an ordinary daytime trip, for contrast
                "DAY,08:00:00,08:00:00,ST0,1",
                "DAY,08:20:00,08:20:00,ST4,2",
            ],
        )

    def _sim(self, start, end):
        return simulate("metro", start, end, interval=60,
                        gtfs_dir=self.tmp, verbose=False)

    def test_after_midnight_trip_is_found(self):
        """Query 00:30–01:00 on the 15th; the trip lives on the 14th's service day."""
        start = datetime(2026, 4, 15, 0, 30, tzinfo=TZ)
        end = datetime(2026, 4, 15, 1, 0, tzinfo=TZ)
        trips, stats = self._sim(start, end)
        self.assertTrue(trips, "late-night trip was dropped — the cross-midnight bug")
        ids = {t["id"] for t in trips}
        self.assertIn("LATE", ids)
        self.assertNotIn("DAY", ids)

    def test_preceding_service_day_is_scanned(self):
        start = datetime(2026, 4, 15, 0, 30, tzinfo=TZ)
        end = datetime(2026, 4, 15, 1, 0, tzinfo=TZ)
        _, stats = self._sim(start, end)
        days = {s["day"] for s in stats["service_days"]}
        self.assertIn("2026-04-14", days,
                      "the preceding service day must be included as a candidate")

    def test_samples_stay_inside_the_window(self):
        start = datetime(2026, 4, 15, 0, 30, tzinfo=TZ)
        end = datetime(2026, 4, 15, 1, 0, tzinfo=TZ)
        trips, _ = self._sim(start, end)
        for t in trips:
            for ts in t["times"]:
                self.assertGreaterEqual(ts, start)
                self.assertLessEqual(ts, end)

    def test_a_naive_same_day_scan_would_miss_it(self):
        """Guard against a regression that stops expanding to the previous day."""
        start = datetime(2026, 4, 15, 0, 30, tzinfo=TZ)
        end = datetime(2026, 4, 15, 1, 0, tzinfo=TZ)
        trips, _ = self._sim(start, end)
        late = [t for t in trips if t["id"] == "LATE"][0]
        # every sample is on the 15th in wall-clock terms...
        self.assertTrue(all(ts.date() == date(2026, 4, 15) for ts in late["times"]))
        # ...yet the schedule row that produced it says 25:30 on the 14th
        self.assertGreater(_secs("25:30:00"), 86400)


class TestInterpolation(unittest.TestCase):
    def _feed(self, with_dist):
        tmp = tempfile.mkdtemp()
        build_feed(
            tmp,
            calendar_rows=[WEEKDAY_SERVICE],
            calendar_dates_rows=[],
            trips_rows=["R1,WK,T1,S1"],
            stop_times_rows=(
                ["T1,08:00:00,08:00:00,ST0,1,0", "T1,08:20:00,08:20:00,ST4,2,4000"]
                if with_dist else
                ["T1,08:00:00,08:00:00,ST0,1", "T1,08:20:00,08:20:00,ST4,2"]
            ),
            with_shape_dist=with_dist,
        )
        return Gtfs(os.path.join(tmp, "stm", "2026-08-18", "gtfs.zip"), {"1"}), tmp

    def test_profile_monotonic_with_projection(self):
        g, _ = self._feed(False)
        profile, _ = trip_stop_profile(g, "T1")
        self.assertIsNotNone(profile)
        times = [p[0] for p in profile]
        dists = [p[1] for p in profile]
        self.assertEqual(times, sorted(times))
        self.assertEqual(dists, sorted(dists))

    def test_profile_uses_native_shape_dist(self):
        g, _ = self._feed(True)
        profile, _ = trip_stop_profile(g, "T1")
        self.assertIsNotNone(profile)
        self.assertAlmostEqual(profile[0][1], 0.0, places=3)
        self.assertGreater(profile[-1][1], 3000)

    def test_positions_lie_on_the_shape(self):
        _, tmp = self._feed(False)
        start = datetime(2026, 4, 14, 8, 0, tzinfo=TZ)
        end = datetime(2026, 4, 14, 8, 20, tzinfo=TZ)
        trips, _ = simulate("metro", start, end, interval=60,
                            gtfs_dir=tmp, verbose=False)
        self.assertTrue(trips)
        for t in trips:
            for lon, lat in t["path"]:
                # the synthetic shape is the straight line lat == 45.50
                self.assertAlmostEqual(lat, 45.50, places=6)
                self.assertTrue(-73.601 <= lon <= -73.559, f"lon {lon} off the shape")

    def test_vehicle_advances_over_time(self):
        _, tmp = self._feed(False)
        start = datetime(2026, 4, 14, 8, 0, tzinfo=TZ)
        end = datetime(2026, 4, 14, 8, 20, tzinfo=TZ)
        trips, _ = simulate("metro", start, end, interval=60, gtfs_dir=tmp, verbose=False)
        lons = [p[0] for p in trips[0]["path"]]
        self.assertEqual(lons, sorted(lons), "vehicle must move forward along the shape")
        self.assertGreater(lons[-1] - lons[0], 0.03, "should traverse most of the line")


class TestPayloadShape(unittest.TestCase):
    def test_shape_matches_the_documented_format(self):
        tmp = tempfile.mkdtemp()
        build_feed(tmp, calendar_rows=[WEEKDAY_SERVICE], calendar_dates_rows=[],
                   trips_rows=["R1,WK,T1,S1"],
                   stop_times_rows=["T1,08:00:00,08:00:00,ST0,1",
                                    "T1,08:20:00,08:20:00,ST4,2"])
        start = datetime(2026, 4, 14, 8, 0, tzinfo=TZ)
        end = datetime(2026, 4, 14, 8, 20, tzinfo=TZ)
        trips, _ = simulate("metro", start, end, interval=60, gtfs_dir=tmp, verbose=False)
        payload = to_payload(trips)
        self.assertEqual(sorted(payload), ["duration_sec", "end_time", "modes",
                                           "start_time", "trips"])
        t = payload["trips"][0]
        self.assertEqual(sorted(t), ["id", "label", "mode", "path", "timestamps"])
        self.assertEqual(t["mode"], "metro")
        self.assertEqual(t["label"], "R1")
        self.assertEqual(len(t["path"]), len(t["timestamps"]))
        self.assertEqual(t["timestamps"][0], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
