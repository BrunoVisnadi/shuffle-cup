import csv
import io
import secrets
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import CSVUploadForm, JudgeAllocationForm
from .models import (
    BreakChoice, Debater, DebaterPartnerConflict, Judge,
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
    rooms = round_obj.rooms.prefetch_related("pairs__slots__debater__society", "pairs__slots__swing", "judge_allocations__judge__society")
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
        ("Rodada 1", 1, Round.PRELIM, False), ("Rodada 2", 2, Round.PRELIM, False),
        ("Rodada 3", 3, Round.PRELIM, False), ("Rodada 4", 4, Round.PRELIM, True),
        ("Rodada 5", 5, Round.PRELIM, True), ("Semifinais Open", 6, Round.OPEN_SEMI, False),
        ("Final novice", 6, Round.NOVICE_FINAL, False), ("Final Open", 7, Round.OPEN_FINAL, False),
    ]
    for name, number, kind, silent in specifications:
        Round.objects.update_or_create(number=number, kind=kind, defaults={"name": name, "silent": silent})
    messages.success(request, "As rodadas padrão estão prontas.")
    return redirect("dashboard")


def _bool(value):
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "1", "sim"}:
        return True
    if normalized in {"false", "no", "0", "nao", "não"}:
        return False
    raise ValueError(f"Valor booleano inválido: {value}")


def _find_person(model, email, name):
    if email:
        return model.objects.filter(email__iexact=email.strip()).first()
    return model.objects.filter(name__exact=name.strip()).first()


def _validate_csv(kind, rows):
    required = {
        "debaters": {"name", "email", "society", "is_novice"},
        "judges": {"name", "email", "society"},
        "partner-conflicts": {"debater_1_email", "debater_2_email"},
        "judge-conflicts": {"judge_email", "debater_email"},
    }[kind]
    errors = []
    if not rows:
        return ["O CSV não contém linhas de dados."]
    if rows and not required <= set(rows[0]):
        errors.append(f"Colunas obrigatórias: {', '.join(sorted(required))}")
    for index, row in enumerate(rows, 2):
        try:
            if kind in {"debaters", "judges"} and not row.get("name", "").strip():
                raise ValueError("name é obrigatório")
            if kind == "debaters":
                _bool(row.get("is_novice"))
            if kind == "partner-conflicts":
                if not _find_person(Debater, row.get("debater_1_email"), row.get("debater_1_name", "")) or not _find_person(Debater, row.get("debater_2_email"), row.get("debater_2_name", "")):
                    raise ValueError("debatedor não encontrado")
            if kind == "judge-conflicts":
                if not _find_person(Judge, row.get("judge_email"), row.get("judge_name", "")) or not _find_person(Debater, row.get("debater_email"), row.get("debater_name", "")):
                    raise ValueError("juiz ou debatedor não encontrado")
        except (ValueError, TypeError) as exc:
            errors.append(f"Linha {index}: {exc}")
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
            DebaterPartnerConflict.objects.get_or_create(debater_a=a, debater_b=b)
        else:
            judge = _find_person(Judge, row["judge_email"], row.get("judge_name", ""))
            debater = _find_person(Debater, row["debater_email"], row.get("debater_name", ""))
            JudgeDebaterConflict.objects.get_or_create(judge=judge, debater=debater)


@login_required
def csv_import(request, kind):
    if kind not in {"debaters", "judges", "partner-conflicts", "judge-conflicts"}:
        raise Http404
    preview = errors = None
    if request.method == "POST" and "commit" in request.POST:
        rows = request.session.get(f"csv_{kind}")
        if rows is None:
            messages.error(request, "Envie o CSV novamente; a prévia expirou.")
        else:
            errors = _validate_csv(kind, rows)
            if not errors:
                _commit_csv(kind, rows)
                request.session.pop(f"csv_{kind}", None)
                messages.success(request, f"{len(rows)} linhas importadas.")
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
    rooms = round_obj.rooms.prefetch_related("pairs__slots__debater__society", "pairs__slots__swing", "pairs__result")
    hard, warnings = draw_warnings(round_obj) if round_obj.rooms.exists() else ([], [])
    all_slots = ParticipantSlot.objects.filter(pair__round=round_obj).select_related("debater", "swing")
    return render(request, "tournament/manage_round.html", {"round": round_obj, "rooms": rooms, "hard": hard, "warnings": warnings, "all_slots": all_slots})


