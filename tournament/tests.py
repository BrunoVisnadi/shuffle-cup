from decimal import Decimal

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import (
    Debater, DebaterPartnerConflict, Judge, JudgeAllocation,
    JudgeDebaterConflict, PairResult, ParticipantSlot, Room, Round, SiteSettings, Society,
    RoundUnavailableDebater, SpeakerScore, TemporaryPair,
)
from .services import (
    break_lists, confirm_room, draw_warnings, generate_prelim_draw, standings,
    submit_elimination, submit_prelim,
)


class TournamentTestCase(TestCase):
    def make_debaters(self, count, society=None):
        return [Debater.objects.create(name=f"Debater {i:02}", email=f"d{i}@example.com", society=society) for i in range(count)]

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

    def test_round_unavailable_debater_is_skipped_only_for_that_round(self):
        debaters = self.make_debaters(9)
        first_round = self.make_round(1)
        second_round = self.make_round(2)
        RoundUnavailableDebater.objects.create(round=first_round, debater=debaters[0])

        generate_prelim_draw(first_round)
        first_slots = ParticipantSlot.objects.filter(pair__round=first_round)
        self.assertNotIn(debaters[0].id, set(first_slots.values_list("debater_id", flat=True)))
        self.assertEqual(first_slots.filter(debater__isnull=False).count(), 8)
        hard, _ = draw_warnings(first_round)
        self.assertFalse(hard)

        generate_prelim_draw(second_round)
        second_ids = set(ParticipantSlot.objects.filter(pair__round=second_round).values_list("debater_id", flat=True))
        self.assertIn(debaters[0].id, second_ids)


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

    def test_speaker_scores_must_be_between_50_and_100(self):
        values = self.values_with_totals()
        values[next(iter(values))] = Decimal("49.99")
        with self.assertRaises(ValidationError):
            submit_prelim(self.room, values)
        values[next(iter(values))] = Decimal("100.01")
        with self.assertRaises(ValidationError):
            submit_prelim(self.room, values)

    def test_boundary_speaker_scores_are_accepted(self):
        values = self.values_with_totals()
        slot_ids = list(values)
        values[slot_ids[0]] = Decimal("50")
        values[slot_ids[-1]] = Decimal("100")
        submit_prelim(self.room, values)
        self.assertTrue(PairResult.objects.filter(room=self.room, submitted=True).exists())

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
        semi = self.make_round(number=5, kind=Round.OPEN_SEMI)
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

        response = self.client.get(reverse("public_standings"), secure=True)
        self.assertContains(response, "Total de pontos")
        self.assertNotContains(response, "Força do draw")
        self.assertNotContains(response, "Speaker points")
        setting = SiteSettings.load()
        setting.final_tab_published = True
        setting.save()
        response = self.client.get(reverse("public_standings"), secure=True)
        self.assertContains(response, "Força do draw")
        self.assertContains(response, "Speaker points")

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
        self.make_debaters(24)
        for number in range(1, 5):
            round_obj = self.make_round(number, silent=number > 2)
            generate_prelim_draw(round_obj)
            for room in round_obj.rooms.all():
                values = {}
                for index, pair in enumerate(room.pairs.prefetch_related("slots")):
                    for slot in pair.slots.all():
                        values[slot.id] = Decimal(70 + index)
                submit_prelim(room, values)
                confirm_room(room)

    def test_top_16_break_to_open_semifinals(self):
        semifinalists = break_lists()
        self.assertEqual(len(semifinalists), 16)


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
        self.assertNotContains(response, "Impedimentos e sociedades geram avisos, mas não bloqueiam")

    def test_conflicted_allocation_is_allowed_but_duplicate_is_rejected(self):
        rooms = list(self.round.rooms.all())
        response = self.client.post(reverse("allocate_judges", args=[self.round.id]), {
            f"room-{rooms[0].id}-chair": self.judge.id,
            f"room-{rooms[1].id}-chair": self.judge.id,
        }, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "não pode ser alocado mais de uma vez")
        self.assertFalse(JudgeAllocation.objects.filter(judge=self.judge).exists())

        response = self.client.post(reverse("allocate_judges", args=[self.round.id]), {
            f"room-{rooms[0].id}-chair": self.judge.id,
        }, secure=True)
        self.assertRedirects(response, reverse("allocate_judges", args=[self.round.id]), fetch_redirect_response=False)
        self.assertEqual(JudgeAllocation.objects.filter(judge=self.judge).count(), 1)

    def test_judge_can_be_moved_between_rooms_without_unique_constraint_error(self):
        rooms = list(self.round.rooms.all())
        JudgeAllocation.objects.create(round=self.round, room=rooms[1], judge=self.judge, role="chair")

        response = self.client.post(reverse("allocate_judges", args=[self.round.id]), {
            f"room-{rooms[0].id}-chair": self.judge.id,
        }, secure=True)

        self.assertRedirects(response, reverse("allocate_judges", args=[self.round.id]), fetch_redirect_response=False)
        allocation = JudgeAllocation.objects.get(judge=self.judge)
        self.assertEqual(allocation.room, rooms[0])

    def test_judge_can_be_removed_without_unique_constraint_error(self):
        rooms = list(self.round.rooms.all())
        JudgeAllocation.objects.create(round=self.round, room=rooms[0], judge=self.judge, role="chair")

        response = self.client.post(reverse("allocate_judges", args=[self.round.id]), {}, secure=True)

        self.assertRedirects(response, reverse("allocate_judges", args=[self.round.id]), fetch_redirect_response=False)
        self.assertFalse(JudgeAllocation.objects.filter(judge=self.judge).exists())

    def test_manage_page_saves_round_unavailability_for_next_draw(self):
        response = self.client.get(reverse("manage_round", args=[self.round.id]), secure=True)
        self.assertContains(response, "Indisponíveis nesta rodada")

        self.client.post(reverse("update_round_availability", args=[self.round.id]), {
            "unavailable": [self.debaters[0].id],
        }, secure=True)
        self.assertTrue(RoundUnavailableDebater.objects.filter(round=self.round, debater=self.debaters[0]).exists())

        self.client.post(reverse("generate_draw", args=[self.round.id]), secure=True)
        drawn_ids = set(ParticipantSlot.objects.filter(pair__round=self.round).values_list("debater_id", flat=True))
        self.assertNotIn(self.debaters[0].id, drawn_ids)


