import csv
import io
import json
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import CSVUploadForm, JudgeAllocationForm
from .models import (
    BallotToken, BreakChoice, Debater, DebaterPartnerConflict, Judge,
    JudgeAllocation, JudgeDebaterConflict, PairResult, ParticipantSlot, Room,
    Round, SiteSettings, Society, SpeakerScore,
)
from .services import (
    break_lists, confirm_room, draw_warnings, generate_outround,
    generate_prelim_draw, standings, submit_elimination, submit_prelim,
)


def _setting():
    return SiteSettings.load()


def home(request):
    setting = _setting()
    rounds = Round.objects.filter(draw_published=True)
    return render(request, "tournament/home.html", {"setting": setting, "rounds": rounds})


def public_draw(request, round_id):
    round_obj = get_object_or_404(Round, pk=round_id, draw_published=True)
    rooms = round_obj.rooms.prefetch_related("pairs__slots__debater__society", "pairs__slots__swing", "judge_allocations__judge")
    return render(request, "tournament/draw.html", {"round": round_obj, "rooms": rooms})


def public_standings(request):
    completed = Round.objects.filter(kind=Round.PRELIM, results_confirmed=True, silent=False).order_by("-number").first()
    limit = completed.number if completed else 0
    silent_active = Round.objects.filter(kind=Round.PRELIM, silent=True, results_confirmed=True).exists()
    return render(request, "tournament/standings.html", {"rows": standings(round_limit=limit, public=True), "limit": limit, "silent_active": silent_active, "public": True})


def final_results(request):
    setting = _setting()
    if not setting.final_tab_published:
        raise Http404
    champions = PairResult.objects.filter(confirmed=True, champion=True).select_related("round", "temporary_pair").prefetch_related("temporary_pair__slots__debater")
    open_finalists = Debater.objects.filter(participant_slots__pair__round__kind=Round.OPEN_FINAL).distinct()
    novice_finalists = Debater.objects.filter(participant_slots__pair__round__kind=Round.NOVICE_FINAL).distinct()
    rows = standings(round_limit=5)
    speaker_rows = sorted(rows, key=lambda row: (-row["speaker_points"], row["debater"].name.lower()))
    for index, row in enumerate(speaker_rows, 1):
        row["speaker_rank"] = index
    return render(request, "tournament/final_results.html", {"setting": setting, "champions": champions, "open_finalists": open_finalists, "novice_finalists": novice_finalists, "rows": rows, "speaker_rows": speaker_rows})


@login_required
def dashboard(request):
    rounds = Round.objects.prefetch_related("rooms")
    active_count = Debater.objects.filter(active=True).count()
    swing_count = (8 - active_count % 8) % 8 if active_count else 0
    return render(request, "tournament/dashboard.html", {"rounds": rounds, "active_count": active_count, "swing_count": swing_count, "setting": _setting()})


@login_required
@require_POST
def setup_rounds(request):
    specifications = [
        ("Round 1", 1, Round.PRELIM, False), ("Round 2", 2, Round.PRELIM, False),
        ("Round 3", 3, Round.PRELIM, False), ("Round 4", 4, Round.PRELIM, True),
        ("Round 5", 5, Round.PRELIM, True), ("Open Semifinals", 6, Round.OPEN_SEMI, False),
        ("Novice Final", 6, Round.NOVICE_FINAL, False), ("Open Final", 7, Round.OPEN_FINAL, False),
    ]
    for name, number, kind, silent in specifications:
        Round.objects.update_or_create(number=number, kind=kind, defaults={"name": name, "silent": silent})
    messages.success(request, "Standard Shuffle Cup rounds are ready.")
    return redirect("dashboard")


def _bool(value):
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "1", "sim"}:
        return True
    if normalized in {"false", "no", "0", "nao", "não"}:
        return False
    raise ValueError(f"Invalid boolean: {value}")


def _find_person(model, email, name):
    if email:
        return model.objects.filter(email__iexact=email.strip()).first()
    return model.objects.filter(name__exact=name.strip()).first()


