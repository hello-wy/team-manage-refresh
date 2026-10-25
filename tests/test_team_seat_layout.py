import unittest
import json
from pathlib import Path
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.services.seat_snapshot import build_seat_layout


class TeamSeatLayoutTests(unittest.TestCase):
    def test_dashboard_renders_compact_rows_and_places_seat_board_only_in_member_modal(self):
        templates = Environment(
            loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "app/templates"),
            autoescape=select_autoescape(),
        )
        templates.filters.update(format_datetime=lambda value: str(value or "-"), escape_js=json.dumps)
        html = templates.get_template("admin/index.html").render(
            user={"username": "test"}, admin_profile={"avatar": "", "nickname": "管理员"},
            active_page="dashboard", stats={"total_teams": 1, "available_teams": 1},
            teams=[{"id": 1, "email": "owner@example.com", "team_name": "Example",
                    "joined_members": 1, "total_seats": 5, "seat_snapshot": None,
                    "status": "active", "pool_type": "normal"}],
            pagination={"current_page": 1, "per_page": 20, "total_pages": 1, "total": 1},
        )
        rows = html[html.index('<div id="teamTableRegion">'):html.index('</tbody>')]
        self.assertNotIn("team-seat-slot", rows)
        self.assertIn('id="memberPanelSeats"', html)
        self.assertIn('id="memberSeatsBoard"', html)
        self.assertIn('class="btn btn-sm btn-secondary btn-team-more"', rows)

    def test_unpurchased_premium_seats_keep_an_empty_zero_of_zero_progress_bar(self):
        templates = Environment(
            loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "app/templates"),
            autoescape=select_autoescape(),
        )
        templates.filters.update(format_datetime=lambda value: str(value or "-"), escape_js=json.dumps)
        html = templates.get_template("admin/index.html").render(
            user={"username": "test"}, admin_profile={"avatar": "", "nickname": "管理员"},
            active_page="dashboard", stats={"total_teams": 1, "available_teams": 1},
            teams=[{"id": 1, "email": "owner@example.com", "team_name": "Example",
                    "joined_members": 1, "total_seats": 1,
                    "seat_snapshot": {"standard_paid": 1, "standard_remaining": 0,
                                      "premium_paid": 0, "premium_remaining": 0,
                                      "observed_at": datetime.now()},
                    "status": "active", "pool_type": "normal"}],
            pagination={"current_page": 1, "per_page": 20, "total_pages": 1, "total": 1},
        )
        premium_row = html.split('title="高级席位：共 0 席，可用 0 席"', 1)[1].split('</div>', 1)[0]
        self.assertIn('<progress class="team-seat-progress" max="1" value="0"', premium_row)
        self.assertIn('可用 0/0', premium_row)
        self.assertNotIn('未购买', premium_row)

    def test_all_seats_include_members_invites_holds_and_empty_slots(self):
        layout = build_seat_layout([
            {"email": "owner@example.com", "status": "joined", "role": "account-owner", "seat_type": "default"},
            {"email": "invited@example.com", "status": "invited", "seat_type": "standard"},
            {"email": "premium@example.com", "status": "joined", "seat_type": "prolite"},
            {"email": "departed@example.com", "status": "removed", "seat_type": "default"},
        ], {"standard": {"known": True, "paid": 5, "remaining": 2},
            "premium": {"known": True, "paid": 2, "remaining": 1}})
        self.assertEqual([slot["state"] for slot in layout[0]["slots"]],
                         ["joined", "invited", "reserved", "available", "available"])
        self.assertEqual(layout[0]["slots"][0]["detail"], "所有者")
        self.assertEqual([slot["state"] for slot in layout[1]["slots"]], ["joined", "available"])
        self.assertEqual([group["seat_type"] for group in layout], ["default", "premium"])

    def test_stale_capacity_does_not_show_occupied_seats_as_invitable(self):
        layout = build_seat_layout([
            {"email": f"member{i}@example.com", "status": "joined", "seat_type": "standard"}
            for i in range(3)
        ], {"standard": {"known": True, "paid": 3, "remaining": 2}})
        self.assertEqual(layout[0]["remaining"], 0)
        self.assertEqual(len(layout[0]["slots"]), 3)
        self.assertFalse(any(slot["state"] == "available" for slot in layout[0]["slots"]))

    def test_premium_seat_carries_rotation_count_and_limit_but_standard_does_not(self):
        layout = build_seat_layout([
            {"email": "premium@example.com", "status": "joined", "seat_type": "premium",
             "rotation_round": 3, "rotation_rounds": 5},
            {"email": "standard@example.com", "status": "joined", "seat_type": "standard",
             "rotation_round": 1, "rotation_rounds": 5},
        ])
        premium, standard = layout[1]["slots"][0], layout[0]["slots"][0]
        self.assertEqual((premium["rotation_round"], premium["rotation_rounds"]), (3, 5))
        self.assertNotIn("rotation_round", standard)

    def test_unknown_capacity_and_unknown_member_types_never_create_empty_seats(self):
        layout = build_seat_layout([
            {"email": "unknown@example.com", "status": "joined", "seat_type": "usage_based"},
        ])
        self.assertEqual(layout[0]["slots"], [])
        self.assertIsNone(layout[0]["paid"])
        self.assertEqual(layout[2]["slots"][0]["label"], "unknown@example.com")
        self.assertTrue(all(slot["state"] != "available" for group in layout for slot in group["slots"]))

    def test_no_purchased_seats_and_large_workspaces_are_not_truncated(self):
        layout = build_seat_layout([], {
            "standard": {"known": True, "paid": 120, "remaining": 120},
            "premium": {"known": True, "paid": 0, "remaining": 0},
        })
        self.assertEqual(len(layout[0]["slots"]), 120)
        self.assertEqual(layout[1]["paid"], 0)
        self.assertEqual(layout[1]["slots"], [])