class JudgePortalTests(TournamentTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="tab", password="secret")
        self.debaters = self.make_debaters(8)
        self.round = self.make_round()
        generate_prelim_draw(self.round)
        self.room = self.round.rooms.get()
        self.judge = Judge.objects.create(name="Chair Teste")
        JudgeAllocation.objects.create(round=self.round, room=self.room, judge=self.judge, role="chair")
        setting = SiteSettings.load()
        setting.current_round = self.round
        setting.save()

    def test_private_url_is_fixed_and_only_uses_current_chair_room(self):
        url = reverse("judge_portal", args=[self.judge.private_token])
        response = self.client.get(url, secure=True)
        self.assertContains(response, self.room.name)
        content = response.content.decode()
        self.assertLess(content.index('data-position="OG"'), content.index('data-position="OO"'))
        self.assertLess(content.index('data-position="OO"'), content.index('data-position="CG"'))
        self.assertLess(content.index('data-position="CG"'), content.index('data-position="CO"'))
        self.assertContains(response, 'min="50"')
        self.assertContains(response, 'max="100"')
        self.assertContains(response, "call-badge")
        self.assertEqual(self.client.get("/ballot/old-token/", secure=True).status_code, 404)

    def test_chair_can_submit_from_private_url(self):
        data = {}
        for index, pair in enumerate(self.room.pairs.prefetch_related("slots")):
            for slot in pair.slots.all():
                data[f"score_{slot.id}"] = 70 + index
        response = self.client.post(reverse("judge_portal", args=[self.judge.private_token]), data, secure=True)
        self.assertContains(response, "Resultado recebido")
        self.assertEqual(PairResult.objects.filter(room=self.room, submitted=True).count(), 4)

    def test_panel_does_not_receive_a_result_form(self):
        panel = Judge.objects.create(name="Panel Teste")
        JudgeAllocation.objects.create(round=self.round, room=self.room, judge=panel, role="panel")
        response = self.client.get(reverse("judge_portal", args=[panel.private_token]), secure=True)
        self.assertContains(response, "não está alocado como chair")
        self.assertNotContains(response, "result-form")

    def test_manage_page_lists_each_judges_fixed_url(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("judge_links"), secure=True)
        self.assertContains(response, self.judge.name)
        self.assertContains(response, reverse("judge_portal", args=[self.judge.private_token]))


