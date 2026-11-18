"""来源与发布窗口的采集覆盖约定。

采集窗口只描述"承诺收到多少条有效记录"，不描述这些记录的内容。
尚未到达的承诺数始终作为缺口保留，不能由其他来源的转载或观察记录填补。
"""
from .contracts import digest, text, timestamp, utc_now
from .errors import require

# 同一来源可能有多个相邻窗口，状态按严重程度取最差的一个。
_FRAME_STATUS_ORDER = ("partial_window", "import_pending", "open", "shortfall",
                       "incomplete", "surplus", "revised")


def _merge_covers(intervals, start, end):
    """判断若干半开区间能否无缝覆盖 [start, end)。"""
    if not intervals:
        return False
    cursor = start
    for left, right in sorted(intervals):
        if right <= cursor:
            continue
        if left > cursor:
            return False
        cursor = right
        if cursor >= end:
            return True
    return cursor >= end


def _worst(names):
    present = [name for name in names if name in _FRAME_STATUS_ORDER]
    if not present:
        return "complete"
    return min(present, key=_FRAME_STATUS_ORDER.index)


class CollectionFrames:
    def __init__(self, store, sources, observations):
        self.store = store
        self.sources = sources
        self.observations = observations

    # ------------------------------------------------------------------
    # 登记与封存
    # ------------------------------------------------------------------
    def open(self, frame_id, source_id, start, end, expected_count, batch_id=None):
        with self.store.transaction():
            return self._open_locked(frame_id, source_id, start, end, expected_count,
                                     batch_id=batch_id)

    def _open_locked(self, frame_id, source_id, start, end, expected_count, batch_id=None):
        frame_id = text(frame_id, "frame_id")
        source_id = text(source_id, "source_id")
        start = timestamp(start, "start")
        end = timestamp(end, "end")
        require(start < end, "invalid_window", "frame start must precede end")
        require(type(expected_count) is int and expected_count >= 0, "invalid_expected_count",
                "expected_count must be nonnegative")
        self.sources.validate_for_ingest(source_id)
        batch_id = text(batch_id, "batch_id") if batch_id is not None else None
        if batch_id is not None:
            batch = self.store.one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
            require(batch is not None, "batch_missing", "frame batch is not registered")
            require(batch["expected_count"] >= expected_count, "frame_batch_mismatch",
                    "frame expects more rows than the import manifest contains",
                    expected=expected_count, manifest=batch["expected_count"])
        existing = self.store.one("SELECT * FROM collection_frames WHERE frame_id=?", (frame_id,))
        if existing:
            same = all((existing["source_id"] == source_id, existing["start_at"] == start,
                        existing["end_at"] == end, existing["expected_count"] == expected_count,
                        existing["batch_id"] == batch_id))
            require(same, "frame_conflict", "frame identifier has different coverage contract")
            return self.describe(frame_id) | {"duplicate": True}
        overlapping = self.store.one(
            "SELECT frame_id FROM collection_frames WHERE source_id=? AND start_at<? AND end_at>?",
            (source_id, end, start))
        require(overlapping is None, "frame_overlap", "source coverage windows overlap")
        now = utc_now()
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO collection_frames(frame_id,source_id,start_at,end_at,expected_count,"
                "state,batch_id,created_at) VALUES(?,?,?,?,?,'open',?,?)",
                (frame_id, source_id, start, end, expected_count, batch_id, now))
            self.store.event("frame_opened", frame_id,
                             digest([source_id, start, end, expected_count, batch_id]), now)
        return self.describe(frame_id) | {"duplicate": False}

    def _candidates(self, frame):
        rows = self.store.all(
            "SELECT o.* FROM observations o JOIN observation_heads h ON o.origin=h.origin "
            "AND o.external_id=h.external_id AND o.revision=h.revision WHERE o.source_id=? "
            "AND o.published_at>=? AND o.published_at<? AND o.status='active' "
            "ORDER BY o.origin,o.external_id",
            (frame["source_id"], frame["start"], frame["end"]))
        return [{"origin": row["origin"], "external_id": row["external_id"],
                 "revision": row["revision"], "hash": row["payload_hash"]} for row in rows]

    def _batch_progress(self, batch_id):
        if not batch_id:
            return None
        row = self.store.one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        if row is None:
            return {"batch_id": batch_id, "missing": True, "state": "missing",
                    "expected_count": 0, "cursor": 0, "processed_rows": 0,
                    "pending_rows": 0, "accepted": 0, "duplicate_count": 0,
                    "rejected": 0, "complete": False}
        pending = max(row["expected_count"] - row["cursor"], 0)
        return {"batch_id": batch_id, "missing": False, "state": row["state"],
                "expected_count": row["expected_count"], "cursor": row["cursor"],
                "processed_rows": row["cursor"], "pending_rows": pending,
                "accepted": row["accepted"], "duplicate_count": row["duplicate_count"],
                "rejected": row["rejected"],
                "complete": row["state"] == "closed" and pending == 0}

    def describe(self, frame_id):
        row = self.store.one("SELECT * FROM collection_frames WHERE frame_id=?", (frame_id,))
        require(row is not None, "frame_missing", "frame not found")
        data = {"frame_id": frame_id, "source_id": row["source_id"], "start": row["start_at"],
                "end": row["end_at"], "expected_count": row["expected_count"],
                "state": row["state"], "actual_count": row["actual_count"],
                "frozen_hash": row["frozen_hash"], "batch_id": row["batch_id"],
                "created_at": row["created_at"], "closed_at": row["closed_at"]}
        candidates = self._candidates(data)
        current = len(candidates)
        expected = row["expected_count"]
        batch = self._batch_progress(row["batch_id"])
        data["current_count"] = current
        data["observed_count"] = current
        data["current_hash"] = digest(candidates)
        data["batch"] = batch
        data["import_pending_rows"] = (batch or {}).get("pending_rows", 0)
        data["rejected_import_rows"] = (batch or {}).get("rejected", 0)
        data["drifted"] = row["state"] == "closed" and data["current_hash"] != row["frozen_hash"]
        if not data["drifted"]:
            data["drift_kind"] = None
        elif current < row["actual_count"]:
            data["drift_kind"] = "shortfall"
        elif current > row["actual_count"]:
            data["drift_kind"] = "surplus"
        else:
            data["drift_kind"] = "revised"
        data["outstanding_reports"] = max(expected - current, 0)
        data["surplus_reports"] = max(current - expected, 0)
        # 未处理的导入行本身也是尚未确认的覆盖；两类缺口不重复相加。
        data["pending_reports"] = max(data["outstanding_reports"], data["import_pending_rows"])
        data["coverage_complete"] = (
            row["state"] == "closed" and not data["drifted"] and current == expected
            and data["import_pending_rows"] == 0 and (batch or {}).get("rejected", 0) == 0
        )
        data["status"] = self._frame_status(data)
        return data

    @staticmethod
    def _frame_status(frame):
        if frame["batch"] and frame["batch"].get("missing"):
            return "partial_window"
        if frame["import_pending_rows"] > 0:
            return "import_pending"
        if frame["state"] != "closed":
            return "open"
        if frame["drift_kind"] == "shortfall":
            return "shortfall"
        if frame["current_count"] < frame["expected_count"]:
            return "incomplete"
        if frame["drift_kind"] == "surplus" or frame["current_count"] > frame["expected_count"]:
            return "surplus"
        if frame["drift_kind"] == "revised":
            return "revised"
        if frame["rejected_import_rows"]:
            return "incomplete"
        return "complete"

    def _seal(self, frame, now):
        self.store.db.execute(
            "UPDATE collection_frames SET state='closed',actual_count=?,frozen_hash=?,closed_at=? "
            "WHERE frame_id=?", (frame["current_count"], frame["current_hash"], now,
                                 frame["frame_id"]))
        self.store.db.execute("DELETE FROM frame_members WHERE frame_id=?", (frame["frame_id"],))
        for item in self._candidates(frame):
            self.store.db.execute(
                "INSERT INTO frame_members(frame_id,origin,external_id,revision) VALUES(?,?,?,?)",
                (frame["frame_id"], item["origin"], item["external_id"], item["revision"]))
        seal_no = self.store.one(
            "SELECT COALESCE(MAX(seal_no),0)+1 AS next FROM frame_seals WHERE frame_id=?",
            (frame["frame_id"],))["next"]
        self.store.db.execute(
            "INSERT INTO frame_seals(frame_id,seal_no,actual_count,current_count,frozen_hash,sealed_at) "
            "VALUES(?,?,?,?,?,?)",
            (frame["frame_id"], seal_no, frame["current_count"], frame["current_count"],
             frame["current_hash"], now))
        return seal_no

    def _require_batch_sealable(self, frame):
        batch = frame["batch"]
        if not batch:
            return
        require(not batch.get("missing"), "batch_missing", "frame batch is not registered")
        require(batch["complete"], "frame_batch_incomplete",
                "import has unprocessed rows; accepted and pending rows cannot share a window",
                pending=batch["pending_rows"], cursor=batch["cursor"],
                expected=batch["expected_count"])
        require(batch["rejected"] == 0, "frame_batch_rejected",
                "import contains rejected rows; coverage commitment is not met",
                rejected=batch["rejected"])

    def close(self, frame_id):
        with self.store.transaction():
            return self._close_locked(frame_id)

    def _close_locked(self, frame_id):
        frame = self.describe(frame_id)
        if frame["state"] == "closed":
            require(not frame["drifted"], "frame_drift", "closed frame changed after sealing")
            return frame | {"duplicate": True}
        # 导入未走完时，已接受行与待处理行不能合成一个完整窗口。
        self._require_batch_sealable(frame)
        require(frame["current_count"] == frame["expected_count"], "frame_incomplete",
                "observed count differs from declared coverage", expected=frame["expected_count"],
                actual=frame["current_count"])
        now = utc_now()
        with self.store.transaction():
            self._seal(frame, now)
            self.store.event("frame_closed", frame_id, frame["current_hash"], now)
        return self.describe(frame_id) | {"duplicate": False}

    def reseal(self, frame_id, reason="coverage interpretation repaired"):
        """在记录更正后重新封存一个已漂移的窗口。

        只允许封存仍然满足原承诺条数的窗口；撤回造成的实际观察减少必须继续
        显示为缺口，不能靠重新封存抹掉。
        """
        with self.store.transaction():
            return self._reseal_locked(frame_id, reason)

    def _reseal_locked(self, frame_id, reason):
        reason = text(reason, "reason", 800)
        frame = self.describe(frame_id)
        require(frame["state"] == "closed", "frame_not_closed",
                "only a closed frame can be resealed")
        require(frame["drifted"], "frame_not_drifted", "frame has not drifted")
        require(frame["current_count"] == frame["expected_count"], "frame_incomplete",
                "resealed frame no longer meets its original coverage commitment",
                expected=frame["expected_count"], actual=frame["current_count"])
        self._require_batch_sealable(frame)
        previous = frame["frozen_hash"]
        now = utc_now()
        with self.store.transaction():
            seal_no = self._seal(frame, now)
            self.store.event("frame_resealed", frame_id,
                             digest([previous, frame["current_hash"], reason]), now)
        return self.describe(frame_id) | {"duplicate": False, "seal_no": seal_no,
                                          "previous_hash": previous, "reason": reason}

    def seals(self, frame_id):
        rows = self.store.all(
            "SELECT seal_no,actual_count,current_count,frozen_hash,sealed_at FROM frame_seals "
            "WHERE frame_id=? ORDER BY seal_no", (frame_id,))
        return [dict(row) for row in rows]

    def withdraw(self, frame_id):
        """把窗口退回未封存状态，用于重新导入仍缺口的来源。"""
        with self.store.transaction():
            frame = self.describe(frame_id)
            require(frame["state"] == "closed", "frame_not_closed",
                    "only a closed frame can be reopened")
            require(frame["drifted"], "frame_not_drifted",
                    "a matching frame should be kept sealed")
            now = utc_now()
            self.store.db.execute(
                "UPDATE collection_frames SET state='open',actual_count=0,frozen_hash=NULL,"
                "closed_at=NULL WHERE frame_id=?", (frame_id,))
            self.store.db.execute("DELETE FROM frame_members WHERE frame_id=?", (frame_id,))
            self.store.event("frame_reopened", frame_id, frame["frozen_hash"] or "", now)
        return self.describe(frame_id) | {"duplicate": False}

    # ------------------------------------------------------------------
    # 查询窗口中的覆盖
    # ------------------------------------------------------------------
    def _frames_for_source(self, source_id, spec):
        rows = self.store.all(
            "SELECT frame_id FROM collection_frames WHERE source_id=? AND start_at<? AND end_at>? "
            "ORDER BY start_at", (source_id, spec["end"], spec["start"]))
        return [self.describe(row["frame_id"]) for row in rows]

    def _source_has_observations(self, source_id, spec):
        row = self.store.one(
            "SELECT 1 FROM observations o JOIN observation_heads h ON o.origin=h.origin "
            "AND o.external_id=h.external_id AND o.revision=h.revision WHERE o.source_id=? "
            "AND o.published_at>=? AND o.published_at<? AND o.status='active' LIMIT 1",
            (source_id, spec["start"], spec["end"]))
        return row is not None

    @staticmethod
    def _frame_in_query(frame, spec):
        """窗口是否完全落在查询范围内，因而可把承诺条数归属于该查询。"""
        return frame["start"] >= spec["start"] and frame["end"] <= spec["end"]

    def _source_coverage(self, source, spec):
        source_id = source["source_id"]
        frames = self._frames_for_source(source_id, spec)
        observed = self._source_has_observations(source_id, spec)
        base = {"source_id": source_id, "outlet": source["outlet"],
                "region": source["region"], "active": source["active"],
                "has_observations": observed}
        if not frames and not observed:
            return base | {"in_scope": False, "status": "out_of_scope", "frames": [],
                           "pending_reports": 0, "surplus_reports": 0,
                           "pending_known": True, "import_pending_rows": 0,
                           "registered": False, "issues": []}
        if not frames and observed:
            # 有观察但从未登记窗口承诺：没有可核对的缺口，覆盖约定缺失。
            return base | {"in_scope": True, "status": "unregistered", "frames": [],
                           "pending_reports": 0, "surplus_reports": 0,
                           "pending_known": True, "import_pending_rows": 0,
                           "registered": False,
                           "issues": [{"reason": "unregistered"}]}
        for frame in frames:
            frame["within_query"] = self._frame_in_query(frame, spec)
            frame["attributable"] = frame["within_query"]
            if not frame["within_query"]:
                # 跨出查询范围的窗口无法把预期条数拆分到查询内。
                frame["pending_reports"] = None
                frame["surplus_reports"] = None
        covers_query = _merge_covers([(frame["start"], frame["end"]) for frame in frames],
                                     spec["start"], spec["end"])
        attributable = covers_query and all(frame["attributable"] for frame in frames)
        pending = None
        surplus = None
        if attributable:
            pending = sum(frame["pending_reports"] for frame in frames)
            surplus = sum(frame["surplus_reports"] for frame in frames)
        issues = []
        if not covers_query:
            issues.append({"reason": "partial_window"})
        for frame in frames:
            if frame["attributable"] and frame["status"] != "complete":
                issues.append({"reason": frame["status"], "frame_id": frame["frame_id"],
                               "expected_count": frame["expected_count"],
                               "observed_count": frame["current_count"],
                               "pending_reports": frame["pending_reports"]})
            elif not frame["attributable"]:
                issues.append({"reason": "partial_window", "frame_id": frame["frame_id"]})
        if not covers_query:
            status = "partial_window"
        elif not attributable:
            # 任一窗口跨出查询范围，就无法把承诺条数完整归到查询内。
            status = "partial_window"
        else:
            status = _worst([frame["status"] for frame in frames])
        return base | {"in_scope": True, "status": status, "frames": frames,
                       "pending_reports": pending, "surplus_reports": surplus,
                       "pending_known": attributable,
                       "import_pending_rows": sum(frame["import_pending_rows"]
                                                  for frame in frames),
                       "registered": True, "issues": issues}

    def coverage_report(self, spec):
        """按来源与地区汇总查询窗口内的覆盖承诺。

        没有登记窗口且没有观察的来源不参与覆盖判断。某来源登记了窗口后，
        同地区只有观察、没有窗口的来源会被标为覆盖未知，而不是当作完整。
        """
        total_frames = self.store.one(
            "SELECT COUNT(*) AS count FROM collection_frames")["count"]
        sources = self.sources.list()
        if spec["regions"]:
            sources = [source for source in sources if source["region"] in spec["regions"]]
        by_region = {}
        for source in sources:
            record = self._source_coverage(source, spec)
            by_region.setdefault(source["region"], []).append(record)
        regions = []
        for region, records in sorted(by_region.items()):
            in_scope = [record for record in records if record["in_scope"]]
            if not in_scope:
                continue
            # 只有整个数据库登记过窗口承诺时，未登记窗口的来源才构成可比性缺口；
            # 完全没有任何承诺时状态是 no_commitments，而不是伪造的完整覆盖。
            unregistered = ([record["source_id"] for record in in_scope
                             if record["status"] == "unregistered"]
                            if total_frames else [])
            unbounded = [record["source_id"] for record in in_scope
                         if not record["pending_known"]]
            if not total_frames:
                status = "no_commitments"
            elif unregistered or unbounded:
                status = "partial"
            elif any(record["status"] != "complete" for record in in_scope
                     if record["frames"]):
                status = "incomplete"
            else:
                status = "complete"
            pending = sum(record["pending_reports"] for record in records
                          if record["pending_known"])
            # 未登记窗口的来源或完全没有承诺时，缺口不是零而是未知；
            # 此时不能据此给出精确的占比区间。
            known = not unbounded and not unregistered and bool(total_frames)
            issues = []
            for record in in_scope:
                for issue in record["issues"]:
                    issues.append({"source_id": record["source_id"], **issue})
            regions.append({
                "region": region, "status": status,
                "provisional": status in {"partial", "incomplete"},
                "bounded": known,
                "pending_reports": pending if known else None,
                "pending_known": known,
                "uncovered_sources": unregistered,
                "unbounded_sources": unbounded,
                "registered_frames": sum(len(record["frames"]) for record in in_scope),
                "source_count": len(records),
                "issues": issues,
                "sources": records,
            })
        rank = {"no_commitments": 0, "complete": 1, "incomplete": 2, "partial": 3}
        status = "no_commitments"
        for region in regions:
            if rank[region["status"]] > rank[status]:
                status = region["status"]
        return {
            "start": spec["start"], "end": spec["end"],
            "status": status,
            "provisional": any(region["provisional"] for region in regions),
            "bounded": all(region["bounded"] for region in regions),
            "pending_reports": sum(region["pending_reports"] or 0 for region in regions)
            if all(region["pending_known"] for region in regions) else None,
            "registered_frames": total_frames,
            "regions": regions,
            "rules": {"pending_stance_assigned": False,
                      "cross_source_substitution": False,
                      "repost_fills_gap": False},
        }

    def coverage(self, spec):
        """按查询范围返回各来源窗口描述，保留原有调用方式。"""
        result = []
        for region in self.coverage_report(spec)["regions"]:
            for source in region["sources"]:
                result.extend(source["frames"])
        return result

    def quality(self, spec):
        coverage = self.coverage_report(spec)
        problems = []
        for region in coverage["regions"]:
            if region["status"] in {"complete", "no_commitments"}:
                continue
            for source in region["sources"]:
                if not source["in_scope"]:
                    continue
                for issue in source["issues"]:
                    problems.append({"region": region["region"],
                                     "source_id": source["source_id"], **issue})
        return {"frames": [frame for region in coverage["regions"]
                           for source in region["sources"]
                           for frame in source["frames"]],
                "regions": coverage["regions"],
                "problems": problems, "passed": not problems,
                "provisional": coverage["provisional"],
                "pending_reports": coverage["pending_reports"]}
