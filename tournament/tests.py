from decimal import Decimal

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import (
    BreakChoice, Debater, DebaterPartnerConflict, Judge, JudgeAllocation,
    JudgeDebaterConflict, PairResult, ParticipantSlot, Room, Round, Society,
    SpeakerScore, TemporaryPair,
)
from .services import (
    break_lists, confirm_room, draw_warnings, generate_prelim_draw, standings,
    submit_elimination, submit_prelim,
)


class TournamentTestCase(TestCase):
    def make_debaters(self, count, society=None):
        return [Debater.objects.create(name=f"Debater {i:02}", email=f"d{i}@example.com", society=society, is_novice=i < 8) for i in range(count)]

    def make_round(self, number=1, kind=Round.PRELIM, silent=False):
        return Round.objects.create(name=f"Round {number}", number=number, kind=kind, silent=silent)


class DrawTests(TournamentTestCase):
    def test_draw_has_each_debater_once_and_correct_swings(self):
        debaters = self.make_debaters(46)
        round_obj = self.make_round()
        generate_prelim_draw(round_obj)
        slots = ParticipantSlot.objects.filter(pair__round=round_obj)
        self.assertEqual(slots.count(), 48)
        self.assertEqual(slots.filter(debater__isnull=False).count(), 46)
        self.assertEqual(set(slots.values_list("debater_id", flat=True)) - {None}, {d.id for d in debaters})
        self.assertEqual(round_obj.swings.count(), 2)
        lowest = round_obj.rooms.order_by("-ordinal").first()
        self.assertFalse(slots.filter(swing__isnull=False).exclude(pair__room=lowest).exists())
        self.assertEqual(lowest.pairs.count(), 4)
        swing_pair = [p for p in lowest.pairs.prefetch_related("slots") if sum(s.is_swing for s in p.slots.all()) == 2]
        self.assertEqual(len(swing_pair), 1)
        self.assertTrue(all(room.pairs.count() == 4 for room in round_obj.rooms.all()))
        self.assertTrue(all(pair.slots.count() == 2 for pair in round_obj.pairs.all()))

    def test_odd_swings_create_exactly_one_real_swing_pair(self):
        self.make_debaters(9)
        round_obj = self.make_round()
        generate_prelim_draw(round_obj)
        lowest = round_obj.rooms.order_by("-ordinal").first()
        mixed = [pair for pair in lowest.pairs.prefetch_related("slots") if sum(slot.is_swing for slot in pair.slots.all()) == 1]
        self.assertEqual(round_obj.swings.count(), 7)
        self.assertEqual(len(mixed), 1)

    def test_partner_conflict_is_never_violated(self):
        debaters = self.make_debaters(8)
        DebaterPartnerConflict.objects.create(debater_a=debaters[0], debater_b=debaters[1])
        round_obj = self.make_round()
        generate_prelim_draw(round_obj)
        for pair in round_obj.pairs.prefetch_related("slots"):
            self.assertNotEqual({s.debater_id for s in pair.slots.all()}, {debaters[0].id, debaters[1].id})
        hard, _ = draw_warnings(round_obj)
        self.assertFalse(hard)

    def test_position_history_is_created_for_real_debaters(self):
        self.make_debaters(8)
        round_obj = self.make_round()
        generate_prelim_draw(round_obj)
        self.assertEqual(ParticipantSlot.objects.filter(pair__round=round_obj, debater__isnull=False).count(), 8)
        self.assertEqual(set(ParticipantSlot.objects.filter(pair__round=round_obj).values_list("pair__position", flat=True)), {"OG", "OO", "CG", "CO"})