def _validate_csv(kind, rows):
    required = {
        "debaters": {"name", "email", "society", "is_novice"},
        "judges": {"name", "email", "society"},
        "partner-conflicts": {"debater_1_email", "debater_2_email", "reason"},
        "judge-conflicts": {"judge_email", "debater_email", "reason"},
    }[kind]
    errors = []
    if not rows:
        return ["The CSV has no data rows."]
    if rows and not required <= set(rows[0]):
        errors.append(f"Required columns: {', '.join(sorted(required))}")
    for index, row in enumerate(rows, 2):
        try:
            if kind in {"debaters", "judges"} and not row.get("name", "").strip():
                raise ValueError("name is required")
            if kind == "debaters":
                _bool(row.get("is_novice"))
            if kind == "partner-conflicts":
                if not _find_person(Debater, row.get("debater_1_email"), row.get("debater_1_name", "")) or not _find_person(Debater, row.get("debater_2_email"), row.get("debater_2_name", "")):
                    raise ValueError("debater not found")
            if kind == "judge-conflicts":
                if not _find_person(Judge, row.get("judge_email"), row.get("judge_name", "")) or not _find_person(Debater, row.get("debater_email"), row.get("debater_name", "")):
                    raise ValueError("judge or debater not found")
        except (ValueError, TypeError) as exc:
            errors.append(f"Row {index}: {exc}")
    return errors


@transaction.atomic
def _commit_csv(kind, rows):
    for row in rows:
        if kind in {"debaters", "judges"}:
            society = Society.objects.get_or_create(name=row["society"].strip())[0] if row["society"].strip() else None
            model = Debater if kind == "debaters" else Judge
            instance = _find_person(model, row["email"], row["name"]) or model()
            instance.name, instance.email, instance.society = row["name"].strip(), row["email"].strip() or None, society
            if kind == "debaters":
                instance.is_novice = _bool(row["is_novice"])
            instance.save()
        elif kind == "partner-conflicts":
            first = _find_person(Debater, row["debater_1_email"], row.get("debater_1_name", ""))
            second = _find_person(Debater, row["debater_2_email"], row.get("debater_2_name", ""))
            a, b = sorted((first, second), key=lambda d: d.id)
            DebaterPartnerConflict.objects.update_or_create(debater_a=a, debater_b=b, defaults={"reason": row.get("reason", "")})
        else:
            judge = _find_person(Judge, row["judge_email"], row.get("judge_name", ""))
            debater = _find_person(Debater, row["debater_email"], row.get("debater_name", ""))
            JudgeDebaterConflict.objects.update_or_create(judge=judge, debater=debater, defaults={"reason": row.get("reason", "")})


@login_required
def csv_import(request, kind):
    if kind not in {"debaters", "judges", "partner-conflicts", "judge-conflicts"}:
        raise Http404
    preview = errors = None
    if request.method == "POST" and "commit" in request.POST:
        rows = request.session.get(f"csv_{kind}")
        if rows is None:
            messages.error(request, "Upload the CSV again; the preview expired.")
        else:
            errors = _validate_csv(kind, rows)
            if not errors:
                _commit_csv(kind, rows)
                request.session.pop(f"csv_{kind}", None)
                messages.success(request, f"Imported {len(rows)} rows.")
                return redirect("dashboard")
    elif request.method == "POST":
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                text = form.cleaned_data["file"].read().decode("utf-8-sig")
                preview = list(csv.DictReader(io.StringIO(text)))
                errors = _validate_csv(kind, preview)
                if not errors:
                    request.session[f"csv_{kind}"] = preview
            except (UnicodeDecodeError, csv.Error) as exc:
                errors = [str(exc)]
    else:
        form = CSVUploadForm()
    return render(request, "tournament/import.html", {"form": form, "kind": kind, "preview": preview, "errors": errors})


@login_required
def manage_round(request, round_id):
    round_obj = get_object_or_404(Round, pk=round_id)
    rooms = round_obj.rooms.prefetch_related("pairs__slots__debater__society", "pairs__slots__swing", "pairs__result", "ballot_tokens")
    hard, warnings = draw_warnings(round_obj) if round_obj.rooms.exists() else ([], [])
    all_slots = ParticipantSlot.objects.filter(pair__round=round_obj).select_related("debater", "swing")
    return render(request, "tournament/manage_round.html", {"round": round_obj, "rooms": rooms, "hard": hard, "warnings": warnings, "all_slots": all_slots})


