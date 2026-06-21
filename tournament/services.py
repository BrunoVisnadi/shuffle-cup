import math
import random
from collections import Counter, defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    BreakChoice, Debater, DebaterPartnerConflict, PairResult, ParticipantSlot,
    POSITIONS, Room, Round, SpeakerScore, SwingSlot, TemporaryPair,
)


POSITION_NAMES = [p[0] for p in POSITIONS]


def _conflict_keys():
    return {frozenset((a, b)) for a, b in DebaterPartnerConflict.objects.values_list("debater_a_id", "debater_b_id")}


def _partner_history(exclude_round=None):
    history = Counter()
    pairs = TemporaryPair.objects.prefetch_related("slots").filter(slots__debater__isnull=False).distinct()
    if exclude_round:
        pairs = pairs.exclude(round=exclude_round)
    for pair in pairs:
        ids = [s.debater_id for s in pair.slots.all() if s.debater_id]
        if len(ids) == 2:
            history[frozenset(ids)] += 1
    return history


def _position_history(exclude_round=None):
    history = defaultdict(Counter)
    slots = ParticipantSlot.objects.filter(debater__isnull=False).select_related("pair")
    if exclude_round:
        slots = slots.exclude(pair__round=exclude_round)
    for slot in slots:
        history[slot.debater_id][slot.pair.position] += 1
    return history


def _candidate_pairs(real_ids, swing_ids, conflicts, history, societies):
    participants = [("d", value) for value in real_ids] + [("s", value) for value in swing_ids]
    best = None
    for _ in range(1200):
        reals = [("d", value) for value in real_ids]
        swings = [("s", value) for value in swing_ids]
        random.shuffle(reals)
        random.shuffle(swings)
        pairs = []
        while len(swings) >= 2:
            pairs.append((swings.pop(), swings.pop()))
        if swings:
            pairs.append((swings.pop(), reals.pop()))
        random.shuffle(reals)
        valid = True
        while reals:
            first = reals.pop()
            choices = [i for i, second in enumerate(reals) if frozenset((first[1], second[1])) not in conflicts]
            if not choices:
                valid = False
                break
            second = reals.pop(random.choice(choices))
            pairs.append((first, second))
        if not valid or len(pairs) != 4 or sum(map(len, pairs)) != len(participants):
            continue
        repeats = same_society = 0
        for first, second in pairs:
            if first[0] == second[0] == "d":
                key = frozenset((first[1], second[1]))
                repeats += history[key]
                if societies.get(first[1]) and societies.get(first[1]) == societies.get(second[1]):
                    same_society += 1
        score = (repeats, same_society, random.random())
        if best is None or score < best[0]:
            best = (score, pairs)
    if best is None:
        raise ValidationError("No valid pairing exists. Check imported partnership conflicts.")
    return best[1]


def _assign_positions(pairs, position_history):
    best = None
    for _ in range(200):
        positions = random.sample(POSITION_NAMES, 4)
        score = 0
        for pair, position in zip(pairs, positions):
            for kind, identifier in pair:
                if kind == "d":
                    score += position_history[identifier][position] ** 2
        candidate = (score, random.random(), list(zip(pairs, positions)))
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best[2]


def standings(round_limit=None, public=False):
    rounds = Round.objects.filter(kind=Round.PRELIM)
    if round_limit is not None:
        rounds = rounds.filter(number__lte=round_limit)
    if public:
        rounds = rounds.filter(silent=False)
    confirmed_results = PairResult.objects.filter(round__in=rounds, confirmed=True).select_related("temporary_pair", "room")
    rows = {d.id: {"debater": d, "team_points": 0, "speaker_points": Decimal("0"), "firsts": 0, "seconds": 0, "draw_strength": 0} for d in Debater.objects.filter(active=True)}
    result_by_pair = {r.temporary_pair_id: r for r in confirmed_results}
    confirmed_pair_ids = set(result_by_pair)
    slots = ParticipantSlot.objects.filter(pair_id__in=confirmed_pair_ids).select_related("pair", "pair__room")
    slot_list = list(slots)
    for slot in slot_list:
        if not slot.debater_id:
            continue
        result = result_by_pair[slot.pair_id]
        row = rows[slot.debater_id]
        row["team_points"] += result.team_points or 0
        row["firsts"] += result.rank == 1
        row["seconds"] += result.rank == 2
        score = SpeakerScore.objects.filter(participant_slot=slot, confirmed=True).values_list("speaker_points", flat=True).first()
        row["speaker_points"] += score or 0
    total_points = {identifier: row["team_points"] for identifier, row in rows.items()}
    by_room = defaultdict(list)
    for slot in slot_list:
        by_room[slot.pair.room_id].append(slot)
    for room_slots in by_room.values():
        for slot in room_slots:
            if not slot.debater_id:
                continue
            opponents = [other for other in room_slots if other.pair_id != slot.pair_id]
            rows[slot.debater_id]["draw_strength"] += sum(total_points.get(other.debater_id, 0) for other in opponents if other.debater_id)
    ranked = list(rows.values())
    ranked.sort(key=lambda r: (-r["team_points"], -r["draw_strength"], -r["speaker_points"], -r["firsts"], -r["seconds"], r["debater"].name.lower()))
    for index, row in enumerate(ranked, 1):
        row["rank"] = index
    return ranked


