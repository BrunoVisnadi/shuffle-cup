import secrets

from django.core.exceptions import ValidationError
from django.db import models


POSITIONS = [(p, p) for p in ("OG", "OO", "CG", "CO")]


class Society(models.Model):
    name = models.CharField(max_length=200, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Debater(models.Model):
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True, null=True, unique=True)
    society = models.ForeignKey(Society, blank=True, null=True, on_delete=models.SET_NULL)
    is_novice = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Judge(models.Model):
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True, null=True, unique=True)
    society = models.ForeignKey(Society, blank=True, null=True, on_delete=models.SET_NULL)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Round(models.Model):
    PRELIM = "prelim"
    OPEN_SEMI = "open_semifinal"
    OPEN_FINAL = "open_final"
    NOVICE_FINAL = "novice_final"
    KINDS = [(PRELIM, "Preliminar"), (OPEN_SEMI, "Semifinal Open"), (OPEN_FINAL, "Final Open"), (NOVICE_FINAL, "Final novice")]

    name = models.CharField(max_length=100)
    number = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=30, choices=KINDS, default=PRELIM)
    silent = models.BooleanField(default=False)
    draw_published = models.BooleanField(default=False)
    results_confirmed = models.BooleanField(default=False)

    class Meta:
        ordering = ["number", "id"]
        unique_together = [("number", "kind")]

    def __str__(self):
        return self.name


class Room(models.Model):
    round = models.ForeignKey(Round, related_name="rooms", on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    ordinal = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["ordinal"]
        unique_together = [("round", "ordinal")]

    def __str__(self):
        return f"{self.round}: {self.name}"


class SwingSlot(models.Model):
    round = models.ForeignKey(Round, related_name="swings", on_delete=models.CASCADE)
    display_name = models.CharField(max_length=100)
    room = models.ForeignKey(Room, related_name="swings", blank=True, null=True, on_delete=models.CASCADE)
    position = models.CharField(max_length=2, choices=POSITIONS, blank=True)

    class Meta:
        unique_together = [("round", "display_name")]

    def __str__(self):
        return self.display_name


class TemporaryPair(models.Model):
    round = models.ForeignKey(Round, related_name="pairs", on_delete=models.CASCADE)
    room = models.ForeignKey(Room, related_name="pairs", on_delete=models.CASCADE)
    position = models.CharField(max_length=2, choices=POSITIONS)

    class Meta:
        ordering = ["room__ordinal", "id"]
        unique_together = [("room", "position")]

    def __str__(self):
        return f"{self.room} {self.position}"

    @property
    def names(self):
        return " & ".join(slot.display_name for slot in self.slots.all())


class ParticipantSlot(models.Model):
    pair = models.ForeignKey(TemporaryPair, related_name="slots", on_delete=models.CASCADE)
    debater = models.ForeignKey(Debater, related_name="participant_slots", blank=True, null=True, on_delete=models.CASCADE)
    swing = models.ForeignKey(SwingSlot, related_name="participant_slots", blank=True, null=True, on_delete=models.CASCADE)
    order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["order"]
        constraints = [models.UniqueConstraint(fields=["pair", "order"], name="unique_pair_slot")]

    def clean(self):
        if (self.debater_id is None) == (self.swing_id is None):
            raise ValidationError("Uma vaga deve conter exatamente um debatedor ou swing.")

    @property
    def display_name(self):
        return self.debater.name if self.debater_id else self.swing.display_name

    @property
    def society_name(self):
        return self.debater.society.name if self.debater_id and self.debater.society_id else ""

    @property
    def is_swing(self):
        return self.swing_id is not None


class SpeakerScore(models.Model):
    round = models.ForeignKey(Round, related_name="speaker_scores", on_delete=models.CASCADE)
    room = models.ForeignKey(Room, related_name="speaker_scores", on_delete=models.CASCADE)
    participant_slot = models.OneToOneField(ParticipantSlot, related_name="speaker_score", on_delete=models.CASCADE)
    speaker_points = models.DecimalField(max_digits=5, decimal_places=2)
    confirmed = models.BooleanField(default=False)


class PairResult(models.Model):
    round = models.ForeignKey(Round, related_name="pair_results", on_delete=models.CASCADE)
    room = models.ForeignKey(Room, related_name="pair_results", on_delete=models.CASCADE)
    temporary_pair = models.OneToOneField(TemporaryPair, related_name="result", on_delete=models.CASCADE)
    rank = models.PositiveSmallIntegerField(blank=True, null=True)
    team_points = models.PositiveSmallIntegerField(blank=True, null=True)
    advances = models.BooleanField(default=False)
    champion = models.BooleanField(default=False)
    submitted = models.BooleanField(default=False)
    confirmed = models.BooleanField(default=False)


class JudgeAllocation(models.Model):
    ROLES = [("chair", "Chair"), ("panel", "Panel")]
    round = models.ForeignKey(Round, related_name="judge_allocations", on_delete=models.CASCADE)
    room = models.ForeignKey(Room, related_name="judge_allocations", on_delete=models.CASCADE)
    judge = models.ForeignKey(Judge, related_name="allocations", on_delete=models.CASCADE)
    role = models.CharField(max_length=10, choices=ROLES)

    class Meta:
        unique_together = [("room", "judge")]


class BallotToken(models.Model):
    round = models.ForeignKey(Round, related_name="ballot_tokens", on_delete=models.CASCADE)
    room = models.ForeignKey(Room, related_name="ballot_tokens", on_delete=models.CASCADE)
    token = models.CharField(max_length=64, unique=True, default=secrets.token_urlsafe)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(blank=True, null=True)
    submitted_by_name = models.CharField(max_length=200, blank=True)
    submitted_by_email = models.EmailField(blank=True)


class DebaterPartnerConflict(models.Model):
    debater_a = models.ForeignKey(Debater, related_name="partner_conflicts_a", on_delete=models.CASCADE)
    debater_b = models.ForeignKey(Debater, related_name="partner_conflicts_b", on_delete=models.CASCADE)

    class Meta:
        unique_together = [("debater_a", "debater_b")]


class JudgeDebaterConflict(models.Model):
    judge = models.ForeignKey(Judge, related_name="debater_conflicts", on_delete=models.CASCADE)
    debater = models.ForeignKey(Debater, related_name="judge_conflicts", on_delete=models.CASCADE)

    class Meta:
        unique_together = [("judge", "debater")]


class BreakChoice(models.Model):
    CHOICES = [("open", "Semifinais Open"), ("novice", "Final novice")]
    debater = models.OneToOneField(Debater, related_name="break_choice", on_delete=models.CASCADE)
    choice = models.CharField(max_length=10, choices=CHOICES)


class SiteSettings(models.Model):
    tournament_name = models.CharField(max_length=200, default="Shuffle Cup")
    current_round = models.ForeignKey(Round, blank=True, null=True, on_delete=models.SET_NULL)
    final_tab_published = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]
