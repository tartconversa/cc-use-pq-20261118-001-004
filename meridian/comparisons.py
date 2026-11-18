"""保留分母的窗口与地区对比，并区分已确认故事与未到齐的来源覆盖。"""
from collections import defaultdict
from .contracts import report_query
from .errors import require


def _share_interval(count, denominator, pending):
    """在 pending 条承诺尚未到达、且倾向未知时，某倾向占比的可行区间。

    未收到的报道不分配倾向；这里只给出上下界，不把上界当作预期值。
    """
    if denominator is None or denominator <= 0:
        return None
    if pending is None:
        return None
    total = denominator + pending
    return {"lower": round(count / total, 6),
            "upper": round(min(count + pending, total) / total, 6),
            "denominator": denominator, "pending": pending}


class ComparisonEngine:
    def __init__(self, analytics):
        self.analytics = analytics

    @staticmethod
    def _region_map(report):
        return {row["region"]: row for row in report["regions"]}

    @staticmethod
    def _coverage_record(report, region):
        for row in (report.get("coverage") or {}).get("regions", []):
            if row["region"] == region:
                return row
        return None

    def _sensitivity(self, left, right, left_cover, right_cover):
        """指出缺口足以翻转的结论。

        未收到的报道不分配倾向；区间只给出"如果这些承诺全部到齐且倾向未知"
        的可行范围，不是预期值。两端缺口不可量化时不给出精确区间。
        """
        left_pending = (left_cover or {}).get("pending_reports")
        right_pending = (right_cover or {}).get("pending_reports")
        bounded = bool(left_cover and right_cover
                       and left_cover["pending_known"] and right_cover["pending_known"])
        claims = []
        for stance in sorted(set(left["stance_counts"]) | set(right["stance_counts"])):
            left_count = left["stance_counts"].get(stance, 0)
            right_count = right["stance_counts"].get(stance, 0)
            left_interval = _share_interval(left_count, left["story_count"], left_pending)
            right_interval = _share_interval(right_count, right["story_count"], right_pending)
            observed = (round(right_count / right["story_count"]
                              - left_count / left["story_count"], 6)
                        if left["story_count"] and right["story_count"] else None)
            if not bounded or left_interval is None or right_interval is None:
                claims.append({"claim": f"{stance}_share", "observed_difference": observed,
                               "direction": "undetermined", "possible_difference": None,
                               "could_flip": None, "status": "undetermined",
                               "left_interval": left_interval,
                               "right_interval": right_interval})
                continue
            minimum = right_interval["lower"] - left_interval["upper"]
            maximum = right_interval["upper"] - left_interval["lower"]
            if minimum == 0 and maximum == 0:
                direction = "equal"
            elif minimum > 0:
                direction = "right_higher"
            elif maximum < 0:
                direction = "left_higher"
            else:
                direction = "undetermined"
            could_flip = direction == "undetermined"
            claims.append({
                "claim": f"{stance}_share", "observed_difference": observed,
                "direction": direction,
                "possible_difference": [round(minimum, 6), round(maximum, 6)],
                "could_flip": could_flip,
                "status": "fragile" if could_flip else "stable",
                "left_interval": left_interval, "right_interval": right_interval,
            })
        return {"bounded": bounded,
                "left_pending_reports": left_pending,
                "right_pending_reports": right_pending,
                "claims": claims,
                "fragile_claims": [row["claim"] for row in claims if row["could_flip"]],
                "tentative": bool(not bounded or any(row["could_flip"] for row in claims))}

    @staticmethod
    def _trace(report, region):
        record = ComparisonEngine._coverage_record(report, region)
        stories = [row for row in report["stories"] if row["region"] == region]
        denominators = defaultdict(lambda: {"stories": 0, "observations": 0, "sources": set()})
        for story in stories:
            key = (story["story_key"], story["topic"], story["audience"])
            denominators[key]["stories"] += 1
            denominators[key]["observations"] += story["observation_count"]
        story_rows = [{"story_key": key[0], "topic": key[1], "audience": key[2],
                       "story_count": value["stories"],
                       "observation_count": value["observations"]}
                      for key, value in sorted(denominators.items())]
        sources = []
        for source in (record or {}).get("sources", []):
            if not source["in_scope"]:
                continue
            sources.append({"source_id": source["source_id"], "outlet": source["outlet"],
                            "status": source["status"],
                            "pending_reports": source["pending_reports"],
                            "pending_known": source["pending_known"],
                            "observed_observations": source["has_observations"],
                            "frames": [{"frame_id": frame["frame_id"],
                                        "start": frame["start"], "end": frame["end"],
                                        "state": frame["state"],
                                        "expected_count": frame["expected_count"],
                                        "observed_count": frame["current_count"],
                                        "pending_reports": frame["pending_reports"],
                                        "status": frame["status"],
                                        "within_query": frame["within_query"]}
                                       for frame in source["frames"]]})
        region_row = next((row for row in report["regions"] if row["region"] == region), None)
        return {"region": region,
                "coverage": None if record is None else {
                    "status": record["status"], "pending_reports": record["pending_reports"],
                    "pending_known": record["pending_known"],
                    "uncovered_sources": record["uncovered_sources"]},
                "denominator": None if region_row is None else {
                    "stories": region_row["story_count"],
                    "observations": region_row["observation_count"]},
                "sources": sources, "stories": story_rows}

    def windows(self, left_query, right_query):
        left = self.analytics.report(left_query)
        right = self.analytics.report(right_query)
        require(left["query"]["regions"] == right["query"]["regions"], "incompatible_query",
                "region filters must match for comparison")
        require(left["query"]["topics"] == right["query"]["topics"], "incompatible_query",
                "topic filters must match for comparison")
        require(left["query"]["audiences"] == right["query"]["audiences"], "incompatible_query",
                "audience filters must match for comparison")
        left_regions = self._region_map(left)
        right_regions = self._region_map(right)
        rows = []
        for region in sorted(set(left_regions) | set(right_regions)):
            previous = left_regions.get(region, {})
            current = right_regions.get(region, {})
            old_count = previous.get("story_count", 0)
            new_count = current.get("story_count", 0)
            left_cover = self._coverage_record(left, region)
            right_cover = self._coverage_record(right, region)
            stances = {}
            for stance in sorted(set(previous.get("stance_counts", {})) | set(current.get("stance_counts", {}))):
                old = previous.get("stance_counts", {}).get(stance, 0)
                new = current.get("stance_counts", {}).get(stance, 0)
                old_share = old / old_count if old_count else None
                new_share = new / new_count if new_count else None
                stances[stance] = {"left_count": old, "right_count": new, "count_delta": new - old,
                                    "left_share": round(old_share, 6) if old_share is not None else None,
                                    "right_share": round(new_share, 6) if new_share is not None else None,
                                    "share_delta": round(new_share - old_share, 6)
                                    if old_share is not None and new_share is not None else None}
            rows.append({"region": region, "left_stories": old_count, "right_stories": new_count,
                         "story_delta": new_count - old_count, "stances": stances,
                         "left_coverage": None if left_cover is None else {
                             "status": left_cover["status"],
                             "pending_reports": left_cover["pending_reports"],
                             "pending_known": left_cover["pending_known"]},
                         "right_coverage": None if right_cover is None else {
                             "status": right_cover["status"],
                             "pending_reports": right_cover["pending_reports"],
                             "pending_known": right_cover["pending_known"]},
                         "tentative": bool((left_cover or {}).get("status") != "complete"
                                           or (right_cover or {}).get("status") != "complete")})
        return {"left_query_hash": left["query_hash"], "right_query_hash": right["query_hash"],
                "left_head": left["event_head"], "right_head": right["event_head"],
                "left_excluded": left["excluded"], "right_excluded": right["excluded"],
                "left_coverage": left["coverage"], "right_coverage": right["coverage"],
                "provisional": left["provisional"] or right["provisional"],
                "regions": rows}

    def regions(self, query, left_region, right_region):
        spec = report_query(query)
        require(left_region != right_region, "same_region", "choose distinct regions")
        if spec["regions"]:
            require(left_region in spec["regions"] and right_region in spec["regions"],
                    "incompatible_query", "both regions must belong to query")
        report = self.analytics.report(spec)
        regions = self._region_map(report)
        # 一侧完全没有已确认故事时仍需能比较：它的分母来自覆盖记录而不是缺失行。
        require(left_region in regions or left_region in {
            row["region"] for row in (report.get("coverage") or {}).get("regions", [])},
            "region_missing", "left region is not present")
        require(right_region in regions or right_region in {
            row["region"] for row in (report.get("coverage") or {}).get("regions", [])},
            "region_missing", "right region is not present")
        left = regions.get(left_region) or {"region": left_region, "story_count": 0,
                                            "observation_count": 0, "stance_counts": {},
                                            "stance_shares": {}, "topic_counts": {},
                                            "audience_counts": {}}
        right = regions.get(right_region) or {"region": right_region, "story_count": 0,
                                              "observation_count": 0, "stance_counts": {},
                                              "stance_shares": {}, "topic_counts": {},
                                              "audience_counts": {}}
        require(left["story_count"] or right["story_count"], "region_missing",
                "both regions lack eligible stories")
        left_cover = self._coverage_record(report, left_region)
        right_cover = self._coverage_record(report, right_region)
        stances = {}
        for stance in sorted(set(left["stance_counts"]) | set(right["stance_counts"])):
            left_count = left["stance_counts"].get(stance, 0)
            right_count = right["stance_counts"].get(stance, 0)
            left_share = left_count / left["story_count"] if left["story_count"] else None
            right_share = right_count / right["story_count"] if right["story_count"] else None
            stances[stance] = {
                "left_share": round(left_share, 6) if left_share is not None else None,
                "right_share": round(right_share, 6) if right_share is not None else None,
                "difference": round(right_share - left_share, 6)
                if left_share is not None and right_share is not None else None,
                "left_interval": _share_interval(left_count, left["story_count"],
                                                 (left_cover or {}).get("pending_reports")),
                "right_interval": _share_interval(right_count, right["story_count"],
                                                  (right_cover or {}).get("pending_reports")),
            }
        sensitivity = self._sensitivity(left, right, left_cover, right_cover)
        return {"query_hash": report["query_hash"], "event_head": report["event_head"],
                "left": left, "right": right, "stances": stances, "excluded": report["excluded"],
                "coverage": report["coverage"],
                "tentative": report["provisional"] or sensitivity["tentative"],
                "sensitivity": sensitivity,
                "trace": {"left": self._trace(report, left_region),
                          "right": self._trace(report, right_region)}}

    def topic_matrix(self, query):
        report = self.analytics.report(query)
        matrix = defaultdict(lambda: defaultdict(lambda: {"stories": 0, "stances": defaultdict(int)}))
        for story in report["stories"]:
            cell = matrix[story["region"]][story["topic"]]
            cell["stories"] += 1
            cell["stances"][story["stance"]] += 1
        rows = []
        for region, topics in sorted(matrix.items()):
            for topic, cell in sorted(topics.items()):
                rows.append({"region": region, "topic": topic, "story_count": cell["stories"],
                             "stance_counts": dict(sorted(cell["stances"].items()))})
        require(sum(row["story_count"] for row in rows) == report["story_count"], "matrix_mismatch",
                "topic matrix does not reconcile to report")
        return {"query_hash": report["query_hash"], "event_head": report["event_head"],
                "total_stories": report["story_count"], "rows": rows,
                "coverage_status": report["coverage_status"],
                "provisional": report["provisional"]}
