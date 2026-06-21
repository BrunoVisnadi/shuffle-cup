from django.contrib import admin

from .models import (
    BallotToken, BreakChoice, Debater, DebaterPartnerConflict, Judge,
    JudgeAllocation, JudgeDebaterConflict, PairResult, ParticipantSlot, Room,
    Round, SiteSettings, Society, SpeakerScore, SwingSlot, TemporaryPair,
)


@admin.register(Debater)
class DebaterAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "society", "is_novice", "active")
    list_filter = ("active", "is_novice", "society")
    search_fields = ("name", "email")


@admin.register(Judge)
class JudgeAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "society", "active")
    list_filter = ("active", "society")
    search_fields = ("name", "email")


@admin.register(Round)
class RoundAdmin(admin.ModelAdmin):
    list_display = ("name", "number", "kind", "silent", "draw_published", "results_confirmed")
    list_filter = ("kind", "silent")


admin.site.register(Society)
admin.site.register(Room)
admin.site.register(SwingSlot)
admin.site.register(TemporaryPair)
admin.site.register(ParticipantSlot)
admin.site.register(SpeakerScore)
admin.site.register(PairResult)
admin.site.register(JudgeAllocation)
admin.site.register(BallotToken)
admin.site.register(DebaterPartnerConflict)
admin.site.register(JudgeDebaterConflict)
admin.site.register(BreakChoice)
admin.site.register(SiteSettings)
admin.site.site_header = "Administração da Shuffle Cup"
