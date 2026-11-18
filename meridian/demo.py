"""运行本地合成数据的端到端验收场景。"""
import json
import tempfile
from pathlib import Path
from .service import MeridianService


def source(source_id, region, outlet):
    return {"source_id": source_id, "region": region, "outlet": outlet,
            "topics": ["technology", "culture"], "audiences": ["general", "students"]}


def observation(external_id, source_id, story_key, published_at="2026-11-01T10:00:00+00:00"):
    return {"origin": "local-fixture", "external_id": external_id, "source_id": source_id,
            "story_key": story_key, "published_at": published_at,
            "received_at": "2026-11-02T09:00:00+00:00",
            "headline": f"Synthetic observation {external_id}",
            "url": f"https://example.invalid/{external_id}"}


def decision(external_id, reviewer, stance, topic="technology", audience="general"):
    return {"decision_id": f"{external_id}-{reviewer}", "origin": "local-fixture",
            "external_id": external_id, "revision": 1, "reviewer": reviewer,
            "topic": topic, "stance": stance, "audience": audience,
            "reason": "Synthetic reviewer judgment based on supplied fixture"}


def query():
    return {"start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00",
            "regions": ["North"], "topics": [], "audiences": []}


def scenario():
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "scenario.sqlite3")
        service = MeridianService(path)
        service.sources.register(source("s1", "North", "Outlet A"))
        service.sources.register(source("s2", "North", "Outlet B"))
        service.sources.register(source("s3", "South", "Outlet C"))
        rows = [observation("a", "s1", "story-1"), observation("b", "s2", "story-1"),
                observation("c", "s1", "story-2")]
        first = service.batches.step("fixture-batch", rows, limit=1)
        assert first["cursor"] == 1
        batch = service.batches.step("fixture-batch", rows)
        assert batch["accepted"] == 3 and batch["state"] == "closed"
        for external_id in ("a", "b", "c"):
            service.coding.submit(decision(external_id, "reviewer-a", "positive"))
            service.coding.submit(decision(external_id, "reviewer-b", "positive"))
        # 来源窗口先声明承诺；未到达的一条让报告保持暂定，不能当作完整覆盖。
        service.frames.open("north", "s1", query()["start"], query()["end"], 3)
        service.frames.open("north-b", "s2", query()["start"], query()["end"], 1)
        service.frames.open("south", "s3", query()["start"], query()["end"], 0)
        service.frames.close("north-b")
        service.frames.close("south")
        provisional = service.releases.preview(query())
        assert not provisional["quality"]["passed"]
        assert provisional["report"]["provisional"]
        service.observations.ingest(observation("d", "s1", "story-3"))
        service.coding.submit(decision("d", "reviewer-a", "positive"))
        service.coding.submit(decision("d", "reviewer-b", "positive"))
        service.frames.close("north")
        preview = service.releases.preview(query())
        assert preview["quality"]["passed"]
        assert preview["report"]["story_count"] == 3
        release = service.releases.publish("november", query(), preview["token"])
        assert release["report"]["story_count"] == 3
        assert release["report"]["coverage"]["status"] == "complete"
        service.observations.retract("local-fixture", "c", "bad source record", expected_revision=1)
        comparison = service.releases.compare("november")
        assert comparison["story_count_delta"] == -1
        assert comparison["coverage_changed"]
        assert service.releases.describe("november")["report"]["story_count"] == 3
        assert service.releases.describe("november")["report"]["coverage"]["status"] == "complete"
        audit = service.audit.run()
        assert audit["ok"]
        result = {"batch": batch["state"], "released_stories": 3,
                  "current_delta": comparison["story_count_delta"],
                  "frozen_coverage": "complete", "audit": audit["ok"]}
        service.close()
        return result


if __name__ == "__main__":
    print(json.dumps(scenario(), ensure_ascii=False, sort_keys=True))