class ResultTests(TournamentTestCase):
    def setUp(self):
        self.debaters = self.make_debaters(8)
        self.round = self.make_round()
        generate_prelim_draw(self.round)
        self.room = self.round.rooms.get()

    def values_with_totals(self):
        values = {}
        for index, pair in enumerate(self.room.pairs.prefetch_related("slots")):
            for slot in pair.slots.all():
                values[slot.id] = Decimal(70 + index)
        return values

    def test_prelim_ranks_are_inferred_and_map_to_points(self):
        submit_prelim(self.room, self.values_with_totals())
        results = PairResult.objects.filter(room=self.room).order_by("rank")
        self.assertEqual(list(results.values_list("rank", "team_points")), [(1, 3), (2, 2), (3, 1), (4, 0)])
        self.assertFalse(results.filter(confirmed=True).exists())
        confirm_room(self.room)
        self.assertEqual(sum(row["team_points"] for row in standings()), 12)

    def test_tied_pair_totals_are_rejected(self):
        values = {slot.id: Decimal("75") for slot in ParticipantSlot.objects.filter(pair__room=self.room)}
        with self.assertRaises(ValidationError):
            submit_prelim(self.room, values)

    def test_unconfirmed_results_do_not_affect_standings(self):
        submit_prelim(self.room, self.values_with_totals())
        self.assertEqual(sum(row["team_points"] for row in standings()), 0)
        confirm_room(self.room)
        self.assertGreater(sum(row["team_points"] for row in standings()), 0)

    def test_swings_help_pair_total_but_are_absent_from_tab(self):
        Debater.objects.all().delete()
        self.round.delete()
        self.make_debaters(7)
        round_obj = self.make_round()
        generate_prelim_draw(round_obj)
        room = round_obj.rooms.get()
        values = {}
        for index, pair in enumerate(room.pairs.prefetch_related("slots")):
            for slot in pair.slots.all():
                values[slot.id] = Decimal(70 + index)
        submit_prelim(room, values)
        confirm_room(room)
        self.assertEqual(len(standings()), 7)
        self.assertEqual(SpeakerScore.objects.filter(participant_slot__swing__isnull=False, confirmed=True).count(), 1)

    def test_elimination_selection_counts(self):
        semi = self.make_round(number=6, kind=Round.OPEN_SEMI)
        room = Room.objects.create(round=semi, name="Semi 1", ordinal=1)
        for position in ("OG", "OO", "CG", "CO"):
            pair = TemporaryPair.objects.create(round=semi, room=room, position=position)
            PairResult.objects.create(round=semi, room=room, temporary_pair=pair)
        with self.assertRaises(ValidationError):
            submit_elimination(room, [room.pairs.first().id])
        submit_elimination(room, list(room.pairs.values_list("id", flat=True)[:2]))
        self.assertEqual(PairResult.objects.filter(room=room, advances=True).count(), 2)

    def test_final_selects_exactly_one_champion(self):
        final = self.make_round(number=7, kind=Round.OPEN_FINAL)
        room = Room.objects.create(round=final, name="Final", ordinal=1)
        for position in ("OG", "OO", "CG", "CO"):
            pair = TemporaryPair.objects.create(round=final, room=room, position=position)
            PairResult.objects.create(round=final, room=room, temporary_pair=pair)
        with self.assertRaises(ValidationError):
            submit_elimination(room, list(room.pairs.values_list("id", flat=True)[:2]))
        submit_elimination(room, [room.pairs.first().id])
        confirm_room(room)
        self.assertEqual(PairResult.objects.filter(room=room, champion=True, confirmed=True).count(), 1)


