import datetime

import pytest
from django.test import override_settings

from catalog import quota
from catalog.models import Candidate, ParseJob, QuotaDay, QuotaEvent
from catalog.seed import ensure_seed

DAY = datetime.date(2026, 11, 14)
NEXT = DAY + datetime.timedelta(days=1)


def _login(client):
    r = client.post("/login", {"username": "admin", "password": "123456"})
    assert r.status_code == 302


def _job(rows=8, code="t1", status="待解析"):
    return ParseJob.objects.create(
        title="测试任务", code=code, status=status, source_uri="https://mock.local/t",
        source_rows=rows,
    )


def _post_parse(client, job_id, day=DAY, policy="candidate"):
    return client.post(
        f"/jobs/{job_id}/parse/", {"day": day.isoformat(), "policy": policy}
    )


# --- 候补策略 -------------------------------------------------------------

@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_candidate_overflow_goes_to_next_day(client):
    _login(client)
    job = _job(rows=14)
    _post_parse(client, job.id, policy="candidate")

    job.refresh_from_db()
    day_row = QuotaDay.objects.get(day=DAY)
    assert day_row.used == 10
    assert day_row.used <= day_row.quota
    assert job.parsed == 10
    assert job.status == "已解析待入库"

    cand = Candidate.objects.get(job=job)
    assert cand.rows == 4
    assert cand.origin_day == DAY
    assert cand.target_day == NEXT
    assert cand.status == "待续"

    detail = client.get(f"/jobs/{job.id}/").content.decode("utf-8")
    assert "详情旁注" in detail
    assert "第 1 位" in detail


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=6, OVERAGE_DEFAULT="candidate")
def test_candidate_all_overflow_marks_waiting(client):
    _login(client)
    first = _job(rows=6, code="a")
    second = _job(rows=5, code="b")
    _post_parse(client, first.id)
    _post_parse(client, second.id)

    second.refresh_from_db()
    assert second.parsed == 0
    assert second.status == "候补待续"
    cand = Candidate.objects.get(job=second)
    assert cand.rows == 5


# --- 当场失败策略 ---------------------------------------------------------

@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="fail")
def test_fail_policy_fills_quota_then_marks_full(client):
    _login(client)
    job = _job(rows=14)
    _post_parse(client, job.id, policy="fail")

    job.refresh_from_db()
    day_row = QuotaDay.objects.get(day=DAY)
    assert day_row.used == 10  # 先填满额度
    assert job.parsed == 10
    assert job.status == "额度已满"
    assert Candidate.objects.count() == 0

    detail = client.get(f"/jobs/{job.id}/").content.decode("utf-8")
    assert "额度已满" in detail


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=4, OVERAGE_DEFAULT="fail")
def test_fail_policy_exhausted_has_zero_success(client):
    _login(client)
    job = _job(rows=9)
    _post_parse(client, job.id, policy="fail")

    job.refresh_from_db()
    assert job.parsed == 4
    assert job.status == "额度已满"
    assert Candidate.objects.count() == 0


# --- 超额处理漂移 ---------------------------------------------------------

@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_drift_when_policy_changed_after_page_load(client):
    _login(client)
    job = _job(rows=4)
    client.get(f"/jobs/{job.id}/")  # 页面快照 candidate

    # 后台把本日策略改成 fail
    QuotaDay.objects.create(day=DAY, quota=10, policy="fail", policy_locked=True)

    r = client.post(f"/jobs/{job.id}/parse/", {"day": DAY.isoformat(), "policy": "candidate"})
    body = r.content.decode("utf-8")
    assert "超额处理偏了" in body
    job.refresh_from_db()
    assert job.status == "待解析"
    assert job.parsed == 0
    assert QuotaEvent.objects.filter(kind="grant").count() == 0


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_drift_when_day_rolled(client):
    _login(client)
    job = _job(rows=4)
    client.get(f"/jobs/{job.id}/")

    with override_settings(QUOTA_TODAY=NEXT):
        r = client.post(f"/jobs/{job.id}/parse/", {"day": DAY.isoformat(), "policy": "candidate"})
        assert "超额处理偏了" in r.content.decode("utf-8")
        job.refresh_from_db()
        assert job.status == "待解析"
        assert not QuotaDay.objects.filter(day=NEXT).exists()


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_direct_post_without_snapshot_is_not_drift(client):
    _login(client)
    job = _job(rows=4)
    r = client.post(f"/jobs/{job.id}/parse/")
    assert r.status_code == 302
    job.refresh_from_db()
    assert job.parsed == 4
    assert job.status == "已解析待入库"


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_direct_post_policy_is_honoured_when_day_unset(client):
    _login(client)
    job = _job(rows=14)
    r = client.post(f"/jobs/{job.id}/parse/", {"policy": "fail"})
    assert r.status_code == 302
    job.refresh_from_db()
    assert job.status == "额度已满"
    assert job.parsed == 10
    assert Candidate.objects.count() == 0
    assert QuotaDay.objects.get(day=DAY).policy == "fail"


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_drift_on_invalid_policy_value(client):
    _login(client)
    job = _job(rows=4)
    r = client.post(f"/jobs/{job.id}/parse/", {"day": DAY.isoformat(), "policy": "乱写"})
    assert "超额处理偏了" in r.content.decode("utf-8")
    job.refresh_from_db()
    assert job.status == "待解析"


# --- 末位额度争抢 ---------------------------------------------------------