@transaction.atomic
def generate_prelim_draw(round_obj):
    if round_obj.kind != Round.PRELIM:
        raise ValidationError("This generator is for preliminary rounds only.")
    active = list(Debater.objects.filter(active=True).select_related("society"))
    if not active:
        raise ValidationError("There are no active debaters.")
    room_count = math.ceil(len(active) / 8)
    swing_count = room_count * 8 - len(active)
    prior = {row["debater"].id: row["team_points"] for row in standings(round_limit=round_obj.number - 1)}
    random.shuffle(active)
    if round_obj.number > 1:
        active.sort(key=lambda d: -prior.get(d.id, 0))

    round_obj.rooms.all().delete()
    rooms = [Room.objects.create(round=round_obj, name=f"Room {i}", ordinal=i) for i in range(1, room_count + 1)]
    swings = [SwingSlot.objects.create(round=round_obj, display_name=f"Swing {i + 1}", room=rooms[-1]) for i in range(swing_count)]
    room_reals = []
    cursor = 0
    for index in range(room_count):
        real_count = 8 - swing_count if index == room_count - 1 else 8
        room_reals.append(active[cursor:cursor + real_count])
        cursor += real_count

    conflicts = _conflict_keys()
    history = _partner_history(exclude_round=round_obj)
    position_history = _position_history(exclude_round=round_obj)
    societies = {d.id: d.society_id for d in active}
    for index, room in enumerate(rooms):
        real_ids = [d.id for d in room_reals[index]]
        swing_ids = [s.id for s in swings] if room == rooms[-1] else []
        pairs = _candidate_pairs(real_ids, swing_ids, conflicts, history, societies)
        for participants, position in _assign_positions(pairs, position_history):
            pair = TemporaryPair.objects.create(round=round_obj, room=room, position=position)
            for order, (kind, identifier) in enumerate(participants, 1):
                ParticipantSlot.objects.create(pair=pair, order=order, debater_id=identifier if kind == "d" else None, swing_id=identifier if kind == "s" else None)
                if kind == "s":
                    SwingSlot.objects.filter(pk=identifier).update(position=position)
            PairResult.objects.create(round=round_obj, room=room, temporary_pair=pair)
    return rooms


def draw_warnings(round_obj):
    warnings, hard = [], []
    slots = list(ParticipantSlot.objects.filter(pair__round=round_obj).select_related("pair", "pair__room", "debater__society"))
    counts = Counter(slot.debater_id for slot in slots if slot.debater_id)
    expected = set(Debater.objects.filter(active=True).values_list("id", flat=True)) if round_obj.kind == Round.PRELIM else set(counts)
    if set(counts) != expected or any(count != 1 for count in counts.values()):
        hard.append("A real debater is missing or appears more than once.")
    conflicts = _conflict_keys()
    history = _partner_history(exclude_round=round_obj)
    for pair in round_obj.pairs.prefetch_related("slots__debater__society"):
        pair_slots = list(pair.slots.all())
        if len(pair_slots) != 2:
            hard.append(f"{pair} does not have exactly two participants.")
            continue
        real_ids = [s.debater_id for s in pair_slots if s.debater_id]
        if len(real_ids) == 2:
            key = frozenset(real_ids)
            if key in conflicts:
                hard.append(f"{pair.names} have an imported partner conflict.")
            if history[key]:
                warnings.append(f"Repeated partnership: {pair.names}.")
            if pair_slots[0].debater.society_id and pair_slots[0].debater.society_id == pair_slots[1].debater.society_id:
                warnings.append(f"Same-society partnership: {pair.names}.")
    lowest = round_obj.rooms.order_by("-ordinal").first()
    if ParticipantSlot.objects.filter(pair__round=round_obj, swing__isnull=False).exclude(pair__room=lowest).exists():
        hard.append("A swing is outside the lowest room.")
    return hard, warnings