class StandingsTests(TournamentTestCase):
    def test_silent_round_is_hidden_from_public_standings(self):
        self.make_debaters(8)
        public_round = self.make_round(1)
        generate_prelim_draw(public_round)
        public_values = {slot.id: Decimal(70 + i // 2) for i, slot in enumerate(ParticipantSlot.objects.filter(pair__room=public_round.rooms.get()).order_by("pair_id"))}
        submit_prelim(public_round.rooms.get(), public_values)
        confirm_room(public_round.rooms.get())
        silent = self.make_round(4, silent=True)
        generate_prelim_draw(silent)
        silent_values = {slot.id: Decimal(75 + i // 2) for i, slot in enumerate(ParticipantSlot.objects.filter(pair__room=silent.rooms.get()).order_by("pair_id"))}
        submit_prelim(silent.rooms.get(), silent_values)
        confirm_room(silent.rooms.get())
        public_total = sum(row["team_points"] for row in standings(public=True))
        admin_total = sum(row["team_points"] for row in standings())
        self.assertEqual(public_total, 12)
        self.assertEqual(admin_total, 24)

    def test_draw_strength_counts_repeated_opponents(self):
        self.make_debaters(8)
        for number in (1, 2):
            round_obj = self.make_round(number)
            generate_prelim_draw(round_obj)
            values = {slot.id: Decimal(70 + i // 2) for i, slot in enumerate(ParticipantSlot.objects.filter(pair__room=round_obj.rooms.get()).order_by("pair_id"))}
            submit_prelim(round_obj.rooms.get(), values)
            confirm_room(round_obj.rooms.get())
        self.assertTrue(any(row["draw_strength"] > row["team_points"] for row in standings()))


class BreakTests(TournamentTestCase):
    def setUp(self):
        debaters = self.make_debaters(24)
        Debater.objects.update(is_novice=True)
        for number in range(1, 6):
            round_obj = self.make_round(number, silent=number > 3)
            generate_prelim_draw(round_obj)
            for room in round_obj.rooms.all():
                values = {}
                for index, pair in enumerate(room.pairs.prefetch_related("slots")):
                    for slot in pair.slots.all():
                        values[slot.id] = Decimal(70 + index)
                submit_prelim(room, values)
                confirm_room(room)

    def test_open_top_16_and_novice_top_8(self):
        open_break, novice_break = break_lists()
        self.assertEqual(len(open_break), 16)
        self.assertEqual(len(novice_break), 8)
        self.assertFalse(set(open_break) & set(novice_break))

    def test_dual_eligible_novice_choice_moves_them_to_novice(self):
        selected = standings(round_limit=5)[0]["debater"]
        BreakChoice.objects.create(debater=selected, choice="novice")
        open_break, novice_break = break_lists()
        self.assertNotIn(selected, open_break)
        self.assertIn(selected, novice_break)
        self.assertEqual(len(open_break), 16)
        self.assertEqual(len(novice_break), 8)


class JudgeAllocationTests(TournamentTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="tab", password="secret")
        self.client.force_login(self.user)
        self.society = Society.objects.create(name="Sociedade A")
        self.debaters = self.make_debaters(16)
        self.debaters[0].society = self.society
        self.debaters[0].save(update_fields=["society"])
        self.round = self.make_round()
        generate_prelim_draw(self.round)
        self.judge = Judge.objects.create(name="Juíza Teste", society=self.society)
        JudgeDebaterConflict.objects.create(judge=self.judge, debater=self.debaters[0])

    def test_page_exposes_societies_and_conflicts_for_live_warnings(self):
        response = self.client.get(reverse("allocate_judges", args=[self.round.id]), secure=True)
        self.assertContains(response, "Sociedade A")
        self.assertContains(response, "Juíza Teste")
        self.assertContains(response, str(self.debaters[0].id))
        self.assertContains(response, "Os avisos não impedem a alocação")

    def test_duplicate_and_conflicted_allocation_is_allowed(self):
        rooms = list(self.round.rooms.all())
        response = self.client.post(reverse("allocate_judges", args=[self.round.id]), {
            f"room-{rooms[0].id}-chair": self.judge.id,
            f"room-{rooms[1].id}-chair": self.judge.id,
        }, secure=True)
        self.assertRedirects(response, reverse("allocate_judges", args=[self.round.id]), fetch_redirect_response=False)
        self.assertEqual(JudgeAllocation.objects.filter(judge=self.judge).count(), 2)


class AuthenticationTests(TestCase):
    def test_home_has_login_and_login_redirects_to_manage(self):
        self.assertContains(self.client.get(reverse("home"), secure=True), reverse("login"))
        get_user_model().objects.create_user(username="tab", password="secret")
        response = self.client.post(reverse("login"), {"username": "tab", "password": "secret"}, secure=True)
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