class AdminResultTests(TournamentTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="tab", password="secret")
        self.client.force_login(self.user)
        self.make_debaters(8)
        self.round = self.make_round()
        generate_prelim_draw(self.round)
        self.room = self.round.rooms.get()
        self.chair = Judge.objects.create(name="Chair Admin")
        self.panel = Judge.objects.create(name="Panel Admin")
        JudgeAllocation.objects.create(round=self.round, room=self.room, judge=self.chair, role="chair")
        JudgeAllocation.objects.create(round=self.round, room=self.room, judge=self.panel, role="panel")

    def score_data(self):
        data = {}
        for index, pair in enumerate(self.room.pairs.prefetch_related("slots")):
            for slot in pair.slots.all():
                data[f"score_{slot.id}"] = 70 + index
        return data

    def test_admin_can_insert_edit_and_confirm_result(self):
        edit_url = reverse("edit_room_result", args=[self.room.id])
        self.assertContains(self.client.get(edit_url, secure=True), "Edição administrativa")
        response = self.client.post(edit_url, self.score_data(), secure=True)
        self.assertRedirects(response, reverse("manage_round", args=[self.round.id]), fetch_redirect_response=False)
        self.assertEqual(PairResult.objects.filter(room=self.room, submitted=True).count(), 4)

        manage = self.client.get(reverse("manage_round", args=[self.round.id]), secure=True)
        self.assertContains(manage, "Chair Admin")
        self.assertContains(manage, "Panel Admin")
        self.assertContains(manage, "Aguardando confirmação")
        self.assertNotContains(manage, "Trocar dois participantes")
        self.assertContains(manage, "Gerar draw novamente")
        self.assertEqual(self.client.post(f"/manage/round/{self.round.id}/swap/", secure=True).status_code, 404)

        self.client.post(reverse("confirm_result", args=[self.room.id]), secure=True)
        manage = self.client.get(reverse("manage_round", args=[self.round.id]), secure=True)
        self.assertContains(manage, "Confirmado")

        changed = self.score_data()
        first_key = next(iter(changed))
        changed[first_key] = 80
        self.client.post(edit_url, changed, secure=True)
        self.round.refresh_from_db()
        self.assertFalse(self.round.results_confirmed)


class RoundPublicationTests(TournamentTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="tab", password="secret")
        self.make_debaters(8)
        self.public_round = self.make_round(1)
        self.closed_round = self.make_round(4, silent=True)
        for round_obj in (self.public_round, self.closed_round):
            generate_prelim_draw(round_obj)
            round_obj.draw_published = True
            round_obj.save(update_fields=["draw_published"])
            room = round_obj.rooms.get()
            values = {}
            for index, pair in enumerate(room.pairs.prefetch_related("slots")):
                for slot in pair.slots.all():
                    values[slot.id] = Decimal(70 + index)
            submit_prelim(room, values)
            confirm_room(room)

    def test_closed_round_is_admin_only_until_tournament_ends(self):
        response = self.client.get(reverse("round_results"), secure=True)
        self.assertContains(response, self.public_round.name)
        self.assertNotContains(response, self.closed_round.name)

        self.client.force_login(self.user)
        response = self.client.get(reverse("admin_round_results"), secure=True)
        self.assertContains(response, self.public_round.name)
        self.assertContains(response, self.closed_round.name)

        setting = SiteSettings.load()
        setting.final_tab_published = True
        setting.save()
        response = self.client.get(reverse("round_results"), secure=True)
        self.assertContains(response, self.closed_round.name)


class AuthenticationTests(TestCase):
    def test_home_has_login_and_login_redirects_to_manage(self):
        self.assertContains(self.client.get(reverse("home"), secure=True), reverse("login"))
        get_user_model().objects.create_user(username="tab", password="secret")
        response = self.client.post(reverse("login"), {"username": "tab", "password": "secret"}, secure=True)
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
