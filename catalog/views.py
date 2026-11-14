import datetime

from django.conf import settings
from django.db.models import Sum
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods, require_POST

from catalog import quota
from catalog.models import Candidate, ParseJob, QuotaDay, QuotaEvent


def _authed(request) -> bool:
    return request.session.get("user") == settings.ADMIN_USER


def _pending_candidate_rows(day=None):
    qs = Candidate.objects.filter(status="待续")
    if day is not None:
        qs = qs.filter(origin_day=day)
    return qs.aggregate(total=Sum("rows"))["total"] or 0


def _quota_summary(day=None):
    day = day or quota.today()
    row = quota.preview_day(day)
    used = row.used
    total = row.quota
    pct = int(round(used * 100 / total)) if total else 0
    pending = _pending_candidate_rows()
    return {
        "day": day,
        "quota": total,
        "used": used,
        "remaining": max(0, total - used),
        "pct": min(100, pct),
        "policy": quota.normalize_policy(row.policy) or quota.effective_policy(),
        "policy_label": quota.policy_label(row.policy),
        "pending_candidates": pending,
    }


def _detail_context(request, job, error="", notice=""):
    day = quota.today()
    day_row = quota.preview_day(day)
    policy = quota.normalize_policy(day_row.policy) or quota.effective_policy()
    request.session["quota_snapshot"] = {"day": day.isoformat(), "policy": policy}

    candidates = list(Candidate.objects.filter(job=job).order_by("origin_day", "id"))
    for cand in candidates:
        # 候补匣顺序按当日全局入匣先后排位
        cand.position = Candidate.objects.filter(
            origin_day=cand.origin_day, id__lte=cand.id
        ).count()
        cand.can_resume = cand.status == "待续" and cand.target_day <= day
    events = list(QuotaEvent.objects.filter(job=job).order_by("id"))
    snapshot = {"day": day.isoformat(), "policy": policy}

    return {
        "job": job,
        "lab": settings.LAB_NAME,
        "user": request.session["user"],
        "summary": _quota_summary(day),
        "snapshot": snapshot,
        "candidates": candidates,
        "events": events,
        "error": error,
        "notice": notice,
        "policy_labels": quota.POLICY_LABELS,
        "today": day,
    }


def root(request):
    return HttpResponseRedirect("/jobs/" if _authed(request) else "/login")


@require_http_methods(["GET", "POST"])
def login_page(request):
    err = ""
    if request.method == "POST":
        user = request.POST.get("username", "").strip()
        pwd = request.POST.get("password", "")
        if user == settings.ADMIN_USER and pwd == settings.ADMIN_PASSWORD:
            request.session["user"] = user
            return HttpResponseRedirect("/jobs/")
        err = "账号或密码错误"
    return render(request, "login.html", {"error": err, "lab": settings.LAB_NAME})


def logout_view(request):
    request.session.flush()
    return HttpResponseRedirect("/login")


def job_list(request):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    items = list(ParseJob.objects.all())
    return render(
        request,
        "jobs_list.html",
        {
            "items": items,
            "lab": settings.LAB_NAME,
            "user": request.session["user"],
            "summary": _quota_summary(),
        },
    )


