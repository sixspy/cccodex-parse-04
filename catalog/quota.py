"""每日解析额度的结算与候补匣逻辑。

结算日只取后台时钟（settings.QUOTA_TODAY 可钉死），额度扣减用单条 CAS
更新落到 QuotaDay.used，SQLite 上没有 select_for_update 也可保证末位额度
只被先到者取走。
"""

import datetime
import time
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, OperationalError
from django.utils import timezone

from catalog.models import Candidate, QuotaDay, QuotaEvent

POLICY_LABELS = {
    "candidate": "候补匣",
    "fail": "当场失败（额度已满）",
}
POLICY_CANDIDATE = "candidate"
POLICY_FAIL = "fail"

PARSABLE_STATUSES = ("待解析", "解析失败", "额度已满")


def today() -> datetime.date:
    """后台结算日；settings.QUOTA_TODAY 用于测试钉死。"""
    pinned = getattr(settings, "QUOTA_TODAY", None)
    if isinstance(pinned, datetime.datetime):
        return timezone.localtime(pinned).date() if timezone.is_aware(pinned) else pinned.date()
    if isinstance(pinned, datetime.date):
        return pinned
    if isinstance(pinned, str) and pinned.strip():
        try:
            return datetime.date.fromisoformat(pinned.strip())
        except ValueError:
            pass
    return timezone.localdate()


def daily_quota() -> int:
    try:
        return max(0, int(getattr(settings, "DAILY_QUOTA", 0) or 0))
    except (TypeError, ValueError):
        return 0


def normalize_policy(value):
    """把 candidate/fail 与中文处理名规整为策略键。"""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    low = raw.lower()
    if low in ("candidate", "queue", "queued", "next_day"):
        return POLICY_CANDIDATE
    if low in ("fail", "failed", "failure", "reject"):
        return POLICY_FAIL
    if "候补" in raw or "次日" in raw or "匣" in raw:
        return POLICY_CANDIDATE
    if "失败" in raw or "额度已满" in raw or "已满" in raw or "拒绝" in raw:
        return POLICY_FAIL
    return None


def effective_policy() -> str:
    return normalize_policy(getattr(settings, "OVERAGE_DEFAULT", None)) or POLICY_CANDIDATE


def policy_label(policy) -> str:
    return POLICY_LABELS.get(normalize_policy(policy) or "", "候补匣")


def source_rows_for(job) -> int:
    """沿用既有源行公式，供首次解析落库。"""
    return 12 + (job.id * 2) % 9


def preview_day(day: datetime.date) -> QuotaDay:
    """只读预览：GET 页面绝不落库，避免破坏 set-once 与 override_settings。"""
    row = QuotaDay.objects.filter(day=day).first()
    if row is not None:
        return row
    return QuotaDay(day=day, quota=daily_quota(), used=0, policy=effective_policy())


def policy_for(day: datetime.date) -> str:
    policy = QuotaDay.objects.filter(day=day).values_list("policy", flat=True).first()
    return normalize_policy(policy) or effective_policy()


def get_or_create_day(day: datetime.date, quota=None, policy=None) -> QuotaDay:
    quota = daily_quota() if quota is None else max(0, int(quota))
    policy = normalize_policy(policy) or effective_policy()
    try:
        row, _ = QuotaDay.objects.get_or_create(
            day=day, defaults={"quota": quota, "policy": policy}
        )
    except IntegrityError:
        row = QuotaDay.objects.get(day=day)
    return row


@dataclass
class ClaimResult:
    granted: int
    remaining: int
    race_lost: bool
    exhausted: bool


def claim(day_row: QuotaDay, want: int, attempts: int = 12) -> ClaimResult:
    """原子抢占额度；每次 CAS 失败都重读重算，绝不多发。

    返回 granted 为本次实际允许的成功行数。remaining<=0 时直接判 exhausted，
    不做零增量子更新。
    """
    want = max(0, int(want or 0))
    race_lost = False
    for _ in range(attempts):
        day_row.refresh_from_db(fields=["quota", "used"])
        remaining = max(0, day_row.quota - day_row.used)
        if remaining <= 0:
            return ClaimResult(0, 0, race_lost, True)
        if want <= 0:
            return ClaimResult(0, remaining, race_lost, False)
        grant = min(want, remaining)
        try:
            updated = QuotaDay.objects.filter(pk=day_row.pk, used=day_row.used).update(
                used=day_row.used + grant
            )
        except OperationalError:
            # SQLite 写锁竞争：短暂退避后按最新 used 重算。
            race_lost = True
            time.sleep(0.02)
            continue
        if updated:
            day_row.used += grant
            return ClaimResult(grant, max(0, day_row.quota - day_row.used), race_lost, False)
        race_lost = True
    day_row.refresh_from_db(fields=["quota", "used"])
    remaining = max(0, day_row.quota - day_row.used)
    return ClaimResult(0, remaining, True, remaining <= 0)


def record_event(day, *, kind, job=None, candidate=None, rows=0, policy="", message=""):
    return QuotaEvent.objects.create(
        day=day,
        job=job,
        candidate=candidate,
        kind=kind,
        rows=int(rows or 0),
        policy=normalize_policy(policy) or "",
        message=message,
    )