@transaction.atomic
def submit_prelim(room, values):
    pairs = list(room.pairs.prefetch_related("slots"))
    totals = {}
    for pair in pairs:
        totals[pair.id] = sum(Decimal(str(values[slot.id])) for slot in pair.slots.all())
    if len(set(totals.values())) != 4:
        raise ValidationError("Pair totals must be distinct; tied pair totals are invalid.")
    ordered = sorted(totals, key=totals.get, reverse=True)
    for rank, pair_id in enumerate(ordered, 1):
        pair = next(p for p in pairs if p.id == pair_id)
        PairResult.objects.update_or_create(temporary_pair=pair, defaults={"round": room.round, "room": room, "rank": rank, "team_points": 4 - rank, "submitted": True, "confirmed": False})
        for slot in pair.slots.all():
            SpeakerScore.objects.update_or_create(participant_slot=slot, defaults={"round": room.round, "room": room, "speaker_points": values[slot.id], "confirmed": False})


@transaction.atomic
def submit_elimination(room, selected_ids):
    expected = 2 if room.round.kind == Round.OPEN_SEMI else 1
    if len(selected_ids) != expected:
        raise ValidationError(f"Select exactly {expected} pair(s).")
    valid = set(room.pairs.values_list("id", flat=True))
    if not set(selected_ids) <= valid:
        raise ValidationError("Invalid pair selection.")
    for pair in room.pairs.all():
        PairResult.objects.update_or_create(temporary_pair=pair, defaults={"round": room.round, "room": room, "advances": room.round.kind == Round.OPEN_SEMI and pair.id in selected_ids, "champion": room.round.kind != Round.OPEN_SEMI and pair.id in selected_ids, "submitted": True, "confirmed": False})


@transaction.atomic
def confirm_room(room):
    results = room.pairs.select_related("result")
    if any(not hasattr(pair, "result") or not pair.result.submitted for pair in results):
        raise ValidationError("This room does not have a complete submitted ballot.")
    if room.round.kind == Round.PRELIM:
        totals = [sum(s.speaker_score.speaker_points for s in pair.slots.all()) for pair in results]
        if len(set(totals)) != 4:
            raise ValidationError("Tied pair totals cannot be confirmed.")
        SpeakerScore.objects.filter(room=room).update(confirmed=True)
    PairResult.objects.filter(room=room).update(confirmed=True)
    if not PairResult.objects.filter(round=room.round, confirmed=False).exists():
        room.round.results_confirmed = True
        room.round.save(update_fields=["results_confirmed"])


def break_lists():
    ranked = standings(round_limit=5)
    open_candidates = [row["debater"] for row in ranked[:16]]
    open_ids = set()
    for debater in open_candidates:
        choice = getattr(debater, "break_choice", None)
        if not debater.is_novice or not choice or choice.choice == "open":
            open_ids.add(debater.id)
    for row in ranked:
        if len(open_ids) >= 16:
            break
        if row["debater"].id not in open_ids and (not row["debater"].is_novice or getattr(row["debater"], "break_choice", None) is None or row["debater"].break_choice.choice == "open"):
            open_ids.add(row["debater"].id)
    open_break = [row["debater"] for row in ranked if row["debater"].id in open_ids][:16]
    novice_break = [row["debater"] for row in ranked if row["debater"].is_novice and row["debater"].id not in open_ids][:8]
    return open_break, novice_break


@transaction.atomic
def generate_outround(round_obj, debaters):
    if len(debaters) % 8:
        raise ValidationError("Outround participant count must be divisible by eight.")
    round_obj.rooms.all().delete()
    conflicts, history = _conflict_keys(), _partner_history(exclude_round=round_obj)
    societies = {d.id: d.society_id for d in debaters}
    position_history = _position_history(exclude_round=round_obj)
    chunks = [debaters[i:i + 8] for i in range(0, len(debaters), 8)]
    for index, chunk in enumerate(chunks, 1):
        room = Room.objects.create(round=round_obj, name=f"Room {index}", ordinal=index)
        pairs = _candidate_pairs([d.id for d in chunk], [], conflicts, history, societies)
        for participants, position in _assign_positions(pairs, position_history):
            pair = TemporaryPair.objects.create(round=round_obj, room=room, position=position)
            for order, (_, identifier) in enumerate(participants, 1):
                ParticipantSlot.objects.create(pair=pair, order=order, debater_id=identifier)
            PairResult.objects.create(round=round_obj, room=room, temporary_pair=pair)
    return list(round_obj.rooms.all())