@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=1, OVERAGE_DEFAULT="fail")
def test_last_slot_only_first_wins(client):
    _login(client)
    first = _job(rows=5, code="first")
    second = _job(rows=5, code="second")

    _post_parse(client, first.id, policy="fail")
    _post_parse(client, second.id, policy="fail")

    day_row = QuotaDay.objects.get(day=DAY)
    assert day_row.used == 1
    assert day_row.used <= day_row.quota

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.parsed == 1
    assert second.parsed == 0
    assert second.status == "额度已满"

    detail = client.get(f"/jobs/{second.id}/").content.decode("utf-8")
    assert "抢额失败" in detail


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=3, OVERAGE_DEFAULT="fail")
def test_claim_never_oversubscribes(client):
    _login(client)
    _job(rows=0, code="x")
    day_row = quota.get_or_create_day(DAY)
    r1 = quota.claim(day_row, 2)
    r2 = quota.claim(day_row, 2)
    r3 = quota.claim(day_row, 2)
    assert (r1.granted, r2.granted, r3.granted) == (2, 1, 0)
    assert r3.exhausted is True
    day_row.refresh_from_db()
    assert day_row.used == 3


@pytest.mark.django_db(transaction=True)
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=1, OVERAGE_DEFAULT="fail")
def test_concurrent_last_slot_only_one_winner():
    import threading

    quota.get_or_create_day(DAY)
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def worker():
        from django.db import close_old_connections

        close_old_connections()
        try:
            barrier.wait(timeout=5)
            day_row = QuotaDay.objects.get(day=DAY)
            res = quota.claim(day_row, 1)
            with lock:
                results.append(res.granted)
        finally:
            close_old_connections()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == [0, 1]  # 末位额度只留先到
    assert QuotaDay.objects.get(day=DAY).used == 1


# --- 候补跨日 -------------------------------------------------------------

@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=4, OVERAGE_DEFAULT="candidate")
def test_candidate_does_not_auto_parse_across_day(client):
    _login(client)
    job = _job(rows=10)
    _post_parse(client, job.id)
    cand = Candidate.objects.get(job=job)

    # 次日什么也不做：不应自动开析
    with override_settings(QUOTA_TODAY=NEXT):
        cand.refresh_from_db()
        assert cand.status == "待续"
        assert not QuotaDay.objects.filter(day=NEXT).exists()
        assert Candidate.objects.filter(status="待续").count() == 1


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=4, OVERAGE_DEFAULT="candidate")
def test_resume_before_target_day_is_rejected(client):
    _login(client)
    job = _job(rows=10)
    _post_parse(client, job.id)
    cand = Candidate.objects.get(job=job)

    r = client.post(f"/candidates/{cand.id}/resume/")
    assert r.status_code == 302
    cand.refresh_from_db()
    assert cand.status == "待续"
    assert not QuotaDay.objects.filter(day=NEXT).exists()


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=4, OVERAGE_DEFAULT="candidate")
def test_resume_next_day_recalculates_with_new_quota(client):
    _login(client)
    job = _job(rows=10)
    _post_parse(client, job.id)  # 成功 4，候补 6
    cand = Candidate.objects.get(job=job)

    with override_settings(QUOTA_TODAY=NEXT, DAILY_QUOTA=8):
        r = client.post(f"/candidates/{cand.id}/resume/", follow=True)
        assert r.status_code == 200

    cand.refresh_from_db()
    assert cand.status == "已续"
    job.refresh_from_db()
    assert job.parsed == 4 + 6  # 新日额度 8 足够覆盖 6
    next_row = QuotaDay.objects.get(day=NEXT)
    assert next_row.used == 6
    assert next_row.used <= next_row.quota
    assert Candidate.objects.filter(job=job, status="待续").count() == 0


# --- 额度列表 -------------------------------------------------------------

@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_quota_list_footer_exposes_quota_and_candidates(client):
    _login(client)
    job = _job(rows=15)
    _post_parse(client, job.id)

    body = client.get("/quota/").content.decode("utf-8")
    assert "额度列表" not in body or "每日解析额度" in body
    assert "额度 10" in body
    assert "今日已析 10" in body
    assert "候补数 5" in body
    assert "候补匣" in body


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_policy_can_only_be_set_once(client):
    _login(client)
    client.post("/quota/set/", {"quota": "10", "policy": "candidate"})
    row = QuotaDay.objects.get(day=DAY)
    assert row.policy == "candidate" and row.policy_locked is True

    r = client.post("/quota/set/", {"quota": "10", "policy": "fail"})
    assert "本日超额策略已定" in r.content.decode("utf-8")
    row.refresh_from_db()
    assert row.policy == "candidate"


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_chinese_policy_and_aliases_are_accepted(client):
    _login(client)
    client.post("/quota/set/", {"额度": "6", "处理名": "当场失败"})
    row = QuotaDay.objects.get(day=DAY)
    assert row.policy == "fail"
    assert row.quota == 6


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_quota_list_works_with_setting_already_locked(client):
    _login(client)
    job = _job(rows=15)
    _post_parse(client, job.id)
    assert QuotaDay.objects.get(day=DAY).policy_locked is True
    body = client.get("/quota/").content.decode("utf-8")
    assert "额度 10" in body and "候补数 5" in body


@pytest.mark.django_db
@override_settings(QUOTA_TODAY=DAY, DAILY_QUOTA=10, OVERAGE_DEFAULT="candidate")
def test_hidden_fields_and_labels_present_on_detail(client):
    _login(client)
    job = _job(rows=3)
    body = client.get(f"/jobs/{job.id}/").content.decode("utf-8")
    assert 'name="day"' in body and 'name="policy"' in body
    assert "额度" in body and "今日已析" in body