def settle_parse(job, day_row: QuotaDay) -> dict:
    """按后台结算日与当日唯一策略结算一次解析。"""
    day = day_row.day
    policy = normalize_policy(day_row.policy) or effective_policy()
    source = job.source_rows or source_rows_for(job)
    result = claim(day_row, source)
    granted = result.granted
    excess = max(0, source - granted)

    job.source_rows = source
    job.parsed = granted
    candidate = None

    if policy == POLICY_CANDIDATE and excess > 0:
        candidate = Candidate.objects.create(
            job=job,
            origin_day=day,
            target_day=day + datetime.timedelta(days=1),
            rows=excess,
        )
        record_event(
            day,
            kind="candidate",
            job=job,
            candidate=candidate,
            rows=excess,
            policy=policy,
            message=f"超额 {excess} 行进次日候补匣，需手动继续",
        )
        job.status = "已解析待入库" if granted > 0 else "候补待续"
    elif policy == POLICY_FAIL and excess > 0:
        if granted > 0:
            record_event(
                day,
                kind="quota_full",
                job=job,
                rows=excess,
                policy=policy,
                message=f"额度已满：先填满 {granted} 行，超出 {excess} 行当场判失败",
            )
        job.status = "额度已满"
    else:
        job.status = "已解析待入库"

    if granted > 0:
        record_event(
            day,
            kind="grant",
            job=job,
            rows=granted,
            policy=policy,
            message=f"解析成功 {granted} 行，实时记入后台额度",
        )

    if granted == 0:
        kind = "race_lost" if result.race_lost else "quota_full"
        record_event(
            day,
            kind=kind,
            job=job,
            rows=source,
            policy=policy,
            message="抢额失败：额度已满，末位额度已被先到者取走",
        )

    # 本日只能定一种超额处理：一旦实际结算，就把本日策略钉死。
    if not day_row.policy_locked:
        day_row.policy_locked = True
        day_row.save(update_fields=["policy_locked"])

    job.save(update_fields=["source_rows", "parsed", "status"])
    return {
        "granted": granted,
        "excess": excess,
        "candidate": candidate,
        "policy": policy,
        "race_lost": result.race_lost,
        "exhausted": result.exhausted,
    }


def resume_candidate(candidate: Candidate) -> dict:
    """候补匣手动继续：只在新的一天按新日额度重算，绝不自动开析。"""
    day = today()
    if candidate.status != "待续":
        return {"resumed": False, "reason": "该候补已处理"}
    if candidate.target_day > day:
        return {"resumed": False, "reason": "未到续析日，跨日后需再点继续"}

    # 排他状态转移，避免两次点击同时扣额。
    moved = Candidate.objects.filter(pk=candidate.pk, status="待续").update(status="已续")
    if not moved:
        return {"resumed": False, "reason": "该候补已处理"}

    day_row = get_or_create_day(day)
    policy = normalize_policy(day_row.policy) or effective_policy()
    result = claim(day_row, candidate.rows)
    granted = result.granted
    excess = max(0, candidate.rows - granted)

    if granted == 0:
        # 没抢到额度：候补回到待续，保留顺序，稍后可再点。
        Candidate.objects.filter(pk=candidate.pk).update(status="待续")
        record_event(
            day,
            kind="race_lost" if result.race_lost else "quota_full",
            job=candidate.job,
            candidate=candidate,
            rows=candidate.rows,
            policy=policy,
            message="抢额失败：额度已满，候补暂留待续",
        )
        return {"resumed": False, "granted": 0, "excess": candidate.rows, "policy": policy}

    job = candidate.job
    job.parsed = int(job.parsed or 0) + granted
    record_event(
        day,
        kind="resume",
        job=job,
        candidate=candidate,
        rows=granted,
        policy=policy,
        message=f"候补继续成功 {granted} 行，按新日额度重算",
    )

    new_candidate = None
    if excess > 0 and policy == POLICY_CANDIDATE:
        new_candidate = Candidate.objects.create(
            job=job,
            origin_day=day,
            target_day=day + datetime.timedelta(days=1),
            rows=excess,
        )
        record_event(
            day,
            kind="candidate",
            job=job,
            candidate=new_candidate,
            rows=excess,
            policy=policy,
            message=f"候补仍超 {excess} 行，转次日候补匣",
        )
        job.status = "已解析待入库" if job.parsed > 0 else "候补待续"
    elif excess > 0:
        record_event(
            day,
            kind="quota_full",
            job=job,
            candidate=candidate,
            rows=excess,
            policy=policy,
            message=f"额度已满：候补超出 {excess} 行当场判失败",
        )
        job.status = "额度已满"
    else:
        job.status = "已解析待入库"

    if not day_row.policy_locked:
        day_row.policy_locked = True
        day_row.save(update_fields=["policy_locked"])

    job.save(update_fields=["parsed", "status"])
    return {
        "resumed": True,
        "granted": granted,
        "excess": excess,
        "policy": policy,
        "candidate": new_candidate,
    }
