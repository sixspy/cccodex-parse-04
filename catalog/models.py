from django.db import models


class ParseJob(models.Model):
    title = models.CharField(max_length=120)
    code = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=32, default="待解析")
    source_uri = models.CharField(max_length=255)
    parsed = models.IntegerField(default=0)
    landed = models.IntegerField(default=0)
    source_rows = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class QuotaDay(models.Model):
    POLICY_CHOICES = [
        ("candidate", "候补匣"),
        ("fail", "当场失败"),
    ]

    day = models.DateField(unique=True)
    quota = models.IntegerField(default=0)
    used = models.IntegerField(default=0)
    policy = models.CharField(max_length=16, choices=POLICY_CHOICES, default="candidate")
    policy_locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-day"]

    def remaining(self) -> int:
        return max(0, self.quota - self.used)


class Candidate(models.Model):
    STATUS_CHOICES = [
        ("待续", "待续"),
        ("已续", "已续"),
        ("已废", "已废"),
    ]

    job = models.ForeignKey(ParseJob, on_delete=models.CASCADE, related_name="candidates")
    origin_day = models.DateField()
    target_day = models.DateField()
    rows = models.IntegerField(default=0)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default="待续")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["origin_day", "id"]
        indexes = [models.Index(fields=["origin_day", "status"])]


class QuotaEvent(models.Model):
    KIND_CHOICES = [
        ("grant", "额度扣减"),
        ("candidate", "入候补匣"),
        ("quota_full", "额度已满"),
        ("race_lost", "抢额失败"),
        ("resume", "候补续析"),
        ("policy", "超额策略"),
    ]

    day = models.DateField()
    job = models.ForeignKey(ParseJob, null=True, blank=True, on_delete=models.CASCADE, related_name="quota_events")
    candidate = models.ForeignKey(
        Candidate, null=True, blank=True, on_delete=models.CASCADE, related_name="quota_events"
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    rows = models.IntegerField(default=0)
    policy = models.CharField(max_length=16, blank=True)
    message = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["job", "created_at"]),
            models.Index(fields=["day", "kind"]),
        ]