@login_required
@require_POST
def generate_draw(request, round_id):
    round_obj = get_object_or_404(Round, pk=round_id)
    try:
        if PairResult.objects.filter(round=round_obj, submitted=True).exists():
            raise ValidationError("Um ballot já foi enviado. O draw não pode mais ser gerado novamente.")
        if round_obj.kind == Round.PRELIM:
            generate_prelim_draw(round_obj)
        else:
            if Round.objects.filter(kind=Round.PRELIM, number__lte=5, results_confirmed=True).count() < 5:
                raise ValidationError("Confirme as cinco rodadas preliminares antes de gerar as eliminatórias.")
            open_break, novice_break = break_lists()
            if round_obj.kind == Round.OPEN_SEMI:
                if len(open_break) < 16:
                    raise ValidationError("Há menos de 16 semifinalistas Open disponíveis.")
                pattern = [0, 3, 4, 7, 8, 11, 12, 15, 1, 2, 5, 6, 9, 10, 13, 14]
                generate_outround(round_obj, [open_break[i] for i in pattern])
            elif round_obj.kind == Round.NOVICE_FINAL:
                generate_outround(round_obj, novice_break)
            else:
                advancing = list(Debater.objects.filter(participant_slots__pair__result__advances=True, participant_slots__pair__result__confirmed=True).distinct())
                generate_outround(round_obj, advancing)
        round_obj.draw_published = False
        round_obj.save(update_fields=["draw_published"])
        messages.success(request, "Rascunho do draw gerado.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("manage_round", round_id=round_id)


@login_required
@require_POST
def publish_draw(request, round_id):
    round_obj = get_object_or_404(Round, pk=round_id)
    hard, _ = draw_warnings(round_obj)
    if hard:
        messages.error(request, "Publicação bloqueada: " + " ".join(hard))
    else:
        round_obj.draw_published = True
        round_obj.save(update_fields=["draw_published"])
        setting = _setting()
        setting.current_round = round_obj
        setting.save()
        messages.success(request, "Draw publicado.")
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
        messages.error(request, "Troca rejeitada: " + " ".join(hard))
    else:
        for slot in (first, second):
            if slot.swing_id:
                slot.swing.room = slot.pair.room
                slot.swing.position = slot.pair.position
                slot.swing.save(update_fields=["room", "position"])
        messages.success(request, "Participantes trocados.")
    return redirect("manage_round", round_id=round_id)


@login_required
def allocate_judges(request, round_id):
    round_obj = get_object_or_404(Round, pk=round_id)
    rooms = list(round_obj.rooms.prefetch_related("pairs__slots__debater__society", "judge_allocations__judge"))
    if request.method == "POST":
        submitted_forms = [(room, JudgeAllocationForm(request.POST, prefix=f"room-{room.id}")) for room in rooms]
        if all(form.is_valid() for room, form in submitted_forms):
            seen = set()
            duplicate = False
            for room, form in submitted_forms:
                selected = ([form.cleaned_data["chair"]] if form.cleaned_data["chair"] else []) + list(form.cleaned_data["panels"])
                ids = [judge.id for judge in selected]
                if len(ids) != len(set(ids)) or seen.intersection(ids):
                    duplicate = True
                    break
                seen.update(ids)
            if duplicate:
                messages.error(request, "Um juiz não pode ser alocado mais de uma vez na mesma rodada.")
            else:
                with transaction.atomic():
                    for room, form in submitted_forms:
                        room.judge_allocations.all().delete()
                        chair = form.cleaned_data["chair"]
                        if chair:
                            JudgeAllocation.objects.create(round=round_obj, room=room, judge=chair, role="chair")
                        for judge in form.cleaned_data["panels"]:
                            JudgeAllocation.objects.create(round=round_obj, room=room, judge=judge, role="panel")
                messages.success(request, "Alocações de juízes salvas.")
                return redirect("allocate_judges", round_id=round_id)
        messages.error(request, "Revise os campos de alocação.")
    forms = []
    room_data = {}
    for room in rooms:
        chair = room.judge_allocations.filter(role="chair").values_list("judge_id", flat=True).first()
        panels = list(room.judge_allocations.filter(role="panel").values_list("judge_id", flat=True))
        debaters = []
        for pair in room.pairs.all():
            for slot in pair.slots.all():
                if slot.debater_id:
                    debaters.append({
                        "id": slot.debater_id,
                        "name": slot.debater.name,
                        "society_id": slot.debater.society_id,
                        "society": slot.debater.society.name if slot.debater.society_id else "",
                    })
        room_data[str(room.id)] = {"name": room.name, "debaters": debaters}
        forms.append((room, JudgeAllocationForm(prefix=f"room-{room.id}", initial={"chair": chair, "panels": panels})))

    judges = Judge.objects.filter(active=True).select_related("society").prefetch_related("debater_conflicts")
    judge_data = {
        str(judge.id): {
            "name": judge.name,
            "society_id": judge.society_id,
            "society": judge.society.name if judge.society_id else "",
            "conflicts": [conflict.debater_id for conflict in judge.debater_conflicts.all()],
        }
        for judge in judges
    }
    return render(request, "tournament/judges.html", {
        "round": round_obj, "forms": forms, "judge_data": judge_data, "room_data": room_data,
    })


@login_required
def judge_links(request):
    judges = list(Judge.objects.filter(active=True).select_related("society"))
    for judge in judges:
        judge.private_url = request.build_absolute_uri(reverse("judge_portal", args=[judge.private_token]))
    return render(request, "tournament/judge_links.html", {"judges": judges})


@login_required
@require_POST
def regenerate_judge_link(request, judge_id):
    judge = get_object_or_404(Judge, pk=judge_id)
    judge.private_token = secrets.token_urlsafe()
    judge.save(update_fields=["private_token"])
    messages.success(request, f"Nova URL privada gerada para {judge.name}.")
    return redirect("judge_links")


def judge_portal(request, token):
    judge = get_object_or_404(Judge, private_token=token, active=True)
    current_round = _setting().current_round
    allocation = None
    if current_round:
        allocation = JudgeAllocation.objects.filter(
            round=current_round, judge=judge, role="chair",
        ).select_related("room", "round").first()
    room = allocation.room if allocation else None
    if not room:
        return render(request, "tournament/judge_portal.html", {"judge": judge, "room": None})
    pairs = room.pairs.prefetch_related("slots__debater", "slots__swing")
    if request.method == "POST":
        try:
            if PairResult.objects.filter(room=room, confirmed=True).exists():
                raise ValidationError("O resultado desta sala já foi confirmado.")
            if room.round.kind == Round.PRELIM:
                values = {}
                for pair in pairs:
                    for slot in pair.slots.all():
                        values[slot.id] = Decimal(request.POST[f"score_{slot.id}"])
                submit_prelim(room, values)
            else:
                submit_elimination(room, [int(value) for value in request.POST.getlist("selected")])
            return render(request, "tournament/ballot_thanks.html", {"room": room})
        except (ValidationError, InvalidOperation, KeyError, ValueError) as exc:
            messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else "Informe uma nota válida para cada participante.")
    return render(request, "tournament/judge_portal.html", {"judge": judge, "room": room, "pairs": pairs})


@login_required
@require_POST
def confirm_result(request, room_id):
    room = get_object_or_404(Room, pk=room_id)
    try:
        confirm_room(room)
        messages.success(request, f"Resultado de {room.name} confirmado.")
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
        messages.success(request, "Escolhas de break salvas.")
        return redirect("manage_break")
    return render(request, "tournament/break.html", {"open_break": open_break, "novice_break": novice_break, "overlap": overlap})


@login_required
@require_POST
def publish_final(request):
    champion_kinds = set(PairResult.objects.filter(confirmed=True, champion=True).values_list("round__kind", flat=True))
    if not {Round.OPEN_FINAL, Round.NOVICE_FINAL} <= champion_kinds:
        messages.error(request, "Confirme os ballots dos campeões das duas finais antes de publicar os tabs finais.")
        return redirect("dashboard")
    setting = _setting()
    setting.final_tab_published = True
    setting.save()
    messages.success(request, "Tabs finais e resultados publicados.")
    return redirect("dashboard")