@login_required
@require_POST
def generate_draw(request, round_id):
    round_obj = get_object_or_404(Round, pk=round_id)
    try:
        if PairResult.objects.filter(round=round_obj, submitted=True).exists():
            raise ValidationError("A ballot has already been submitted. The draw can no longer be regenerated.")
        if round_obj.kind == Round.PRELIM:
            generate_prelim_draw(round_obj)
        else:
            if Round.objects.filter(kind=Round.PRELIM, number__lte=5, results_confirmed=True).count() < 5:
                raise ValidationError("Confirm all five preliminary rounds before generating outrounds.")
            open_break, novice_break = break_lists()
            if round_obj.kind == Round.OPEN_SEMI:
                if len(open_break) < 16:
                    raise ValidationError("Fewer than 16 open semifinalists are available.")
                pattern = [0, 3, 4, 7, 8, 11, 12, 15, 1, 2, 5, 6, 9, 10, 13, 14]
                generate_outround(round_obj, [open_break[i] for i in pattern])
            elif round_obj.kind == Round.NOVICE_FINAL:
                generate_outround(round_obj, novice_break)
            else:
                advancing = list(Debater.objects.filter(participant_slots__pair__result__advances=True, participant_slots__pair__result__confirmed=True).distinct())
                generate_outround(round_obj, advancing)
        round_obj.draw_published = False
        round_obj.save(update_fields=["draw_published"])
        messages.success(request, "Draft draw generated.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("manage_round", round_id=round_id)


@login_required
@require_POST
def publish_draw(request, round_id):
    round_obj = get_object_or_404(Round, pk=round_id)
    hard, _ = draw_warnings(round_obj)
    if hard:
        messages.error(request, "Publishing blocked: " + " ".join(hard))
    else:
        round_obj.draw_published = True
        round_obj.save(update_fields=["draw_published"])
        setting = _setting()
        setting.current_round = round_obj
        setting.save()
        messages.success(request, "Draw published.")
    return redirect("manage_round", round_id=round_id)


@login_required
@require_POST
@transaction.atomic
def swap_slots(request, round_id):
    round_obj = get_object_or_404(Round, pk=round_id)
    first = get_object_or_404(ParticipantSlot, pk=request.POST.get("first"), pair__round=round_obj)
    second = get_object_or_404(ParticipantSlot, pk=request.POST.get("second"), pair__round=round_obj)
    first.debater_id, second.debater_id = second.debater_id, first.debater_id
    first.swing_id, second.swing_id = second.swing_id, first.swing_id
    first.save(update_fields=["debater", "swing"])
    second.save(update_fields=["debater", "swing"])
    hard, _ = draw_warnings(round_obj)
    if hard:
        transaction.set_rollback(True)
        messages.error(request, "Swap rejected: " + " ".join(hard))
    else:
        for slot in (first, second):
            if slot.swing_id:
                slot.swing.room = slot.pair.room
                slot.swing.position = slot.pair.position
                slot.swing.save(update_fields=["room", "position"])
        messages.success(request, "Participants swapped.")
    return redirect("manage_round", round_id=round_id)


@login_required
def allocate_judges(request, round_id):
    round_obj = get_object_or_404(Round, pk=round_id)
    rooms = list(round_obj.rooms.prefetch_related("pairs__slots__debater__society", "judge_allocations__judge"))
    if request.method == "POST":
        for room in rooms:
            form = JudgeAllocationForm(request.POST, prefix=f"room-{room.id}")
            if form.is_valid():
                room.judge_allocations.all().delete()
                chair = form.cleaned_data["chair"]
                if chair:
                    JudgeAllocation.objects.create(round=round_obj, room=room, judge=chair, role="chair")
                for judge in form.cleaned_data["panels"]:
                    if judge != chair:
                        JudgeAllocation.objects.create(round=round_obj, room=room, judge=judge, role="panel")
        messages.success(request, "Judge allocations saved.")
        return redirect("allocate_judges", round_id=round_id)
    forms = []
    for room in rooms:
        chair = room.judge_allocations.filter(role="chair").values_list("judge_id", flat=True).first()
        panels = list(room.judge_allocations.filter(role="panel").values_list("judge_id", flat=True))
        participant_ids = set(ParticipantSlot.objects.filter(pair__room=room, debater__isnull=False).values_list("debater_id", flat=True))
        society_ids = set(ParticipantSlot.objects.filter(pair__room=room, debater__society__isnull=False).values_list("debater__society_id", flat=True))
        room.allocation_warnings = []
        for allocation in room.judge_allocations.select_related("judge"):
            if JudgeDebaterConflict.objects.filter(judge=allocation.judge, debater_id__in=participant_ids).exists():
                room.allocation_warnings.append(f"{allocation.judge.name} has an imported conflict in this room.")
            if allocation.judge.society_id and allocation.judge.society_id in society_ids:
                room.allocation_warnings.append(f"{allocation.judge.name} shares a society with a debater in this room.")
        forms.append((room, JudgeAllocationForm(prefix=f"room-{room.id}", initial={"chair": chair, "panels": panels})))
    conflict_pairs = set(JudgeDebaterConflict.objects.values_list("judge_id", "debater_id"))
    return render(request, "tournament/judges.html", {"round": round_obj, "forms": forms, "conflict_pairs": conflict_pairs})


@login_required
@require_POST
def create_ballot_token(request, room_id):
    room = get_object_or_404(Room, pk=room_id)
    token = BallotToken.objects.create(round=room.round, room=room)
    messages.success(request, request.build_absolute_uri(reverse("ballot", args=[token.token])))
    return redirect("manage_round", round_id=room.round_id)


def ballot(request, token):
    ballot_token = get_object_or_404(BallotToken, token=token)
    room = ballot_token.room
    pairs = room.pairs.prefetch_related("slots__debater", "slots__swing")
    if request.method == "POST":
        try:
            if room.round.kind == Round.PRELIM:
                values = {}
                for pair in pairs:
                    for slot in pair.slots.all():
                        values[slot.id] = Decimal(request.POST[f"score_{slot.id}"])
                submit_prelim(room, values)
            else:
                submit_elimination(room, [int(value) for value in request.POST.getlist("selected")])
            ballot_token.used_at = timezone.now()
            ballot_token.submitted_by_name = request.POST.get("submitted_by_name", "")
            ballot_token.submitted_by_email = request.POST.get("submitted_by_email", "")
            ballot_token.save()
            return render(request, "tournament/ballot_thanks.html", {"room": room})
        except (ValidationError, InvalidOperation, KeyError, ValueError) as exc:
            messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else "Enter a valid score for every participant.")
    return render(request, "tournament/ballot.html", {"token": ballot_token, "room": room, "pairs": pairs})