def job_detail(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    return render(request, "job_detail.html", _detail_context(request, job))


def _parse_day(value):
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return False


def _drifted(request):
    """返回 True 表示页面快照与后台结算日/策略漂移。"""
    snap = request.session.get("quota_snapshot") or {}
    posted_day = request.POST.get("day")
    posted_policy = request.POST.get("policy")

    # 直接 POST、既无隐藏字段也无页面快照：没有可判的陈旧快照，不拦。
    if posted_day in (None, "") and posted_policy in (None, "") and not snap:
        return False

    sent_day = _parse_day(posted_day if posted_day not in (None, "") else snap.get("day"))
    if sent_day is False:
        return True
    today_date = quota.today()
    if sent_day is not None and sent_day != today_date:
        return True

    sent_policy = posted_policy if posted_policy not in (None, "") else snap.get("policy")
    submitted = quota.normalize_policy(sent_policy)
    if sent_policy not in (None, "") and submitted is None:
        return True

    current = quota.policy_for(today_date)
    has_backend_policy = QuotaDay.objects.filter(day=today_date).exists()
    # 本日尚无已写策略、页面也无快照时，随表单带来的合法策略可作本日策略。
    if submitted is not None and submitted != current and (has_backend_policy or snap):
        return True
    # 页面显示的策略与后台现行策略不一致。
    if submitted is None and snap.get("policy") not in (None, ""):
        prior = quota.normalize_policy(snap.get("policy"))
        if prior is not None and prior != current:
            return True
    return False


@require_POST
def job_parse(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)

    if _drifted(request):
        return render(
            request,
            "job_detail.html",
            _detail_context(request, job, error="超额处理偏了"),
            status=200,
        )

    if job.status in quota.PARSABLE_STATUSES:
        day = quota.today()
        day_row = quota.get_or_create_day(day)
        if not day_row.policy_locked:
            submitted = quota.normalize_policy(request.POST.get("policy"))
            if submitted and submitted != quota.normalize_policy(day_row.policy):
                day_row.policy = submitted
                day_row.save(update_fields=["policy"])
        quota.settle_parse(job, day_row)
    return HttpResponseRedirect(f"/jobs/{job.id}/")


@require_POST
def job_land(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    if job.status == "已解析待入库":
        job.landed = job.parsed
        job.status = "已入库"
        job.save(update_fields=["landed", "status"])
    return HttpResponseRedirect(f"/jobs/{job.id}/")


def _quota_list_context(day, error="", notice=""):
    rows = list(QuotaDay.objects.all().order_by("-day"))
    if not any(r.day == day for r in rows):
        rows.insert(0, quota.preview_day(day))

    entries = []
    total_candidates = 0
    for row in rows:
        cand_rows = _pending_candidate_rows(row.day)
        total_candidates += cand_rows
        entries.append(
            {
                "day": row.day,
                "quota": row.quota,
                "used": row.used,
                "remaining": max(0, row.quota - row.used),
                "candidates": cand_rows,
                "policy": quota.normalize_policy(row.policy) or quota.effective_policy(),
                "policy_label": quota.policy_label(row.policy),
            }
        )

    today_row = next((e for e in entries if e["day"] == day), None)
    return {
        "entries": entries,
        "totals": {
            "quota": today_row["quota"] if today_row else quota.daily_quota(),
            "used": today_row["used"] if today_row else 0,
            "candidates": total_candidates,
        },
        "today": day,
        "summary": _quota_summary(day),
        "lab": settings.LAB_NAME,
        "error": error,
        "notice": notice,
    }


def quota_list(request):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    ctx = _quota_list_context(
        quota.today(), notice="设置已保存" if request.GET.get("notice") else ""
    )
    ctx["user"] = request.session["user"]
    return render(request, "quota_list.html", ctx)


@require_POST
def quota_set(request):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    day = quota.today()
    day_row = quota.get_or_create_day(day)

    raw_quota = (
        request.POST.get("quota")
        or request.POST.get("daily_quota")
        or request.POST.get("额度")
    )
    raw_policy = (
        request.POST.get("policy")
        or request.POST.get("overage")
        or request.POST.get("处理名")
        or request.POST.get("超额处理")
    )
    error = ""
    notice = ""

    if day_row.policy_locked:
        new_policy = quota.normalize_policy(raw_policy)
        if new_policy and new_policy != quota.normalize_policy(day_row.policy):
            error = "本日超额策略已定，不可更改"
        elif raw_quota not in (None, ""):
            try:
                new_quota = int(raw_quota)
            except (TypeError, ValueError):
                new_quota = None
            if new_quota is None or new_quota < day_row.used:
                error = "额度不能低于今日已解析行数"
            elif new_quota != day_row.quota:
                day_row.quota = new_quota
                day_row.save(update_fields=["quota"])
                notice = "今日额度已更新"
    else:
        new_policy = quota.normalize_policy(raw_policy) or quota.effective_policy()
        new_quota = day_row.quota
        if raw_quota not in (None, ""):
            try:
                new_quota = max(0, int(raw_quota))
            except (TypeError, ValueError):
                error = "额度需为整数"
        if new_quota < day_row.used:
            error = "额度不能低于今日已解析行数"
        if not error:
            changed = new_policy != day_row.policy or new_quota != day_row.quota
            day_row.policy = new_policy
            day_row.quota = new_quota
            day_row.policy_locked = True
            day_row.save(update_fields=["policy", "quota", "policy_locked"])
            if changed:
                quota.record_event(
                    day,
                    kind="policy",
                    policy=new_policy,
                    message=f"超额策略定为{quota.policy_label(new_policy)}，额度 {new_quota} 行",
                )
            notice = "本日超额策略与额度已定"

    if error:
        ctx = _quota_list_context(day, error=error, notice=notice)
        ctx["user"] = request.session["user"]
        return render(request, "quota_list.html", ctx, status=200)

    return HttpResponseRedirect("/quota/?" + ("notice=1" if notice else ""))


@require_POST
def candidate_resume(request, candidate_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    candidate = get_object_or_404(Candidate, pk=candidate_id)
    quota.resume_candidate(candidate)
    return HttpResponseRedirect(f"/jobs/{candidate.job_id}/")