@login_required
@require_POST
def confirm_result(request, room_id):
    room = get_object_or_404(Room, pk=room_id)
    try:
        confirm_room(room)
        messages.success(request, f"{room.name} result confirmed.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("manage_round", round_id=room.round_id)


@login_required
def admin_standings(request):
    return render(request, "tournament/standings.html", {"rows": standings(round_limit=5), "public": False})


@login_required
def manage_break(request):
    open_break, novice_break = break_lists()
    overlap = [row["debater"] for row in standings(round_limit=5)[:16] if row["debater"].is_novice]
    if request.method == "POST":
        for debater in overlap:
            value = request.POST.get(f"choice_{debater.id}")
            if value in {"open", "novice"}:
                BreakChoice.objects.update_or_create(debater=debater, defaults={"choice": value})
        messages.success(request, "Break choices saved.")
        return redirect("manage_break")
    return render(request, "tournament/break.html", {"open_break": open_break, "novice_break": novice_break, "overlap": overlap})


@login_required
@require_POST
def publish_final(request):
    champion_kinds = set(PairResult.objects.filter(confirmed=True, champion=True).values_list("round__kind", flat=True))
    if not {Round.OPEN_FINAL, Round.NOVICE_FINAL} <= champion_kinds:
        messages.error(request, "Confirm both final champion ballots before publishing final tabs.")
        return redirect("dashboard")
    setting = _setting()
    setting.final_tab_published = True
    setting.save()
    messages.success(request, "Final tabs and results published.")
    return redirect("dashboard")
